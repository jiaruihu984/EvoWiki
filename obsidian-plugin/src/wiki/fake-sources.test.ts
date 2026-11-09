import { describe, expect, it } from "vitest";
import { AbortedError, ApiError } from "../api/errors";
import { FakeWikiApi } from "./fake";
import type { SourceEvent } from "./schema";

const pdf = (): ArrayBuffer => new TextEncoder().encode("%PDF-1.4 fake").buffer;
const follow = async (api: FakeWikiApi, id: string): Promise<SourceEvent[]> => {
  const seen: SourceEvent[] = [];
  await api.followSource(id, (e) => seen.push(e));
  return seen;
};

describe("FakeWikiApi sources", () => {
  it("upload → reading; the progress stream steps through the stages and ends with done; then ready with sections", async () => {
    const api = new FakeWikiApi();
    const s = await api.uploadSource({ file: { name: "讲义.pdf", data: pdf() }, use: "learn" });
    expect(s).toMatchObject({ title: "讲义", kind: "pdf", status: "reading", deleted: false, error: null });
    const evs = await follow(api, s.id);
    expect(evs.slice(0, -1).every((e) => e.event === "progress")).toBe(true);
    expect(new Set(evs.slice(0, -1).map((e) => (e.event === "progress" ? e.data.stage : "")))).toEqual(new Set(["parse", "toc", "embed", "summary"]));
    const done = evs[evs.length - 1]!;
    expect(done.event).toBe("done");
    const d = await api.getSource(s.id);
    expect(d.status).toBe("ready");
    expect(done.data).toEqual({ title: "讲义", status: "ready", pages: d.pages, sections: d.sections.length, unreadable_pages: [], note: null });
    expect(d.sections.length).toBeGreaterThan(0);
    expect(d.uses.map((u) => u.use)).toEqual(["learn"]);
    // a second follow replays only the end
    expect((await follow(api, s.id)).map((e) => e.event)).toEqual(["done"]);
  });

  it("the same content again returns the same source; list is newest first", async () => {
    let t = 0;
    const api = new FakeWikiApi({ now: () => new Date(Date.UTC(2026, 9, 21, 8, 0, t++)) });
    const a = await api.uploadSource({ text: "第一段笔记", title: "笔记" });
    const b = await api.uploadSource({ url: "https://example.org/a" });
    expect((await api.uploadSource({ text: "第一段笔记" })).id).toBe(a.id);
    expect((await api.listSources()).map((x) => x.id)).toEqual([b.id, a.id]);
    expect(a).toMatchObject({ kind: "text", title: "笔记" });
    expect(b).toMatchObject({ kind: "url", origin: "https://example.org/a" });
  });

  it("refuses: not exactly one input (422), an unsupported file (415), a file over the cap (413)", async () => {
    const api = new FakeWikiApi();
    await expect(api.uploadSource({ text: "  " })).rejects.toMatchObject({ status: 422 });
    await expect(api.uploadSource({ text: "x", url: "https://a" } as never)).rejects.toMatchObject({ status: 422 });
    await expect(api.uploadSource({ file: { name: "a.exe", data: pdf() } })).rejects.toMatchObject({ status: 415 });
    await expect(api.uploadSource({ file: { name: "a.pdf", data: new ArrayBuffer(50 * 1024 * 1024 + 1) } }))
      .rejects.toMatchObject({ status: 413 });
    expect(await api.listSources()).toEqual([]);
  });

  it("a chat attachment used only to ask is not listed until it gets another use", async () => {
    const api = new FakeWikiApi();
    const s = await api.uploadSource({ file: { name: "a.pdf", data: pdf() }, use: "ask", session_id: "chat1" });
    expect(await api.listSources()).toEqual([]);
    await api.addSourceUse(s.id, { use: "learn" });
    expect((await api.listSources()).map((x) => x.id)).toEqual([s.id]);
  });

  it("file, rename, uses, delete; a deleted source is a 404 everywhere", async () => {
    const api = new FakeWikiApi();
    const data = pdf();
    const s = await api.uploadSource({ file: { name: "a.pdf", data } });
    expect(new Uint8Array(await api.sourceFile(s.id))).toEqual(new Uint8Array(data));
    expect((await api.renameSource(s.id, "  新名字 ")).title).toBe("新名字");
    await expect(api.renameSource(s.id, "   ")).rejects.toMatchObject({ status: 422 });
    const u = await api.addSourceUse(s.id, { use: "exam", goal_id: "g1" });
    expect(u).toMatchObject({ use: "exam", goal_id: "g1", page_id: null, session_id: null });
    await expect(api.deleteSource(s.id)).resolves.toBeUndefined();
    for (const p of [api.getSource(s.id), api.sourceFile(s.id), api.deleteSource(s.id), api.renameSource(s.id, "x"),
      api.addSourceUse(s.id, { use: "ask" }), api.followSource(s.id, () => {})]) {
      await expect(p).rejects.toBeInstanceOf(ApiError);
    }
    expect(await api.listSources()).toEqual([]);
  });

  it("__failSourceImport: the next import ends with error and the source is failed", async () => {
    const api = new FakeWikiApi();
    api.__failSourceImport("读不出这份 PDF");
    const s = await api.uploadSource({ file: { name: "a.pdf", data: pdf() } });
    const evs = await follow(api, s.id);
    expect(evs[evs.length - 1]).toEqual({ event: "error", data: { detail: "读不出这份 PDF", code: null } });
    expect(await api.getSource(s.id)).toMatchObject({ status: "failed", error: "读不出这份 PDF" });
    expect(await follow(api, s.id)).toEqual([{ event: "error", data: { detail: "读不出这份 PDF", code: null } }]);
  });

  it("an aborted follow throws AbortedError and leaves the source reading", async () => {
    const api = new FakeWikiApi({ delayMs: 5 });
    const s = await api.uploadSource({ text: "abc" });
    const ac = new AbortController();
    const p = api.followSource(s.id, () => { ac.abort(); }, { signal: ac.signal });
    await expect(p).rejects.toBeInstanceOf(AbortedError);
    expect((await api.getSource(s.id)).status).toBe("reading");
  });
});

describe("FakeWikiApi sources: matches the backend (review fixes)", () => {
  it("after a failed import the same bytes again start a new source (store.find_by_sha skips failed ones)", async () => {
    const api = new FakeWikiApi();
    api.__failSourceImport("读不出这份 PDF");
    const a = await api.uploadSource({ file: { name: "a.pdf", data: pdf() } });
    await follow(api, a.id);
    expect((await api.getSource(a.id)).status).toBe("failed");
    const b = await api.uploadSource({ file: { name: "a.pdf", data: pdf() } });
    expect(b.id).not.toBe(a.id);
    expect(b.status).toBe("reading");
    expect((await follow(api, b.id)).map((e) => e.event).pop()).toBe("done");
    // a deleted one is skipped too
    await api.deleteSource(b.id);
    const c = await api.uploadSource({ file: { name: "a.pdf", data: pdf() } });
    expect([a.id, b.id]).not.toContain(c.id);
  });

  it("failNext works on followSource: an ErrorEvent ends the stream with SourceFailed {detail}; an ApiError rejects", async () => {
    const api = new FakeWikiApi();
    const s = await api.uploadSource({ text: "abc" });
    api.failNext("followSource", { code: "llm_failed", retryable: true, detail: "出错了" });
    expect(await follow(api, s.id)).toEqual([{ event: "error", data: { detail: "出错了" } }]);
    api.failNext("followSource", new ApiError(404, "找不到这份资料"));
    await expect(api.followSource(s.id, () => {})).rejects.toMatchObject({ status: 404 });
    expect((await follow(api, s.id)).map((e) => e.event).pop()).toBe("done");     // one-shot
  });

  it("a new source has no progress until the first progress event; whitespace-only text is the backend's 422", async () => {
    const api = new FakeWikiApi();
    const s = await api.uploadSource({ text: "abc" });
    expect(s.progress).toBeNull();
    expect((await api.listSources())[0]!.progress).toBeNull();
    await api.followSource(s.id, () => {});
    expect((await api.getSource(s.id)).progress).toMatchObject({ stage: "summary" });
    await expect(api.uploadSource({ text: "  \n " })).rejects.toMatchObject({ status: 422, detail: "文件里没有可读的文字" });
    await expect(api.uploadSource({ text: "" })).rejects.toMatchObject({ status: 422, detail: "文件、网址、文字三选一" });
  });
});
