/**
 * Task 18: the reading assistant's accepted 加入页面 → WikiController.pageEditedElsewhere (re-read the page, update its
 * wiki note), and the controller ruling that an unknown section heading is 另一页上的一段, never the section id.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/errors";
import { WIKI_COPY } from "./copy";
import { firstPage, flush, methods, newGoal, setup, type Setup, until, view } from "./controller-test-kit";
import type { WikiController } from "./controller";
import { topLine } from "./end-block/model";
import type { FeedbackOut, PageOut } from "./schema";

const EDITED = "陪读助手加进来的一段话。";
const PCA_GOAL = "学会 PCA 主成分分析";

/** From now on the server's copy of `pageId` has section s1's body replaced (what an accepted 加入页面 does). */
function editOnServer(s: Setup, pageId: string, body = EDITED): void {
  const edit = (id: string, p: PageOut): PageOut => (id !== pageId ? p
    : { ...p, sections: p.sections.map((x) => (x.id === "s1" ? { ...x, body } : x)) });
  const get = s.api.openPage.bind(s.api);
  const peek = s.api.getPage.bind(s.api);
  vi.spyOn(s.api, "openPage").mockImplementation(async (id: string, o?: { rewrite?: boolean }): Promise<PageOut> => edit(id, await get(id, o)));
  vi.spyOn(s.api, "getPage").mockImplementation(async (id: string): Promise<PageOut> => edit(id, await peek(id)));
}
async function twoPages(c: WikiController): Promise<void> {
  await firstPage(c);
  await newGoal(c, PCA_GOAL);
}
const since = (s: Setup, n: number): string[] => methods(s.api).slice(n);
const last = <T>(xs: readonly T[]): T | undefined => xs[xs.length - 1];

afterEach(() => { vi.restoreAllMocks(); });

describe("pageEditedElsewhere", () => {
  it("the shown page: one GET, the view shows the edit, one sync of the edited page with its result on the sync line", async () => {
    const s = setup();
    await firstPage(s.c);
    const runs = s.sync.runs.length;
    const n = s.api.calls.length;
    editOnServer(s, "p1");
    await s.c.pageEditedElsewhere("p1");
    expect(since(s, n)).toEqual(["getPage"]);
    expect(view(s.c).page.sections.find((x) => x.id === "s1")?.body).toBe(EDITED);
    expect(s.sync.runs.length).toBe(runs + 1);
    expect(last(s.sync.runs)?.pageId).toBe("p1");
    expect(last(s.sync.runs)?.sections.find((x) => x.id === "s1")?.body).toBe(EDITED);
    expect(view(s.c).noteSync).toEqual({ kind: "done", result: "created" });
  });

  it("a page not shown: one GET of it + one sync; the shown page's view is unchanged", async () => {
    const s = setup();
    await twoPages(s.c); // p2 shown
    const before = view(s.c);
    const runs = s.sync.runs.length;
    const n = s.api.calls.length;
    editOnServer(s, "p1");
    await s.c.pageEditedElsewhere("p1");
    expect(s.api.calls.slice(n).map((x) => [x.method, x.args[0]])).toEqual([["getPage", "p1"]]);
    expect(s.sync.runs.length).toBe(runs + 1);
    expect(last(s.sync.runs)?.pageId).toBe("p1");
    expect(last(s.sync.runs)?.sections.find((x) => x.id === "s1")?.body).toBe(EDITED);
    expect(view(s.c)).toBe(before);
  });

  it("the same content as the last sync this session → GET, but the note is not written again", async () => {
    const s = setup();
    await firstPage(s.c);
    editOnServer(s, "p1");
    await s.c.pageEditedElsewhere("p1");
    const runs = s.sync.runs.length;
    const n = s.api.calls.length;
    await s.c.pageEditedElsewhere("p1");
    expect(since(s, n)).toEqual(["getPage"]);
    expect(s.sync.runs.length).toBe(runs);
  });

  it("an unchanged page that was already synced (A3 change condition skipped, dedupe kept) → no write", async () => {
    const s = setup();
    await firstPage(s.c); // the organize already synced p1 as it is
    const runs = s.sync.runs.length;
    await s.c.pageEditedElsewhere("p1");
    expect(s.sync.runs.length).toBe(runs);
  });

  it("demo (sync off) → zero requests, nothing written", async () => {
    const s = setup({ demo: true });
    await firstPage(s.c);
    const n = s.api.calls.length;
    await s.c.pageEditedElsewhere("p1");
    expect(s.api.calls.length).toBe(n);
    expect(s.sync.runs).toEqual([]);
  });

  it("unconfigured → zero requests", async () => {
    const s = setup({ configured: false });
    await s.c.pageEditedElsewhere("p1");
    expect(s.api.calls).toEqual([]);
  });

  it("GET fails → console.warn only: no retry, no status line, no sync, the view untouched", async () => {
    const s = setup();
    await firstPage(s.c);
    const before = view(s.c);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const runs = s.sync.runs.length;
    const n = s.api.calls.length;
    s.api.failNext("getPage", new ApiError(500, "x"));
    await s.c.pageEditedElsewhere("p1");
    expect(since(s, n)).toEqual(["getPage"]);
    expect(warn).toHaveBeenCalledOnce();
    expect(s.sync.runs.length).toBe(runs);
    expect(view(s.c)).toBe(before);
  });

  it("a page not shown whose sync throws → console.warn, no retry", async () => {
    const s = setup();
    await twoPages(s.c);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    editOnServer(s, "p1");
    s.sync.throwNext = true;
    const runs = s.sync.runs.length;
    await s.c.pageEditedElsewhere("p1");
    expect(s.sync.runs.length).toBe(runs + 1);
    expect(warn).toHaveBeenCalledOnce();
  });

  it("busy ruling: while a write is in flight nothing is sent; the GET follows once it is over, then lands + syncs", async () => {
    const s = setup({ delayMs: 15 });
    await firstPage(s.c);
    const p = s.c.organize();
    await until(() => s.c.state.busy?.abortable === true);
    editOnServer(s, "p1");
    const n = s.api.calls.length;
    const edited = s.c.pageEditedElsewhere("p1");
    await new Promise((r) => setTimeout(r, 5));
    expect(s.api.calls.length).toBe(n); // nothing sent during the stream
    await p;
    await edited;
    expect(last(since(s, n))).toBe("getPage");
    expect(s.c.state.busy).toBeNull();
    expect(view(s.c).page.sections.find((x) => x.id === "s1")?.body).toBe(EDITED);
    expect(last(s.sync.runs)?.sections.find((x) => x.id === "s1")?.body).toBe(EDITED);
  });

  it("a write starting while the GET is out → that GET is dropped (not landed, not synced); the page waits in 「待同步」", async () => {
    const s = setup();
    await firstPage(s.c);
    editOnServer(s, "p1");
    const read = s.api.getPage.bind(s.api); // the edited server copy
    let release = (): void => undefined;
    const gate = new Promise<void>((r) => { release = r; });
    let out = false;
    vi.spyOn(s.api, "getPage").mockImplementationOnce(async (id: string) => { out = true; await gate; return read(id); });
    const runs = s.sync.runs.length;
    const p = s.c.pageEditedElsewhere("p1");
    await until(() => out);
    const pin = s.c.togglePin("s3"); // a write starts while the GET is out (pin: no GET, no sync of its own)
    release();
    await p;
    await pin;
    expect(s.sync.runs.length).toBe(runs);
    expect(view(s.c).page.sections.find((x) => x.id === "s1")?.body).not.toBe(EDITED);
    await s.c.openWikiPage("p1"); // a plain open: synced only because the page waits in 「待同步」
    expect(s.sync.runs.length).toBe(runs + 1);
    expect(last(s.sync.runs)?.sections.find((x) => x.id === "s1")?.body).toBe(EDITED);
  });
});

describe("pageEditedElsewhere fix round 1", () => {
  /** Starts 钉住 on s3 with its POST held until the returned release() is called (busy meanwhile). */
  function heldPin(s: Setup): { pin: Promise<void>; release: () => void } {
    const orig = s.api.pin.bind(s.api);
    let release = (): void => undefined;
    const gate = new Promise<void>((r) => { release = r; });
    vi.spyOn(s.api, "pin").mockImplementationOnce(async (pageId: string, sid: string, pinned: boolean) => {
      await gate;
      return orig(pageId, sid, pinned);
    });
    return { pin: s.c.togglePin("s3"), release };
  }

  it("a write beginning right as busy clears (before the waiter runs) still delays the GET until it is over", async () => {
    const s = setup();
    await firstPage(s.c);
    editOnServer(s, "p1");
    const idleAtGet: boolean[] = [];
    const read = s.api.getPage.bind(s.api);
    vi.spyOn(s.api, "getPage").mockImplementation((id: string) => { idleAtGet.push(s.c.state.busy === null); return read(id); });
    const held = heldPin(s);
    const edited = s.c.pageEditedElsewhere("p1");
    const started: Promise<void>[] = [];
    const unsub = s.c.subscribe(() => {
      // synchronously inside the set() that cleared busy: the waiter's continuation has not run yet
      if (started.length === 0 && s.c.state.busy === null) { unsub(); started.push(s.c.know("s3")); }
    });
    held.release();
    await held.pin;
    await edited;
    await Promise.all(started);
    expect(started).toHaveLength(1);
    // know's refresh GET ran under busy; the re-read is the only GET sent while idle, and it came last
    expect(idleAtGet.filter(Boolean)).toHaveLength(1);
    expect(last(idleAtGet)).toBe(true);
    expect(view(s.c).page.sections.find((x) => x.id === "s1")?.body).toBe(EDITED);
  });

  it("two quick calls for the same page → only the latest call's GET is used (one land, one sync)", async () => {
    const s = setup();
    await firstPage(s.c);
    editOnServer(s, "p1");
    const read = s.api.getPage.bind(s.api);
    let release = (): void => undefined;
    const gate = new Promise<void>((r) => { release = r; });
    // the first call's GET answers last, with older content
    vi.spyOn(s.api, "getPage").mockImplementationOnce(async (id: string) => {
      const p = await read(id);
      await gate;
      return { ...p, sections: p.sections.map((x) => (x.id === "s1" ? { ...x, body: "旧的一版" } : x)) };
    });
    const runs = s.sync.runs.length;
    const first = s.c.pageEditedElsewhere("p1");
    await s.c.pageEditedElsewhere("p1");
    release();
    await first;
    expect(s.sync.runs.length).toBe(runs + 1);
    expect(last(s.sync.runs)?.sections.find((x) => x.id === "s1")?.body).toBe(EDITED);
    expect(view(s.c).page.sections.find((x) => x.id === "s1")?.body).toBe(EDITED);
  });

  it("dispose while waiting for a write → the call resolves and nothing is sent", async () => {
    const s = setup();
    await firstPage(s.c);
    const held = heldPin(s);
    const n = s.api.calls.length; // pin recorded only after its gate (held), so nothing yet
    const edited = s.c.pageEditedElsewhere("p1");
    s.c.dispose();
    await edited;
    held.release();
    await held.pin;
    expect(methods(s.api).slice(n).filter((m) => m === "getPage")).toEqual([]);
  });
});

describe("unknown section headings in result lines (controller ruling)", () => {
  it("a ChangeOut naming a section the shown page does not have → 另一页上的一段, never the id", async () => {
    const s = setup();
    await firstPage(s.c);
    const orig = s.api.feedbackKnow.bind(s.api);
    vi.spyOn(s.api, "feedbackKnow").mockImplementation(async (pageId: string, sid: string): Promise<FeedbackOut> => {
      const out = await orig(pageId, sid);
      return { ...out, change: { ...out.change, folded: ["zz-other-9"] } };
    });
    await s.c.know("s3");
    const text = view(s.c).sectionNotes.s3?.text ?? "";
    expect(text).toContain(`折叠了${WIKI_COPY.otherPageSection}`);
    expect(text).not.toContain("zz-other-9");
  });
});

describe("a hand edit goes up before the sync writes the note", () => {
  const NOTE = "EvoWiki/Wiki/p1.md";
  const EDIT = { sections: [{ section_id: "s1", body: "我改的。", note_body: "我改的。" }], removed: [], summary: "【公式】\n删：a\n加：b" };
  const sends = (s: Setup) => s.api.calls.filter((x) => x.method === "handEdit");
  const settle = async (): Promise<void> => { for (let i = 0; i < 5; i++) await flush(); };

  it("an edited note → POST hand-edit, the sync writes the page the server answered over exactly that text", async () => {
    const s = setup();
    s.sync.hand = { path: NOTE, text: "the note", plan: { kind: "ok", edit: EDIT } };
    await firstPage(s.c);
    await until(() => sends(s).length === 1);
    await until(() => s.sync.accepted.includes("the note"));
    expect(sends(s)[0]!.args).toEqual(["p1", EDIT]);
    const run = s.sync.runs[s.sync.accepted.indexOf("the note")]!;
    expect(run.sections.find((x) => x.id === "s1")).toMatchObject({ body: "我改的。", noteBody: "我改的。" });
    expect(view(s.c).page.sections.find((x) => x.id === "s1")).toMatchObject({ body: "我改的。", pinned: true });
    expect(s.notices).toEqual([]);
  });

  it("the top line says so until he leaves the page; the lock stays", async () => {
    const s = setup();
    s.sync.hand = { path: NOTE, text: "the note", plan: { kind: "ok", edit: EDIT } };
    vi.spyOn(s.api, "handEdit").mockImplementation(async (id: string) => {
      const page = await s.api.getPage(id);
      return { edit_id: "he1", page: { ...page, hand_edit: { edit_id: "he1", at: "2026-11-04T00:00:00Z", summary: "已写入你的改动：「公式」", sections: ["s1"], can_undo: true } } };
    });
    await firstPage(s.c);
    await until(() => s.c.endBlockState("p1").handLine);
    expect(topLine(s.c.state, "p1")).toMatchObject({ kind: "hand-edited", text: "已写入你的改动：「公式」", canUndo: true });
    s.sync.hand = null;
    await s.c.noteLeft(NOTE, "p1");
    expect(s.c.endBlockState("p1").handLine).toBe(false);
    expect(topLine(s.c.state, "p1")).toBeNull();
    expect(view(s.c).page.hand_edit?.edit_id).toBe("he1");
  });

  it("while the user is in the note nothing goes up and the note is left alone", async () => {
    const s = setup();
    s.sync.hand = { path: NOTE, text: "the note", plan: { kind: "ok", edit: EDIT } };
    s.sync.openPath = NOTE;
    await firstPage(s.c);
    await settle();
    expect(sends(s)).toEqual([]);
    expect(s.sync.accepted.every((x) => x === undefined)).toBe(true);
  });

  it("noteLeft: an edited note is read fresh and goes up; an unedited one costs no request", async () => {
    const s = setup();
    await firstPage(s.c);
    await settle();
    const before = s.api.calls.length;
    await s.c.noteLeft(NOTE, "p1");
    expect(s.api.calls.length).toBe(before);
    s.sync.hand = { path: NOTE, text: "the note", plan: { kind: "ok", edit: EDIT } };
    await s.c.noteLeft(NOTE, "p1");
    await until(() => sends(s).length === 1);
    expect(s.sync.editedAsked).toEqual([[NOTE, "p1"], [NOTE, "p1"]]);
  });

  it("an edit that cannot go up safely: nothing is sent, the note stays, the top line and a notice say so once; a fixed note clears it", async () => {
    const s = setup();
    s.sync.hand = { path: NOTE, text: "the note", plan: { kind: "unsafe", why: "block", label: "公式" } };
    await firstPage(s.c);
    await until(() => s.notices.length === 1);
    const said = WIKI_COPY.handEditUnsafe("公式", "block");
    expect(s.notices).toEqual([said]);
    expect(s.c.endBlockState("p1").handFailed).toEqual({ text: said, note: "the note" });
    expect(topLine(s.c.state, "p1")).toEqual({ kind: "hand-failed", text: said, note: "the note" });
    expect(sends(s)).toEqual([]);
    expect(s.sync.accepted.every((x) => x === undefined)).toBe(true);
    editOnServer(s, "p1");
    await s.c.pageEditedElsewhere("p1");
    await settle();
    expect(s.notices).toHaveLength(1);
    s.sync.hand = { path: NOTE, text: "the fixed note", plan: { kind: "ok", edit: EDIT } };
    await s.c.noteLeft(NOTE, "p1");
    await until(() => s.c.endBlockState("p1").handFailed === null);
  });

  it("the server refuses (409): the same line with its reason; the note stays", async () => {
    const s = setup();
    s.sync.hand = { path: NOTE, text: "the note", plan: { kind: "ok", edit: EDIT } };
    s.api.failNext("handEdit", new ApiError(409, "这一段已经不在这一页了"));
    await firstPage(s.c);
    await until(() => s.notices.length === 1);
    expect(s.notices[0]).toContain("这一段已经不在这一页了");
    expect(s.sync.accepted.every((x) => x === undefined)).toBe(true);
  });

  it("a network failure: only console.warn; the next trigger tries again", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const s = setup();
    s.sync.hand = { path: NOTE, text: "the note", plan: { kind: "ok", edit: EDIT } };
    s.api.failNext("handEdit", new ApiError(500, "server detail"));
    await firstPage(s.c);
    await until(() => warn.mock.calls.some((c) => String(c[0]).includes("hand edit")));
    expect(s.notices).toEqual([]);
    await s.c.noteLeft(NOTE, "p1");
    await until(() => sends(s).length === 2);
  });

  it("offline, unconfigured or the demo: nothing is read or sent", async () => {
    const s = setup();
    await firstPage(s.c);
    await settle();
    s.sync.hand = { path: NOTE, text: "the note", plan: { kind: "ok", edit: EDIT } };
    const reads = s.sync.handReads.length;
    for (const o of [{ configured: () => false }, { demo: true }]) {
      const deps = { ...(s.c as unknown as { deps: Record<string, unknown> }).deps, ...o };
      const c2 = new (s.c.constructor as new (d: unknown) => WikiController)(deps);
      await c2.noteLeft(NOTE, "p1");
      c2.dispose();
    }
    s.conn.emit("offline");
    await s.c.noteLeft(NOTE, "p1");
    expect(s.sync.handReads).toHaveLength(reads);
    expect(sends(s)).toEqual([]);
  });

  it("no edit: the note is written as usual, its text counts as accepted (only blank lines may differ)", async () => {
    const s = setup();
    s.sync.hand = { path: NOTE, text: "the note", plan: { kind: "none" } };
    await firstPage(s.c);
    await settle();
    expect(sends(s)).toEqual([]);
    expect(s.sync.accepted).toContain("the note");
  });

  it("恢复 EvoWiki 的版本: the note is written from the page over the text the line read", async () => {
    const s = setup();
    s.sync.hand = { path: NOTE, text: "the note", plan: { kind: "unsafe", why: "fence", label: "公式" } };
    await firstPage(s.c);
    await until(() => s.notices.length === 1);
    await s.c.restoreNote("p1", "the note");
    expect(s.sync.accepted[s.sync.accepted.length - 1]).toBe("the note");
    expect(s.c.endBlockState("p1").handFailed).toBeNull();
  });
});

describe("Task 13: wiki files follow the graph", () => {
  it("a trashed page is synced with trashed: true (its file goes to the trash), once per session", async () => {
    const s = setup();
    await firstPage(s.c);
    const orig = s.api.getPage.bind(s.api);
    vi.spyOn(s.api, "getPage").mockImplementation(async (id: string): Promise<PageOut> => ({ ...(await orig(id)), trashed: true }));
    const runs = s.sync.runs.length;
    await s.c.pageEditedElsewhere("p1");
    expect(s.sync.runs.length).toBe(runs + 1);
    expect(last(s.sync.runs)?.trashed).toBe(true);
  });

  it("the page's folder and title go to the note input; a streamed write is flagged partial", async () => {
    const s = setup();
    await firstPage(s.c);
    const orig = s.api.getPage.bind(s.api);
    vi.spyOn(s.api, "getPage").mockImplementation(async (id: string): Promise<PageOut> => ({ ...(await orig(id)), folder: "统计", title: "均值和方差" }));
    await s.c.pageEditedElsewhere("p1", true);
    const l = last(s.sync.streamed);
    expect(l?.folder).toBe("统计");
    expect(l?.title).toBe("均值和方差");
  });
});
