/** Task 13 (fix round 1): files follow the graph - parent name from the backend, write end, reconcile, trash. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/errors";
import { firstPage, setup, type Setup } from "./controller-test-kit";
import type { FileOut, PageOut } from "./schema";

const last = <T>(xs: readonly T[]): T | undefined => xs[xs.length - 1];
afterEach(() => { vi.restoreAllMocks(); });

/** From now on the server's copy of every page is `edit(page)`. */
function serve(s: Setup, edit: (p: PageOut) => PageOut): void {
  const orig = s.api.getPage.bind(s.api);
  vi.spyOn(s.api, "getPage").mockImplementation(async (id: string): Promise<PageOut> => edit(await orig(id)));
}
const settle = async (): Promise<void> => { for (let n = 0; n < 12; n++) await Promise.resolve(); await new Promise((r) => setTimeout(r, 5)); };

describe("note input", () => {
  it("the parent name comes from PageOut.parent_name", async () => {
    const s = setup();
    await firstPage(s.c);
    serve(s, (p) => ({ ...p, parent_name: "食品添加剂" }));
    await s.c.pageEditedElsewhere("p1");
    expect(last(s.sync.runs)?.parentName).toBe("食品添加剂");
  });
});

describe("the end of a write", () => {
  it("keeps a final file-sync failure pending so the next attempt retries the same page", async () => {
    const s = setup();
    await firstPage(s.c);
    serve(s, (p) => ({ ...p, title: "Needs final sync" }));
    vi.spyOn(s.sync, "run").mockRejectedValueOnce(new Error("disk unavailable"));
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    expect(await s.c.pageEditedElsewhere("p1")).toBe(false);
    expect(await s.c.pageEditedElsewhere("p1")).toBe(true);
    expect(last(s.sync.runs)?.title).toBe("Needs final sync");
    s.c.dispose();
  });

  it("failed / undone on a page that holds something: a full sync takes the writing status off", async () => {
    const s = setup();
    await firstPage(s.c);
    serve(s, (p) => ({ ...p, title: "半截" }));
    await s.c.pageEditedElsewhere("p1", true);
    const runs = s.sync.runs.length;
    expect(await s.c.pageEditedElsewhere("p1")).toBe(true);
    expect(s.sync.runs.length).toBe(runs + 1);
    expect(last(s.sync.runs)?.trashed).toBeUndefined();
  });

  it("on a page that holds nothing: the file made for it goes", async () => {
    const s = setup();
    await firstPage(s.c);
    serve(s, (p) => ({ ...p, title: "半截" }));
    await s.c.pageEditedElsewhere("p1", true);
    serve(s, (p) => ({ ...p, organized: false, sections: [] }));
    await s.c.pageEditedElsewhere("p1");
    expect(last(s.sync.runs)?.trashed).toBe(true);
  });

  it("a file left writing by an earlier session (discardEmpty) goes when its page holds nothing", async () => {
    const s = setup();
    await firstPage(s.c);
    serve(s, (p) => ({ ...p, organized: false, sections: [] }));
    await s.c.pageEditedElsewhere("p1", false, true);
    expect(last(s.sync.runs)?.trashed).toBe(true);
  });

  it("a failed read says false (the watcher tries again on its next tick)", async () => {
    const s = setup();
    await firstPage(s.c);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.spyOn(s.api, "getPage").mockRejectedValue(new ApiError(503, "x"));
    expect(await s.c.pageEditedElsewhere("p1")).toBe(false);
  });
});

describe("trashed pages", () => {
  it("later reads of a trashed page do not sync again", async () => {
    const s = setup();
    await firstPage(s.c);
    serve(s, (p) => ({ ...p, trashed: true }));
    await s.c.pageEditedElsewhere("p1");
    const n = s.sync.runs.length;
    await s.c.pageEditedElsewhere("p1");
    await s.c.reconcileFiles([]);
    expect(s.sync.runs.length).toBe(n);   // the second read is deduped (lastSynced), the reconcile has no pages
  });
});

describe("reconcileFiles", () => {
  const file = (over: Partial<FileOut> = {}): FileOut => ({ page_id: "p1", title: "均值", folder: "统计", parent_name: "", trashed: false, organized: true, ...over });

  it("puts the file of every page that has one where the page belongs (placeOnly), from ONE files read, opening no page", async () => {
    const s = setup();
    await firstPage(s.c);
    s.sync.pages = ["p1"];
    s.api.filesNow = [file(), file({ page_id: "p2", title: "别的" })];
    const n = s.sync.runs.length;
    const calls = s.api.calls.length;
    expect(await s.c.reconcileFiles()).toBe(true);
    expect(s.sync.runs.length).toBe(n + 1);
    const i = last(s.sync.runs)!;
    expect([i.placeOnly, i.folder, i.title]).toEqual([true, "统计", "均值"]);
    expect(s.api.calls.slice(calls).map((x) => x.method)).toEqual(["files"]);      // no openPage, no peek
  });

  it("no note of this user in the vault yet: every written page gets its note from the read-only GET, once", async () => {
    const s = setup();
    await firstPage(s.c);
    s.sync.pages = [];
    s.api.filesNow = [file(), file({ page_id: "p2", title: "没写", organized: false }), file({ page_id: "p3", trashed: true })];
    const n = s.sync.runs.length;
    const calls = s.api.calls.length;
    expect(await s.c.reconcileFiles()).toBe(true);
    expect(s.api.calls.slice(calls).map((x) => x.method)).toEqual(["files", "getPage"]);   // read only: no openPage
    expect(s.sync.runs.slice(n).map((r) => [r.pageId, r.placeOnly ?? false])).toEqual([["p1", false]]);
    const again = s.api.calls.length;
    expect(await s.c.reconcileFiles()).toBe(true);                    // a note deleted later is not written back
    expect(s.api.calls.slice(again)).toEqual([]);
  });

  it("a trashed page's file goes (trashed + placeOnly)", async () => {
    const s = setup();
    await firstPage(s.c);
    s.sync.pages = ["p1"];
    s.api.filesNow = [file({ trashed: true })];
    await s.c.reconcileFiles();
    expect(last(s.sync.runs)?.trashed).toBe(true);
  });

  it("false when the files cannot be read or a move fails (the caller tries again later)", async () => {
    const s = setup();
    await firstPage(s.c);
    s.sync.pages = ["p1"];
    s.api.filesNow = [file()];
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    s.api.failNext("files", new ApiError(503, "x"));
    expect(await s.c.reconcileFiles()).toBe(false);
    s.sync.result = "move_failed";
    expect(await s.c.reconcileFiles()).toBe(false);
  });
});

describe("stale writing files", () => {
  it("on the first live set, a note left writing whose page is not live is brought to its page", async () => {
    const s = setup();
    await firstPage(s.c);
    (s.sync as unknown as { listWriting: () => Promise<string[]> }).listWriting = () => Promise.resolve(["p1", "p9"]);
    serve(s, (p) => ({ ...p, organized: false, sections: [] }));
    const notes = (s.c as unknown as { notes: { syncEdited(id: string, partial?: boolean, discardEmpty?: boolean, valid?: () => boolean): Promise<string>;
      clearStaleWriting(l: Set<string>, valid: (id: string) => boolean): Promise<boolean> } }).notes;
    const spy = vi.spyOn(notes, "syncEdited");
    await notes.clearStaleWriting(new Set(["p9"]), () => true);
    expect(spy.mock.calls).toEqual([["p1", false, true, expect.any(Function)]]);      // p9 is live: left alone
    expect(last(s.sync.runs)?.trashed).toBe(true);
  });
});

describe("the user's own stream and the watcher", () => {
  it("a background partial for the page the foreground stream writes is ignored; the stream start drops queued ones", async () => {
    const s = setup();
    await firstPage(s.c);
    const c = (s.c as unknown as { notes: {
      streamingPage: string | null; streamBackground(sync: unknown, id: string, p: PageOut): void;
      syncQueue: Map<string, { bg: boolean; waiters: (() => void)[] }>; dropBackgroundSync(id: string): void;
    } }).notes;
    const orig = await s.api.getPage("p1");
    const base = s.sync.streamed.length;
    c.streamingPage = "p1";
    c.streamBackground(s.sync, "p1", { ...orig, title: "后台写的" });
    await settle();
    expect(s.sync.streamed).toHaveLength(base);
    c.streamingPage = null;
    s.sync.hold = true;                                  // keep the queue from emptying
    c.streamBackground(s.sync, "p1", { ...orig, title: "后台写的" });
    c.streamBackground(s.sync, "p1", { ...orig, title: "后台写的二" });
    expect(c.syncQueue.get("p1")?.bg).toBe(true);
    c.dropBackgroundSync("p1");
    expect(c.syncQueue.has("p1")).toBe(false);
    s.sync.release();
  });
});

describe("how the foreground stream ended", () => {
  it("a stream that ends done is recorded as done (the watcher leaves its file alone)", async () => {
    const s = setup();
    await firstPage(s.c);
    expect((s.c as unknown as { notes: { foregroundResult: Map<string, boolean> } }).notes.foregroundResult.get("p1")).toBe(true);
  });
});

describe("offline rule", () => {
  it("back online the writes watcher looks at once (the files catch up); offline it asks nothing", async () => {
    const s = setup();
    const w = (s.c as unknown as { core: { watcher: { kick(): void } | null } }).core.watcher;
    expect(w).not.toBeNull();
    const kick = vi.spyOn(w!, "kick");
    s.conn.emit("offline");
    await settle();
    expect(kick).not.toHaveBeenCalled();
    s.conn.emit("online");
    await settle();
    expect(kick).toHaveBeenCalled();
    s.c.dispose();
  });
});


it("does not cache a cancelled queued sync as a successful file write", async () => {
  const s = setup();
  await firstPage(s.c);
  serve(s, (page) => ({ ...page, title: "Never written" }));
  const page = await s.api.getPage("p1");
  const notes = (s.c as unknown as { notes: { noteInput(page: PageOut): import("./note-port").WikiNoteInput;
    enqueueSync(sync: unknown, id: string, input: unknown, json: string, partial: boolean, bg: boolean, valid: () => boolean): Promise<void> } }).notes;
  const input = notes.noteInput(page);
  await notes.enqueueSync(s.sync, "p1", input, JSON.stringify(input), false, false, () => false);
  const before = s.sync.runs.length;
  expect(await s.c.pageEditedElsewhere("p1")).toBe(true);
  expect(s.sync.runs.length).toBe(before + 1);
  expect(last(s.sync.runs)?.title).toBe("Never written");
  s.c.dispose();
});
