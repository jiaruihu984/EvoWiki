import { describe, expect, it, vi } from "vitest";
import { ApiError, OfflineError } from "./errors";
import { EvoWikiClient, type HttpRequest, type JsonTransport } from "./client";

const cfg = () => ({ baseUrl: "http://127.0.0.1:8000", userId: "jerry" });
function json(status: number, body: unknown): JsonTransport & { calls: HttpRequest[] } {
  const calls: HttpRequest[] = [];
  return { calls, request: vi.fn((r: HttpRequest) => { calls.push(r); return Promise.resolve({ status, text: JSON.stringify(body) }); }) };
}

describe("me", () => {
  it("GETs /me with the user header and returns the user", async () => {
    const user = { user_id: "jerry", background: "b", condition: "S2", lang: "zh" };
    const t = json(200, user);
    expect(await new EvoWikiClient(cfg, t).me()).toEqual(user);
    expect(t.calls).toEqual([{ method: "GET", url: "http://127.0.0.1:8000/me", headers: { "X-EvoWiki-User": "jerry" } }]);
  });

  it("POSTs /users with exactly the body as JSON and the user header", async () => {
    const t = json(201, { user_id: "jerry", background: "none", condition: "S2", lang: "zh" });
    const body = { user_id: "jerry", survey: { math: "none", ml: "none", dl: "none" }, lang: "zh" } as const;
    expect((await new EvoWikiClient(cfg, t).createUser(body)).lang).toBe("zh");
    expect(t.calls[0]).toMatchObject({ method: "POST", url: "http://127.0.0.1:8000/users",
      headers: { "X-EvoWiki-User": "jerry", "Content-Type": "application/json" } });
    expect(JSON.parse(t.calls[0]!.body!)).toEqual(body);
  });

  it("maps non-2xx to ApiError with the server detail", async () => {
    await expect(new EvoWikiClient(cfg, json(404, { detail: "no such user" })).me()).rejects.toEqual(new ApiError(404, "no such user"));
  });

  it("lets OfflineError from the transport through unchanged", async () => {
    const t: JsonTransport = { request: vi.fn(() => Promise.reject(new OfflineError())) };
    await expect(new EvoWikiClient(cfg, t).me()).rejects.toBeInstanceOf(OfflineError);
  });
});

describe("server readiness before account access", () => {
  it("checks the protocol and core readiness through the configured transport", async () => {
    const request = vi.fn((r: HttpRequest) => Promise.resolve({ status: 200,
      text: JSON.stringify(r.url.endsWith("/health") ? { protocol_version: 3 } : { ok: true }) }));
    await new EvoWikiClient(cfg, { request }).checkServer();
    expect(request.mock.calls.map(([r]) => r.url)).toEqual([
      "http://127.0.0.1:8000/health", "http://127.0.0.1:8000/health/ready"]);
  });

  it("a protocol mismatch stops before readiness or account operations", async () => {
    const t = json(200, { protocol_version: 1 });
    await expect(new EvoWikiClient(cfg, t).checkServer()).rejects.toMatchObject({
      status: 409, code: "protocol_mismatch" });
    expect(t.calls.map((r) => r.url)).toEqual(["http://127.0.0.1:8000/health"]);
  });

  it("an unready server retains its actionable reason and does not retry itself", async () => {
    const request = vi.fn((r: HttpRequest) => Promise.resolve(r.url.endsWith("/health")
      ? { status: 200, text: '{"protocol_version":3}' }
      : { status: 503, text: '{"detail":"正在核对删除记录","code":"memory_recovering"}' }));
    await expect(new EvoWikiClient(cfg, { request }).checkServer()).rejects.toMatchObject({
      status: 503, detail: "正在核对删除记录", code: "memory_recovering" });
    expect(request).toHaveBeenCalledTimes(2);
  });
});
