import { expect, it, vi } from "vitest";
import type { WikiHttp, WikiHttpRequest } from "./api";
import { PURPOSE_GROUPS } from "../generated/service";
import { formatUsd, GROUP_LABEL, parseLimit, UsageClient } from "./usage";

const cfg = () => ({ baseUrl: "http://h:1", userId: "jerry" });
function http(text = "{}"): WikiHttp & { calls: WikiHttpRequest[] } {
  const calls: WikiHttpRequest[] = [];
  return { calls, request: vi.fn((r: WikiHttpRequest) => { calls.push(r); return Promise.resolve({ status: 200, text }); }) };
}

it.each([
  ["usage", (c: UsageClient) => c.usage(), "GET", "/usage", undefined],
  ["settings", (c: UsageClient) => c.settings(), "GET", "/usage/settings", undefined],
  ["patch daily", (c: UsageClient) => c.patch({ daily_limit_usd: 1.5 }), "PATCH", "/usage/settings", { daily_limit_usd: 1.5 }],
  ["patch remove", (c: UsageClient) => c.patch({ monthly_limit_usd: null }), "PATCH", "/usage/settings", { monthly_limit_usd: null }],
] as const)("%s", async (_n, f, method, path, body) => {
  const h = http('{"ok":1}');
  await expect(f(new UsageClient(cfg, h))).resolves.toEqual({ ok: 1 });
  expect(h.calls).toHaveLength(1);
  const r = h.calls[0];
  expect(r?.method).toBe(method);
  expect(r?.url).toBe("http://h:1" + path);
  expect(r?.headers["X-EvoWiki-User"]).toBe("jerry");
  if (body === undefined) {
    expect(r?.body).toBeUndefined();
  } else {
    expect(r?.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse((r?.body as string | undefined) ?? "")).toEqual(body);
  }
});

it("a non-2xx answer is an ApiError with the status", async () => {
  const h: WikiHttp = { request: () => Promise.resolve({ status: 422, text: '{"detail":"bad"}' }) };
  await expect(new UsageClient(cfg, h).patch({ daily_limit_usd: 1 })).rejects.toMatchObject({ status: 422 });
});

it("parseLimit: only numbers", () => {
  expect(parseLimit("")).toBeNull();
  expect(parseLimit("   ")).toBeNull();
  expect(parseLimit(" 2 ")).toBe(2);
  expect(parseLimit("0.5")).toBe(0.5);
  expect(parseLimit("0")).toBe(0);
  for (const bad of ["-1", "abc", "1e3", "NaN", "Infinity", "1,5", "$2", ".", "1.", "0x10"]) expect(parseLimit(bad), bad).toBeUndefined();
});

it("formatUsd", () => {
  expect(formatUsd(0)).toBe("$0");
  expect(formatUsd(0.004)).toBe("不到 $0.01");
  expect(formatUsd(1.5)).toBe("$1.50");
  expect(formatUsd(12.345)).toBe("$12.35");
});

// The wire shapes and constant tables come from src/generated/service.ts (the fixtures are typed against them).
it("every purpose group has a label, in the backend's order", () => {
  expect(Object.keys(GROUP_LABEL)).toEqual([...PURPOSE_GROUPS]);
});
