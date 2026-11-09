import { describe, expect, it, vi } from "vitest";
import type { BinaryVault, NoteVault } from "../../notes/note-service";
import { setup, until } from "../controller-test-kit";
import type { SourcePort } from "./ports";
import { freeOriginalPath, originalsFolder, writeOriginal } from "./save-original";
import { SOURCES_COPY } from "./sources-copy";
import { ApiError, OfflineError } from "../../api/errors";
import { WIKI_COPY } from "../copy";
import { EXAM_POLL_MS, SourceBlocked, SourceRefused } from "./sources-controller";

const bytes = (s: string): ArrayBuffer => new TextEncoder().encode(s).buffer;

const pdf = (name: string, body: string): File => {
  const f = new File([body], name, { type: "application/pdf" });
  // jsdom's File may lack arrayBuffer: give it one.
  if (typeof f.arrayBuffer !== "function") Object.defineProperty(f, "arrayBuffer", { value: () => Promise.resolve(bytes(body)) });
  return f;
};
/** An in-memory vault under the real save-original.ts: createBinary rejects an existing path, like Obsidian. */
function memPort(vaultFiles: Record<string, string> = {}, failOpen = false): SourcePort & { written: Record<string, string>; opened: string[] } {
  const written: Record<string, string> = {};
  const folders = new Set<string>();
  const store: Pick<NoteVault, "occupied" | "ensureFolder"> & BinaryVault = {
    occupied: (p) => Promise.resolve(p in written || p in vaultFiles || folders.has(p)),
    ensureFolder: (p) => { folders.add(p); return Promise.resolve(); },
    createBinary: (p, d) => {
      if (p in written || p in vaultFiles) return Promise.reject(new Error("exists"));
      written[p] = new TextDecoder().decode(d);
      return Promise.resolve();
    },
  };
  const opened: string[] = [];
  return {
    written, opened,
    originalPath: (n) => freeOriginalPath(store, "EvoWiki", n),
    writeOriginal: (p, d) => writeOriginal(store, p, d),
    openFile: (p) => { opened.push(p); return Promise.resolve(!failOpen && (p in vaultFiles || p in written)); },
    exists: (p) => p in vaultFiles || p in written,
  };
}

/** A source the server got some other way (a chat attachment): uploaded on the fake, then the list read again (and followed). */
async function added(s: ReturnType<typeof setup>, name: string, body: string, origin?: string): Promise<void> {
  await s.api.uploadSource({ file: { name, data: bytes(body), type: "application/pdf" }, ...(origin !== undefined ? { origin } : {}) });
  await s.c.sourcesReload();
}
const list = (s: ReturnType<typeof setup>) => (s.c.state.sources.kind === "ready" ? s.c.state.sources.value : []);

describe("SourcesController (参数 · 资料)", () => {
  it("a source being read is followed to the end: progress is kept per id, the card ends ready", async () => {
    const s = setup({ sourcePort: memPort() });
    await added(s, "a.pdf", "one");
    await until(() => list(s)[0]?.status === "ready");
    expect(list(s)[0]?.title).toBe("a");
    expect(s.c.state.sourceProgress[list(s)[0]?.id ?? ""]).toBeDefined();
    await s.c.loadEngine();
    expect(list(s).map((x) => x.status)).toEqual(["ready"]);
  });

  it("a progress item does not redraw the panel: the progress listeners hear every item, the state listeners only the end", async () => {
    const s = setup({ sourcePort: memPort(), delayMs: 1 });
    let redraws = 0;
    let ticks = 0;
    s.c.subscribe(() => { redraws++; });
    s.c.subscribeProgress(() => { ticks++; });
    await added(s, "a.pdf", "x");
    const base = redraws; // the list read itself redrew (the new card)
    await until(() => list(s)[0]?.status === "ready");
    expect(ticks).toBeGreaterThanOrEqual(3);
    expect(redraws - base).toBeLessThanOrEqual(1); // the done item
  });

  it("a failed import shows the server's reason on the card", async () => {
    const s = setup({ sourcePort: memPort() });
    s.api.__failSourceImport("这个文件打不开（加了密码？）");
    await added(s, "secret.pdf", "locked");
    await until(() => list(s)[0]?.status === "failed");
    expect(list(s)[0]?.error).toBe("这个文件打不开（加了密码？）");
  });

  it("delete asks first, deletes only the source (no vault file is touched) and says the pages keep their citations", async () => {
    const port = memPort({ "Notes/a.pdf": "V" });
    const s = setup({ sourcePort: port });
    await added(s, "a.pdf", "1", "Notes/a.pdf");
    const id = list(s)[0]?.id ?? "";
    s.c.sourceAskDelete(id);
    expect(s.c.state.sourceUi.confirm).toBe(id);
    expect(list(s)).toHaveLength(1);
    expect(s.api.calls.some((x) => x.method === "deleteSource")).toBe(false);
    await s.c.sourceConfirmDelete(id);
    expect(list(s)).toHaveLength(0);
    expect(s.c.state.sourceUi.confirm).toBeNull();
    expect(port.written).toEqual({});
    expect(port.exists("Notes/a.pdf")).toBe(true);
    expect(s.notices[s.notices.length - 1]).toBe(SOURCES_COPY.deleted("a"));
  });

  it("rename keeps the typed draft and saves it", async () => {
    const s = setup({ sourcePort: memPort() });
    await added(s, "a.pdf", "1");
    const id = list(s)[0]?.id ?? "";
    s.c.sourceStartRename(id);
    s.c.sourceSetRenameDraft("新名字");
    await s.c.sourceCommitRename();
    expect(list(s)[0]?.title).toBe("新名字");
    expect(s.c.state.sourceUi.rename).toBeNull();
  });

  it("打开: a vault origin opens that file; a source without one is downloaded once into 资料 and opened", async () => {
    const port = memPort({ "Notes/v.pdf": "V" });
    const s = setup({ sourcePort: port });
    await added(s, "v.pdf", "V", "Notes/v.pdf");
    await s.c.sourceOpen(list(s)[0]?.id ?? "");
    expect(port.opened).toEqual(["Notes/v.pdf"]);
    // A source with no vault file: the server's copy is saved under 资料 and opened.
    await added(s, "web.pdf", "W");
    const web = list(s).find((x) => x.title === "web");
    await s.c.sourceOpen(web?.id ?? "");
    expect(port.written["EvoWiki/资料/web.pdf"]).toBe("W");
    expect(port.opened[port.opened.length - 1]).toBe("EvoWiki/资料/web.pdf");
    await s.c.sourceOpen(web?.id ?? ""); // again: reused, not "(2)"
    expect(Object.keys(port.written)).toEqual(["EvoWiki/资料/web.pdf"]);
  });

  it("「读好了 · N 章」 counts the sections of the end item, not the chunk count of the last progress item", async () => {
    const s = setup({ sourcePort: memPort() });
    s.api.followSource = (_id, on) => {
      on({ event: "progress", data: { stage: "embed", done: 140, total: 140, message: "正在读 140/140 块" } });
      on({ event: "done", data: { title: null, status: "ready", pages: 12, sections: 9, unreadable_pages: [], note: null } });
      return Promise.resolve();
    };
    await added(s, "a.pdf", "1");
    await until(() => list(s)[0]?.status === "ready");
    const p = s.c.state.sourceProgress[list(s)[0]?.id ?? ""];
    expect([p?.done, p?.total]).toEqual([9, 9]);
  });

  it("a stream cut on a step (the tab left) leaves no count behind once the source is read: the list's own count shows", async () => {
    const s = setup({ sourcePort: memPort() });
    s.api.followSource = (_id, on) => {
      on({ event: "progress", data: { stage: "toc", done: 1, total: 1, message: "正在整理符号和术语" } });
      return new Promise<void>(() => undefined);
    };
    await added(s, "a.pdf", "1");
    const id = list(s)[0]?.id ?? "";
    await until(() => s.c.state.sourceProgress[id]?.total === 1);
    s.c.setTab("engine"); s.c.setTab("today");
    s.api.followSource = () => Promise.resolve();
    const rows = await s.api.listSources();
    s.api.listSources = () => Promise.resolve(rows.map((x) => ({ ...x, status: "ready" as const })));
    await s.c.sourcesReload();
    expect(s.c.state.sourceProgress[id]).toBeUndefined();
  });

  it("usable shows the card's status while the stream goes on (it is not an end)", async () => {
    const s = setup({ sourcePort: memPort() });
    let finish: () => void = () => undefined;
    s.api.followSource = (_id, on) => {
      on({ event: "usable", data: { title: null, status: "partial", pages: 12, sections: 9, unreadable_pages: [3], note: null } });
      return new Promise<void>((r) => { finish = () => {
        on({ event: "done", data: { title: null, status: "ready", pages: 12, sections: 9, unreadable_pages: [], note: null } }); r(); }; });
    };
    await added(s, "a.pdf", "1");
    await until(() => list(s)[0]?.status === "partial");
    expect(list(s)[0]?.unreadable_pages).toEqual([3]);
    finish();
    await until(() => list(s)[0]?.status === "ready");
  });

  it("a list read still in flight cannot overwrite or resurrect cards after a delete", async () => {
    const s = setup({ sourcePort: memPort() });
    await added(s, "a.pdf", "1");
    const id = list(s)[0]?.id ?? "";
    let release: () => void = () => undefined;
    const real = s.api.listSources.bind(s.api);
    s.api.listSources = async () => { const v = await real(); await new Promise<void>((r) => { release = r; }); return v; };
    s.api.calls.length = 0;
    const reading = s.c.sourcesReload();
    await until(() => s.api.calls.length > 0);
    await s.c.sourceConfirmDelete(id);
    release();
    await reading;
    expect(list(s)).toHaveLength(0);
  });

  it("打开: a file that will not open says so; the demo says it has no real file", async () => {
    const port = memPort({ "Notes/v.pdf": "V" }, true);
    const s = setup({ sourcePort: port });
    await added(s, "v.pdf", "V", "Notes/v.pdf");
    await s.c.sourceOpen(list(s)[0]?.id ?? "");
    expect(s.notices[s.notices.length - 1]).toBe(SOURCES_COPY.openFailed("v"));
    const d = setup({ demo: true, sourcePort: memPort() });
    await added(d, "x.pdf", "x");
    await d.c.sourceOpen(list(d)[0]?.id ?? "");
    expect(d.notices[d.notices.length - 1]).toBe(SOURCES_COPY.openDemo);
  });

  it("a citation opens a chat attachment at its page: saved into the vault once, the server told where; deleted says so", async () => {
    const port = memPort();
    const open = vi.spyOn(port, "openFile");
    const s = setup({ sourcePort: port });
    const att = await s.api.uploadSource({ file: { name: "a.pdf", data: bytes("A") }, use: "ask", session_id: "s1" });
    expect(att.origin).toBeNull();
    expect(await s.c.openSourceAt(att.id, 4)).toBe("opened");
    expect(open).toHaveBeenLastCalledWith(Object.keys(port.written)[0], 4);
    expect((await s.api.getSource(att.id)).origin).toBe(Object.keys(port.written)[0]);
    expect(await s.c.openSourceAt(att.id, 5)).toBe("opened");
    expect(Object.keys(port.written)).toHaveLength(1);
    await s.api.deleteSource(att.id);
    expect(await s.c.openSourceAt(att.id, 4)).toBe("gone");
    expect(s.notices[s.notices.length - 1]).toBe(SOURCES_COPY.citedGone);
  });

  it("the demo and the offline state change nothing (delete)", async () => {
    const d = setup({ demo: true, sourcePort: memPort() });
    await added(d, "a.pdf", "1");
    await d.c.sourceConfirmDelete(list(d)[0]?.id ?? "");
    expect(d.notices).toEqual([SOURCES_COPY.blockedDemo]);
    expect(d.api.calls.some((x) => x.method === "deleteSource")).toBe(false);
    const o = setup({ sourcePort: memPort() });
    await added(o, "a.pdf", "1");
    o.conn.emit("offline");
    await o.c.sourceConfirmDelete(list(o)[0]?.id ?? "");
    expect(o.api.calls.some((x) => x.method === "deleteSource")).toBe(false);
    expect(o.notices).toHaveLength(1);
  });

  it("leaving 参数 stops the progress streams", async () => {
    const s = setup({ sourcePort: memPort(), delayMs: 25 });
    s.c.setTab("engine");
    await added(s, "a.pdf", "1");
    await until(() => s.c.state.sourceProgress[list(s)[0]?.id ?? ""] !== undefined);
    s.c.setTab("today");
    const seen = JSON.stringify(s.c.state.sourceProgress);
    await new Promise((r) => setTimeout(r, 150));
    expect(JSON.stringify(s.c.state.sourceProgress)).toBe(seen);
    expect(originalsFolder("EvoWiki")).toBe("EvoWiki/资料");
  });
});

describe("SourcesController for 学点新的 · 上传附件 (one upload path)", () => {
  it("sourceAttach: uploaded and saved once under <notes>/资料 like 从电脑里选, listed in 资料, no 已加入 notice", async () => {
    const port = memPort();
    const s = setup({ sourcePort: port });
    const out = await s.c.sourceAttach(pdf("讲义.pdf", "AAA"));
    expect(out.origin).toBe("EvoWiki/资料/讲义.pdf");
    expect(Object.keys(port.written)).toEqual(["EvoWiki/资料/讲义.pdf"]);
    expect(list(s).map((x) => x.id)).toEqual([out.id]);
    expect(s.notices).toEqual([]);
  });

  it("sourceAttach rejects with the reason: an unsupported file, the demo, offline (nothing uploaded)", async () => {
    const s = setup({ sourcePort: memPort() });
    await expect(s.c.sourceAttach(pdf("c.exe", "x"))).rejects.toBeInstanceOf(SourceRefused);
    const d = setup({ demo: true, sourcePort: memPort() });
    await expect(d.c.sourceAttach(pdf("a.pdf", "1"))).rejects.toThrow(new SourceBlocked(SOURCES_COPY.blockedDemo));
    expect(d.api.calls.some((x) => x.method === "uploadSource")).toBe(false);
    expect(s.notices).toEqual([]);
  });

  it("sourceWatch: progress while reading, then the end; stop ends it; a failed import carries the server's line", async () => {
    const s = setup({ sourcePort: memPort() });
    const out = await s.c.sourceAttach(pdf("a.pdf", "1"));
    const seen: string[] = [];
    s.c.sourceWatch(out.id, (u) => { seen.push(u.status); });
    await until(() => seen.includes("ready"));
    expect(seen[0]).toBe("reading");
    expect(seen[seen.length - 1]).toBe("ready");
    s.api.__failSourceImport("读不出来");
    const bad = await s.c.sourceAttach(pdf("b.pdf", "2"));
    const ends: (string | null)[] = [];
    s.c.sourceWatch(bad.id, (u) => { if (u.status === "failed") ends.push(u.error); });
    await until(() => ends.length > 0);
    expect(ends).toEqual(["读不出来"]);
  });

  it("sourceWatch: a stream that closes without an end reads the source once instead", async () => {
    const s = setup({ sourcePort: memPort() });
    const out = await s.c.sourceAttach(pdf("a.pdf", "1"));
    // finish the import elsewhere, then make the stream close with nothing
    await new Promise<void>((r) => { void s.api.followSource(out.id, () => undefined).then(() => r()); });
    s.api.followSource = () => Promise.resolve();
    const seen: string[] = [];
    s.c.sourceWatch(out.id, (u) => { seen.push(u.status); });
    await until(() => seen.length > 0);
    expect(seen).toEqual(["ready"]);
  });

  it("目录: sourceDetail reads the sections; sourceEditSections renames / relevels and recomputes the parents", async () => {
    const s = setup({ sourcePort: memPort() });
    const out = await s.c.sourceAttach(pdf("a.pdf", "1"));
    await new Promise<void>((r) => { void s.api.followSource(out.id, () => undefined).then(() => r()); });
    const d = await s.c.sourceDetail(out.id);
    const [a, b] = d.sections.map((x) => x.id);
    const e = await s.c.sourceEditSections(out.id, [{ id: a!, depth: 0, title: "第一章" }, { id: b!, depth: 1 }]);
    expect(e.sections.slice(0, 2).map((x) => [x.title, x.depth, x.parent_id])).toEqual([["第一章", 0, null], [d.sections[1]!.title, 1, a]]);
  });
});

describe("SourcesController for 学点新的: refusal vs a state, one stream per source", () => {
  it("the server refusing the file (415) is SourceRefused; offline (before or during the upload) is SourceBlocked with the plugin's offline line", async () => {
    const s = setup({ sourcePort: memPort() });
    s.api.failNext("uploadSource", new ApiError(415, "不支持这种文件"));
    await expect(s.c.sourceAttach(pdf("a.pdf", "1"))).rejects.toBeInstanceOf(SourceRefused);
    s.api.failNext("uploadSource", new OfflineError("down"));
    await expect(s.c.sourceAttach(pdf("a.pdf", "1"))).rejects.toThrow(new SourceBlocked(WIKI_COPY.offline));
    const o = setup({ sourcePort: memPort() });
    o.conn.emit("offline");
    await expect(o.c.sourceAttach(pdf("a.pdf", "1"))).rejects.toThrow(new SourceBlocked(WIKI_COPY.offline));
    expect(o.api.calls.some((x) => x.method === "uploadSource")).toBe(false);
    expect(s.notices).toEqual([]);
  });

  it("an attach then a watch opens ONE progress stream, on 参数 and with 参数 left; leaving 参数 keeps a watched stream", async () => {
    for (const leftFirst of [false, true]) {
      const s = setup({ sourcePort: memPort(), delayMs: 5 });
      if (leftFirst) { s.c.setTab("engine"); s.c.setTab("today"); }
      const out = await s.c.sourceAttach(pdf("a.pdf", "1"));
      const seen: string[] = [];
      const stop = s.c.sourceWatch(out.id, (u) => { seen.push(u.status); });
      if (!leftFirst) { s.c.setTab("engine"); s.c.setTab("today"); } // 参数 left while watching
      await until(() => seen.includes("ready"));
      expect(s.api.calls.filter((x) => x.method === "followSource")).toHaveLength(1);
      expect(list(s)[0]?.status).toBe("ready"); // 参数's card hears the same stream
      stop();
    }
  });

  it("a watcher hears usable and later progress keeps that status (学点新的's 开始 does not wait for the summaries)", async () => {
    const s = setup({ sourcePort: memPort() });
    let emit: (e: Parameters<Parameters<typeof s.api.followSource>[1]>[0]) => void = () => undefined;
    s.api.followSource = (_id, on) => { emit = on; return new Promise<void>(() => undefined); };
    const out = await s.c.sourceAttach(pdf("a.pdf", "1"));
    const seen: string[] = [];
    s.c.sourceWatch(out.id, (u) => { seen.push(u.status); });
    await until(() => s.api.followSource !== undefined);
    emit({ event: "usable", data: { title: null, status: "ready", pages: 2, sections: 2, unreadable_pages: [], note: null } });
    emit({ event: "progress", data: { stage: "summary", done: 1, total: 2, message: "" } });
    expect(seen).toEqual(["ready", "ready"]);
  });

  it("a source already read is told at once, no stream", async () => {
    const s = setup({ sourcePort: memPort() });
    const out = await s.c.sourceAttach(pdf("a.pdf", "1"));
    await until(() => list(s)[0]?.status === "ready");
    const n = s.api.calls.filter((x) => x.method === "followSource").length;
    const seen: string[] = [];
    s.c.sourceWatch(out.id, (u) => { seen.push(u.status); });
    await until(() => seen.length > 0);
    expect(seen).toEqual(["ready"]);
    expect(s.api.calls.filter((x) => x.method === "followSource")).toHaveLength(n);
  });

  it("sourceEditSections while offline is SourceBlocked with the offline line", async () => {
    const s = setup({ sourcePort: memPort() });
    s.conn.emit("offline");
    await expect(s.c.sourceEditSections("x", [{ id: "a", depth: 1 }])).rejects.toThrow(new SourceBlocked(WIKI_COPY.offline));
  });
});

describe("SourcesController: a watched stream that keeps breaking (review fix round 2)", () => {
  it("reconnects at most twice while listSources says reading, then tells failed with the stream-lost line and stops", async () => {
    const s = setup({ sourcePort: memPort() });
    s.c.setTab("engine"); s.c.setTab("today"); // 参数 left: the upload starts no stream of its own
    const out = await s.c.sourceAttach(pdf("a.pdf", "1"));
    let streams = 0;
    s.api.followSource = () => { streams++; return Promise.reject(new Error("stream broke")); };
    const seen: [string, string | null][] = [];
    s.c.sourceWatch(out.id, (u) => { seen.push([u.status, u.error]); });
    await until(() => seen.length > 0);
    expect(seen).toEqual([["failed", SOURCES_COPY.streamLost]]);
    expect(streams).toBe(3); // the first stream + 2 reconnects
    await new Promise((r) => setTimeout(r, 30));
    expect(streams).toBe(3);
    expect(list(s)[0]?.status).toBe("reading"); // listSources still says reading
  });
});

describe("SourcesController: 真题 summaries (the card's 真题 line)", () => {
  const exam = (state: "extracting" | "done" | "failed") => ({ state, items: state === "done" ? 12 : 0, attached: 9, unattached: 3, pending: 0,
    unreadable: 0, incomplete: 0, unattached_items: [], detail: null });

  it("only a source used as past exams is read (GET /wiki/sources/{id}); re-read while extracting; a failed read keeps the line; leaving 参数 stops it", async () => {
    const s = setup({ sourcePort: memPort() });
    const a = await s.c.sourceAttach(pdf("a.pdf", "1"));
    const b = await s.c.sourceAttach(pdf("b.pdf", "2"));
    await s.api.addSourceUse(a.id, { use: "exam" });
    s.api.__setSourceExam(a.id, exam("extracting"));
    s.api.__setSourceExam(b.id, exam("done")); // no exam use: never read
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      s.c.setTab("engine");
      await vi.waitFor(() => { expect(s.c.state.sourceExam[a.id]?.state).toBe("extracting"); });
      expect(s.api.calls.filter((c) => c.method === "getSource").map((c) => c.args[0])).toEqual([a.id]);
      expect(s.c.state.sourceExam[b.id]).toBeUndefined();
      s.api.__setSourceExam(a.id, exam("done"));
      await vi.advanceTimersByTimeAsync(EXAM_POLL_MS);
      await vi.waitFor(() => { expect(s.c.state.sourceExam[a.id]?.state).toBe("done"); });
      // done: no further reads planned
      const reads = s.api.calls.filter((c) => c.method === "getSource").length;
      await vi.advanceTimersByTimeAsync(EXAM_POLL_MS * 3);
      expect(s.api.calls.filter((c) => c.method === "getSource").length).toBe(reads);
      // a failed read keeps the summary shown
      s.api.getSource = () => Promise.reject(new Error("offline"));
      const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
      await s.c.sourcesReload();
      await vi.advanceTimersByTimeAsync(0);
      expect(s.c.state.sourceExam[a.id]?.state).toBe("done");
      warn.mockRestore();
    } finally { vi.useRealTimers(); }
  });

  it("closing the wiki view (the controller lives on, tab still 参数) stops the poll; reopening reads the summaries again", async () => {
    const s = setup({ sourcePort: memPort() });
    const a = await s.c.sourceAttach(pdf("a.pdf", "1"));
    await s.api.addSourceUse(a.id, { use: "exam" });
    s.api.__setSourceExam(a.id, exam("extracting"));
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      s.c.setTab("engine");
      await vi.waitFor(() => { expect(s.c.state.sourceExam[a.id]?.state).toBe("extracting"); });
      s.c.viewClosed();
      expect(s.c.state.tab).toBe("engine");
      const reads = () => s.api.calls.filter((c) => c.method === "getSource").length;
      const before = reads();
      await vi.advanceTimersByTimeAsync(EXAM_POLL_MS * 3);
      expect(reads()).toBe(before);
      s.api.__setSourceExam(a.id, exam("done"));
      s.c.viewOpened();
      await vi.waitFor(() => { expect(s.c.state.sourceExam[a.id]?.state).toBe("done"); });
      expect(reads()).toBe(before + 1);
    } finally { vi.useRealTimers(); }
  });

  it("an extracting summary is not read again once 参数 is left", async () => {
    const s = setup({ sourcePort: memPort() });
    const a = await s.c.sourceAttach(pdf("a.pdf", "1"));
    await s.api.addSourceUse(a.id, { use: "exam" });
    s.api.__setSourceExam(a.id, exam("extracting"));
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      s.c.setTab("engine");
      await vi.waitFor(() => { expect(s.c.state.sourceExam[a.id]?.state).toBe("extracting"); });
      s.c.setTab("today");
      const reads = s.api.calls.filter((c) => c.method === "getSource").length;
      await vi.advanceTimersByTimeAsync(EXAM_POLL_MS * 2);
      expect(s.api.calls.filter((c) => c.method === "getSource").length).toBe(reads);
    } finally { vi.useRealTimers(); }
  });
});

it("a budget stop offers explicit metered retry after settings change, and success clears its code", async () => {
  const s = setup({ sourcePort: memPort() }); s.c.setTab("engine");
  let raised = false;
  s.api.followSource = (_id, on) => {
    if (!raised) on({ event: "error", data: { code: "budget_exceeded", detail: "Synthetic QA budget limit" } });
    else on({ event: "done", data: { title: "QA", status: "ready", pages: 1, sections: 1, unreadable_pages: [], note: null } });
    return Promise.resolve();
  };
  const retry = vi.spyOn(s.api, "retrySource");
  const out = await s.c.sourceAttach(pdf("qa.pdf", "1"));
  await until(() => list(s)[0]?.error_code === "budget_exceeded");
  expect(list(s)[0]?.can_retry).toBe(true); expect(retry).not.toHaveBeenCalled();
  // The actual settings write is server-owned; the next explicit request uses its normal meter.
  raised = true; s.c.sourceRetryImport(out.id);
  await until(() => list(s)[0]?.status === "ready");
  expect(retry).toHaveBeenCalledOnce(); expect(list(s)[0]?.error_code).toBeNull();
  s.c.dispose();
});
