import { describe, expect, it, vi } from "vitest";
import type { HttpRequest, StreamTransport } from "../api/client";
import { ApiError } from "../api/errors";
import { HttpWikiClient, type WikiHttp, type WikiHttpRequest } from "./api";
import { SOURCE_EVENTS, type SourceEvent } from "./schema";

const cfg = () => ({ baseUrl: "http://h:1", userId: "jerry" });
type Res = { status: number; text: string; bytes?: ArrayBuffer };
function http(res: Res = { status: 200, text: "{}" }): WikiHttp & { calls: WikiHttpRequest[] } {
  const calls: WikiHttpRequest[] = [];
  return { calls, request: vi.fn((r: WikiHttpRequest) => { calls.push(r); return Promise.resolve(res); }) };
}
const noSse: StreamTransport = { stream: vi.fn() };
function sse(chunks: string[], status = 200): StreamTransport & { reqs: (HttpRequest & { signal?: AbortSignal })[] } {
  const out = {
    reqs: [] as (HttpRequest & { signal?: AbortSignal })[],
    stream: vi.fn<StreamTransport["stream"]>((r, onChunk) => {
      out.reqs.push(r);
      if (status >= 200 && status < 300) for (const c of chunks) onChunk(c);
      return Promise.resolve({ status, text: status >= 200 && status < 300 ? "" : '{"detail":"找不到这份资料"}' });
    }),
  };
  return out;
}
const latin = (b: ArrayBuffer): string => Array.from(new Uint8Array(b), (x) => String.fromCharCode(x)).join("");
const utf8 = (s: string): string => latin(new TextEncoder().encode(s).buffer);

describe("sources: JSON calls", () => {
  it.each([
    ["listSources", (c: HttpWikiClient) => c.listSources(), "GET", "/wiki/sources", undefined],
    ["getSource", (c: HttpWikiClient) => c.getSource("s/1"), "GET", "/wiki/sources/s%2F1", undefined],
    ["addSourceUse", (c: HttpWikiClient) => c.addSourceUse("s1", { use: "exam", goal_id: "g1" }), "POST", "/wiki/sources/s1/uses",
      { use: "exam", goal_id: "g1" }],
    ["renameSource", (c: HttpWikiClient) => c.renameSource("s1", "新名字"), "PATCH", "/wiki/sources/s1", { title: "新名字" }],
    ["deleteSource", (c: HttpWikiClient) => c.deleteSource("s1"), "DELETE", "/wiki/sources/s1", undefined],
    ["pageExam", (c: HttpWikiClient) => c.pageExam("p/1"), "GET", "/wiki/pages/p%2F1/exam", undefined],
    ["examSelf", (c: HttpWikiClient) => c.examSelf("it/1", "right"), "POST", "/wiki/exam/items/it%2F1/self", { result: "right" }],
  ] as const)("%s", async (_n, run, method, path, body) => {
    const t = http({ status: method === "DELETE" ? 204 : 200, text: method === "DELETE" ? "" : "{}" });
    await run(new HttpWikiClient(cfg, t, noSse));
    expect(t.calls).toHaveLength(1);
    const req = t.calls[0]!;
    expect(req).toMatchObject({ method, url: `http://h:1${path}` });
    expect(req.headers["X-EvoWiki-User"]).toBe("jerry");
    if (body === undefined) expect(req.body).toBeUndefined();
    else expect(JSON.parse(req.body as string)).toEqual(body);
  });
});

describe("uploadSource (multipart)", () => {
  it("a file: one POST /wiki/sources, multipart body as an ArrayBuffer, the fields and the bytes", async () => {
    const t = http({ status: 201, text: '{"id":"src1"}' });
    const data = Uint8Array.from([0x25, 0x50, 0x44, 0x46, 0xff]).buffer;
    const out = await new HttpWikiClient(cfg, t, noSse).uploadSource({ file: { name: "讲义.pdf", data, type: "application/pdf" },
      origin: "资料/讲义.pdf", use: "learn", goal_id: "g1" });
    expect(out).toEqual({ id: "src1" });
    expect(t.calls).toHaveLength(1);
    const req = t.calls[0]!;
    expect(req).toMatchObject({ method: "POST", url: "http://h:1/wiki/sources" });
    expect(req.body).toBeInstanceOf(ArrayBuffer);
    expect(req.contentType).toMatch(/^multipart\/form-data; boundary=[A-Za-z0-9-]+$/);
    expect(req.headers["Content-Type"]).toBe(req.contentType);
    expect(req.headers["X-EvoWiki-User"]).toBe("jerry");
    const raw = latin(req.body as ArrayBuffer);
    expect(raw).toContain(`name="origin"\r\n\r\n${utf8("资料/讲义.pdf")}\r\n`);
    expect(raw).toContain('name="use"\r\n\r\nlearn\r\n');
    expect(raw).toContain('name="goal_id"\r\n\r\ng1\r\n');
    expect(raw).toContain(`name="file"; filename="${utf8("讲义.pdf")}"; filename*=UTF-8''%E8%AE%B2%E4%B9%89.pdf\r\n`);
    expect(raw).toContain("Content-Type: application/pdf\r\n\r\n%PDF\xff\r\n");
    expect(raw).not.toContain('name="url"');
    expect(raw).not.toContain('name="text"');
    expect(raw).not.toContain('name="page_id"');
  });

  it("pasted text and a URL go as plain fields (no file part)", async () => {
    const t = http({ status: 201, text: "{}" });
    const c = new HttpWikiClient(cfg, t, noSse);
    await c.uploadSource({ text: "第一段", title: "笔记" });
    await c.uploadSource({ url: "https://example.org/a" });
    const [a, b] = t.calls.map((r) => latin(r.body as ArrayBuffer));
    expect(a).toContain(`name="text"\r\n\r\n${utf8("第一段")}\r\n`);
    expect(a).toContain(`name="title"\r\n\r\n${utf8("笔记")}\r\n`);
    expect(a).not.toContain("filename");
    expect(b).toContain('name="url"\r\n\r\nhttps://example.org/a\r\n');
  });

  it("a 4xx is an ApiError with the server's detail (200 = the same file again is fine)", async () => {
    await expect(new HttpWikiClient(cfg, http({ status: 413, text: '{"detail":"文件太大了"}' }), noSse)
      .uploadSource({ text: "x" })).rejects.toMatchObject({ status: 413, detail: "文件太大了" });
    await expect(new HttpWikiClient(cfg, http({ status: 200, text: '{"id":"s"}' }), noSse).uploadSource({ text: "x" }))
      .resolves.toEqual({ id: "s" });
  });
});

describe("sourceFile (binary)", () => {
  it("GET …/file asking for bytes; returns them as-is", async () => {
    const bytes = Uint8Array.from([1, 2, 255]).buffer;
    const t = http({ status: 200, text: "", bytes });
    await expect(new HttpWikiClient(cfg, t, noSse).sourceFile("s/1")).resolves.toBe(bytes);
    expect(t.calls[0]).toMatchObject({ method: "GET", url: "http://h:1/wiki/sources/s%2F1/file", binary: true });
    expect(t.calls[0]!.body).toBeUndefined();
  });
  it("non-2xx → ApiError; a 2xx without bytes fails closed", async () => {
    await expect(new HttpWikiClient(cfg, http({ status: 404, text: '{"detail":"找不到这份资料"}' }), noSse).sourceFile("s"))
      .rejects.toMatchObject({ status: 404, detail: "找不到这份资料" });
    await expect(new HttpWikiClient(cfg, http({ status: 200, text: "x" }), noSse).sourceFile("s")).rejects.toBeInstanceOf(ApiError);
  });
});

const PROG = 'event: progress\ndata: {"stage":"toc","done":1,"total":3,"message":"正在读 1/3 章"}\n\n';
const DONE = 'event: done\ndata: {"status":"ready","pages":3,"sections":2,"unreadable_pages":[],"note":null}\n\n';
const ERR = 'event: error\ndata: {"detail":"读不出这份 PDF"}\n\n';

describe("followSource (GET SSE)", () => {
  it("SOURCE_EVENTS", () => { expect([...SOURCE_EVENTS]).toEqual(["progress", "usable", "done", "error"]); });

  it("GETs the progress URL (no body) and delivers progress then done", async () => {
    const s = sse([PROG, DONE.slice(0, 20), DONE.slice(20)]);
    const seen: SourceEvent[] = [];
    const ac = new AbortController();
    await new HttpWikiClient(cfg, http(), s).followSource("s/1", (e) => seen.push(e), { signal: ac.signal });
    expect(s.reqs[0]).toMatchObject({ method: "GET", url: "http://h:1/wiki/sources/s%2F1/progress", headers: { "X-EvoWiki-User": "jerry" } });
    expect(s.reqs[0]!.headers["Content-Type"]).toBeUndefined();
    expect(s.reqs[0]!.body).toBeUndefined();
    expect(s.reqs[0]!.signal).toBe(ac.signal);
    expect(seen.map((e) => e.event)).toEqual(["progress", "done"]);
    expect(seen[0]!.data).toEqual({ stage: "toc", done: 1, total: 3, message: "正在读 1/3 章" });
  });

  it("only SOURCE_EVENTS reach the callback; nothing after done", async () => {
    const seen: string[] = [];
    await new HttpWikiClient(cfg, http(), sse(["event: outline\ndata: {}\n\n", PROG, DONE, PROG, ERR]))
      .followSource("s1", (e) => seen.push(e.event));
    expect(seen).toEqual(["progress", "done"]);
  });

  it("usable reaches the callback and the stream goes on to done", async () => {
    const USABLE = 'event: usable\ndata: {"status":"partial","pages":3,"sections":2,"unreadable_pages":[2],"note":null}\n\n';
    const seen: SourceEvent[] = [];
    await new HttpWikiClient(cfg, http(), sse([PROG, USABLE, PROG, DONE])).followSource("s1", (e) => seen.push(e));
    expect(seen.map((e) => e.event)).toEqual(["progress", "usable", "progress", "done"]);
    expect(seen[1]!.data).toEqual({ status: "partial", pages: 3, sections: 2, unreadable_pages: [2], note: null });
  });

  it("stops at error", async () => {
    const seen: SourceEvent[] = [];
    await new HttpWikiClient(cfg, http(), sse([PROG, ERR, PROG, DONE])).followSource("s1", (e) => seen.push(e));
    expect(seen.map((e) => e.event)).toEqual(["progress", "error"]);
    expect(seen[1]!.data).toEqual({ detail: "读不出这份 PDF" });
  });

  it("a 404 (deleted / someone else's) is an ApiError", async () => {
    await expect(new HttpWikiClient(cfg, http(), sse([], 404)).followSource("s1", () => {}))
      .rejects.toEqual(new ApiError(404, "找不到这份资料"));
  });
});
