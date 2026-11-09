/**
 * The 「读完了」 block's controller side: the pills (one choice; 内容太简单 / 理解有困难 sent to the assistant), 撤回,
 * the main button (下一页 / 学完了, a page written ahead), the path lookup, the fallback-writer line and the streamed note.
 */
import { describe, expect, it, vi } from "vitest";
import { ApiError, OfflineError } from "../api/errors";
import { firstPage, methods, newGoal, setup, type Setup, view } from "./controller-test-kit";
import { WIKI_COPY } from "./copy";
import { endModel, patchedSections, primaryOf, topLine } from "./end-block/model";
import type { PageOut, StepOut } from "./schema";
import { EMPTY_END } from "./state";

const sent = (api: Setup["api"], from = 0) => api.calls.slice(from).filter((x) => x.method === "pageFeedback").map((x) => x.args[1]);
const pid = (s: Setup): string => view(s.c).page.page_id;
async function ready(o: Parameters<typeof setup>[0] = {}): Promise<Setup> {
  const s = setup(o);
  await firstPage(s.c);
  return s;
}

describe("the pills (one choice; 内容太简单 / 理解有困难 go to the assistant)", () => {
  it("one choice: a new pick replaces the old; pressing the pressed one changes nothing", async () => {
    const { c } = await ready();
    expect(c.endBlockState("p1")).toEqual(EMPTY_END);
    c.pickEndChip("p1", "right");
    expect(c.endBlockState("p1").draft.chips).toEqual(["right"]);
    c.pickEndChip("p1", "hard");
    expect(c.endBlockState("p1").draft.chips).toEqual(["hard"]);
    c.pickEndChip("p1", "shallow");
    expect(c.endBlockState("p1").draft.chips).toEqual(["shallow"]);
  });

  it("基本合适 records one signal on the server; nothing goes to the assistant; a failure takes the pick back and says so", async () => {
    const s = await ready();
    s.c.pickEndChip("p1", "right");
    await Promise.resolve();
    expect(s.sentFresh).toEqual([]);
    expect(s.api.__signals().map((x) => [x.kind, x.page_id])).toEqual([["page_fit", "p1"]]);
    expect(s.c.endBlockState("p1").draft.chips).toEqual(["right"]);
    const t = await ready();
    t.api.failNext("pageFit", new ApiError(500, "boom"));
    t.c.pickEndChip("p1", "right");
    await vi.waitFor(() => { expect(t.c.endBlockState("p1").phase.kind).toBe("error"); });
    expect(t.c.endBlockState("p1").draft.chips).toEqual([]);
  });

  it("内容太简单 / 理解有困难 send 「这页…」 to the assistant with no quote, once per pick (no page feedback request)", async () => {
    const s = await ready();
    const title = view(s.c).page.title;
    const n = s.api.calls.length;
    s.c.pickEndChip("p1", "hard");
    s.c.pickEndChip("p1", "hard");
    s.c.pickEndChip("p1", "shallow");
    expect(s.sentFresh).toEqual([
      { pageId: "p1", title, text: "这页理解有困难" },
      { pageId: "p1", title, text: "这页内容太简单" },
    ]);
    expect(sent(s.api, n)).toEqual([]);
  });

  it("is kept per page, not per visit (leaving the page and coming back keeps it)", async () => {
    const { c } = await ready();
    c.pickEndChip("p1", "right");
    await c.openWikiPage("p1");
    expect(c.endBlockState("p1").draft.chips).toEqual(["right"]);
  });
});

describe("撤回", () => {
  /** The page carries a feedback patch (made on the server, e.g. by the assistant), read again. */
  async function patched(): Promise<Setup> {
    const s = await ready();
    await s.api.pageFeedback("p1", { chips: ["too_hard"], text: "术语没先讲", section_id: null, apply: false });
    await s.c.pageEditedElsewhere("p1");
    s.notices.length = 0;
    return s;
  }

  it("undoes the patch, re-reads the page, syncs the note, drops the line and the tags, and says so", async () => {
    const s = await patched();
    const patchedBody = view(s.c).page.sections.map((x) => x.body).join("|");
    s.sync.runs.length = 0;
    await s.c.undoPageFeedback("p1");
    expect(methods(s.api)).toContain("undoFeedback");
    expect(view(s.c).page.feedback).toBeNull();
    expect(view(s.c).page.sections.map((x) => x.body).join("|")).not.toBe(patchedBody);
    expect(topLine(s.c.state, "p1")).toBeNull();
    expect(patchedSections(s.c.state, "p1").size).toBe(0);
    expect(s.notices).toEqual([WIKI_COPY.noticeUndone]);
    expect(s.sync.runs).toHaveLength(1);
    expect(s.c.state.busy).toBeNull();
  });

  it("a failure keeps the button and says 撤回没成功，请再试一次。; a retry then works", async () => {
    const s = await patched();
    s.api.failNext("undoFeedback", new ApiError(500, "x"));
    await s.c.undoPageFeedback("p1");
    expect(s.c.endBlockState("p1").undo).toEqual({ kind: "failed", text: WIKI_COPY.topUndoFailed, retry: true });
    expect(view(s.c).page.feedback).not.toBeNull();
    expect(topLine(s.c.state, "p1")?.kind).toBe("changed");
    await s.c.undoPageFeedback("p1");
    expect(view(s.c).page.feedback).toBeNull();
    expect(s.c.endBlockState("p1").undo).toEqual({ kind: "idle" });
  });

  it("409 (the page changed since) says so and offers no retry", async () => {
    const s = await patched();
    s.api.failNext("undoFeedback", new ApiError(409, "changed"));
    await s.c.undoPageFeedback("p1");
    expect(s.c.endBlockState("p1").undo).toEqual({ kind: "failed", text: WIKI_COPY.topUndoChanged, retry: false });
  });

  it("after a 409 the page is re-read so the marker is current", async () => {
    const s = await patched();
    s.api.failNext("undoFeedback", new ApiError(409, "changed"));
    const n = s.api.calls.length;
    await s.c.undoPageFeedback("p1");
    expect(methods(s.api).slice(n)).toEqual(["undoFeedback", "openPage"]);
  });

  it("offline sends nothing", async () => {
    const s = await patched();
    s.conn.emit("offline");
    const n = s.api.calls.length;
    await s.c.undoPageFeedback("p1");
    expect(s.api.calls.length).toBe(n);
    expect(s.c.endBlockState("p1").undo.kind).toBe("failed");
  });

  it("the line survives a re-open of the page (it comes from the server's marker) until a new feedback", async () => {
    const s = await patched();
    await s.c.openWikiPage("p1");
    expect(topLine(s.c.state, "p1")?.kind).toBe("changed");
  });
});

describe("the top line's priority; the fallback writer is never shown (single writer)", () => {
  /** Every GET page answers with this writer (the fake's own otherwise). */
  function writtenBy(api: Setup["api"], writer: PageOut["writer"]): void {
    const orig = api.openPage.bind(api);
    api.openPage = async (id) => ({ ...(await orig(id)), writer }) satisfies PageOut;
  }
  /** The organize stream says the fallback took over (after the fake's own writer event). */
  function fallbackStream(api: Setup["api"], reason: string): void {
    const orig = api.organize.bind(api);
    api.organize = (id, on, o) => orig(id, (e) => {
      on(e);
      if (e.event === "writer") on({ event: "writer", data: { model: "deepseek:deepseek-v4-pro", fallback: true, reason } });
    }, o);
  }

  it("which model wrote the page is never shown: no line for the primary writer nor for the fallback (with or without a reason)", async () => {
    const s = await ready();
    expect(topLine(s.c.state, "p1")).toBeNull();
    writtenBy(s.api, { model: "deepseek:deepseek-v4-pro", at: "2026-10-10T08:00:00Z", cost_usd: 0.01, fallback_reason: "Claude 暂时不可用（429）" });
    await s.c.openWikiPage("p1");
    expect(topLine(s.c.state, "p1")).toBeNull();
    writtenBy(s.api, { model: "deepseek:deepseek-v4-pro", at: "2026-10-10T08:00:00Z", cost_usd: 0.01, fallback_reason: "" });
    await s.c.openWikiPage("p1");
    expect(topLine(s.c.state, "p1")).toBeNull();
  });

  it("the stream's writer{fallback} shows nothing either (a failed GET after it, then a read)", async () => {
    const s = setup();
    fallbackStream(s.api, "Claude 不可用");
    s.api.failNext("openPage", new OfflineError());
    await firstPage(s.c);
    expect(view(s.c).streamWriter).toEqual({ fallback: true, reason: "Claude 不可用" });
    expect(topLine(s.c.state, "p1")).toBeNull();
    // A read lands: PageOut.writer is authoritative again (the fake's primary writer: no line).
    await s.c.openWikiPage("p1");
    expect(view(s.c).streamWriter).toBeNull();
    expect(topLine(s.c.state, "p1")).toBeNull();
    s.c.dispose();
  });

  it("a fallback-written page still shows the 撤回 line after feedback", async () => {
    const s = await ready();
    writtenBy(s.api, { model: "m", at: "2026-10-10T08:00:00Z", cost_usd: 0, fallback_reason: "x" });
    await s.c.openWikiPage("p1");
    expect(topLine(s.c.state, "p1")).toBeNull();
    await s.api.pageFeedback("p1", { chips: ["too_hard"], text: "术语没先讲", section_id: null, apply: false });
    await s.c.openWikiPage("p1");
    expect(topLine(s.c.state, "p1")?.kind).toBe("changed");
  });

  it("nothing polls the page after an organize (no refine line, no timer reads)", async () => {
    vi.useFakeTimers();
    try {
      const s = setup();
      await firstPage(s.c);
      const n = s.api.calls.length;
      await vi.advanceTimersByTimeAsync(200_000);
      expect(s.api.calls.length).toBe(n);
      expect(topLine(s.c.state, "p1")).toBeNull();
      expect(Object.keys(s.c.endBlockState("p1"))).not.toContain("refine");
      s.c.dispose();
    } finally { vi.useRealTimers(); }
  });
});

describe("sections stream into the note", () => {
  it("each written section lands in the note as it comes (checks with the outline); the new page's note opens with the first", async () => {
    const s = setup();
    await firstPage(s.c);
    const all = view(s.c).page.sections.map((x) => x.id);
    const runs = s.sync.streamed.map((r) => r.sections.map((x) => x.id));
    // Streamed (partial) writes, growing section by section in page order; then one full sync of the done GET's page.
    expect(runs.length).toBeGreaterThan(1);
    expect(runs[0]!.length).toBeLessThan(all.length);
    for (let i = 1; i < runs.length; i++) expect(runs[i]!.length).toBeGreaterThan(runs[i - 1]!.length);
    for (const r of runs) expect(r).toEqual(all.filter((id) => r.includes(id)));
    expect(s.sync.runs.map((r) => r.sections.map((x) => x.id))).toEqual([all]);
    // Opened once (with the first streamed write), not again after done.
    expect(s.opened).toEqual(["Wiki/p1.md"]);
  });

  it("section_added inserts after the given section; section_delta repeats: the last one wins (heading too)", async () => {
    const s = setup();
    const orig = s.api.organize.bind(s.api);
    s.api.organize = (id, on, o) => orig(id, (e) => {
      if (e.event === "done") {
        const sec = { ...view(s.c).page.sections[0]!, id: "s9", heading: "插进来的一节", role: "explain" as const, cue: "", body: "", check: null };
        on({ event: "section_added", data: { section: sec, after: "s1" } });
        on({ event: "section_delta", data: { section_id: "s9", cue: "", body: "第一版", heading: null } });
        on({ event: "section_done", data: { section_id: "s9", cached: false } });
        on({ event: "section_delta", data: { section_id: "s9", cue: "提示", body: "第二版", heading: "改过的标题" } });
        on({ event: "section_done", data: { section_id: "s9", cached: false } });
        const ids = view(s.c).page.sections.map((x) => x.id);
        expect(ids.indexOf("s9")).toBe(ids.indexOf("s1") + 1);
        const s9 = view(s.c).page.sections.find((x) => x.id === "s9")!;
        expect([s9.heading, s9.cue, s9.body]).toEqual(["改过的标题", "提示", "第二版"]);
      }
      on(e);
    }, o);
    await firstPage(s.c);
    expect(s.sync.streamed.some((r) => r.sections.some((x) => x.id === "s9" && x.heading === "改过的标题" && x.body.includes("第二版")))).toBe(true);
  });
});

describe("the main button", () => {
  async function onPath(): Promise<Setup> {
    const s = setup();
    await newGoal(s.c, "学统计推断");
    await s.c.pathNext();
    return s;
  }

  it("names the next page of the path; 「这一页学完了」 off a path", async () => {
    const off = await ready();
    expect(endModel(off.c.state, "p1")?.primary).toEqual({ kind: "page-done", label: WIKI_COPY.endPageDone });
    const s = await onPath();
    const m = endModel(s.c.state, pid(s));
    expect(m?.primary.kind).toBe("next");
    expect(m?.primary.label).toBe(WIKI_COPY.endNext);
  });

  it("no block while the page is being written here or after its write was stopped (继续学习 is for a page read to its end)", async () => {
    const s = await ready();
    expect(endModel(s.c.state, "p1")).not.toBeNull();
    const writing = { ...s.c.state, busy: { what: "organize", abortable: true } };
    expect(endModel(writing, "p1")).toBeNull();
    const stopped = { ...s.c.state, stopped: { pageId: "p1", title: "x", goal: "" } };
    expect(endModel(stopped, "p1")).toBeNull();
  });

  it("the button follows the server's step once this page is read (PageOut.next_step): next block, goal done, none", () => {
    const step = (kind: StepOut["kind"], region_name = ""): StepOut => ({ kind, goal_id: "m1", goal: "黑壳虾", map_id: "m1", region_id: "c2",
      region_name, path_id: null, index: null, page_id: null, title: "", draft_id: null, progress: null, failure: null, review_first: null });
    expect(primaryOf(step("goal_done"))).toEqual({ kind: "goal-done", label: WIKI_COPY.endGoalDone });
    expect(primaryOf(step("enter_block", "水质与环境"))).toMatchObject({ kind: "next-block", label: "下一块：水质与环境 →" });
    expect(primaryOf(step("start_page"))).toMatchObject({ kind: "next", label: WIKI_COPY.endNext });
    expect(primaryOf(null)).toEqual({ kind: "page-done", label: WIKI_COPY.endPageDone });
  });

  it("下一页: marks this page learned (POST done), then makes and opens the next one (the step it named)", async () => {
    const s = await onPath();
    const first = pid(s);
    const n = s.api.calls.length;
    await s.c.finishPage(first);
    const ms = methods(s.api).slice(n);
    expect(ms.indexOf("done")).toBeGreaterThan(-1);
    expect(ms.indexOf("pathNext")).toBeGreaterThan(ms.indexOf("done"));
    expect(pid(s)).not.toBe(first);
    expect(s.c.state.busy).toBeNull();
  });

  it.each(["ready", "writing", "failed"] as const)(
    "下一页 with the next page written ahead (%s): always one POST organize (a ready run is replayed and consumed), the page opens", async (status) => {
      const s = await onPath();
      const first = pid(s);
      const ahead = s.api.prefetchNext(s.c.state.path.pathId ?? "", status);
      const n = s.api.calls.length;
      const streamedBefore = s.sync.streamed.length;
      const openedBefore = s.opened.length;
      await s.c.finishPage(first);
      const ms = methods(s.api).slice(n);
      expect(ms.filter((m) => m === "organize").length).toBe(1);
      expect(ms.indexOf("organize")).toBeGreaterThan(ms.indexOf("pathNext"));
      // The (replayed) sections streamed into the note, which opened with the first of them (not after done).
      expect(s.sync.streamed.length).toBeGreaterThan(streamedBefore);
      expect(s.opened.length - openedBefore).toBe(1);
      if (status === "failed") expect(pid(s)).not.toBe(ahead);
      else expect(pid(s)).toBe(ahead);
      expect(view(s.c).page.organized).toBe(true);
      expect(view(s.c).page.sections.length).toBeGreaterThan(0);
      expect(s.opened[s.opened.length - 1]).toBe(`Wiki/${pid(s)}.md`);
      expect(s.sync.runs[s.sync.runs.length - 1]?.pageId).toBe(pid(s));
      expect(s.c.state.busy).toBeNull();
      // The claimed prefetch is gone from the shown path.
      expect(s.c.state.path.view.kind === "ready" && s.c.state.path.view.value.prefetch).toBeNull();
      if (status !== "ready") return;
      // The replay consumed the run: 重新组织 afterwards writes the page anew (the fake marks a rewrite).
      await s.c.organize();
      expect(methods(s.api).filter((m) => m === "organize").length - ms.filter((m) => m === "organize").length).toBeGreaterThan(0);
      expect((await s.api.openPage(ahead)).sections.some((x) => x.changed === "rewritten")).toBe(true);
    });

  it("a failed 学完 keeps the page and puts one plain line in the block (no next page is made)", async () => {
    const s = await onPath();
    const first = pid(s);
    s.api.failNext("done", new ApiError(500, "x"));
    const n = s.api.calls.length;
    await s.c.finishPage(first);
    expect(pid(s)).toBe(first);
    expect(methods(s.api).slice(n)).not.toContain("pathNext" as never);
    expect(s.c.endBlockState(first).phase).toEqual({ kind: "error", text: WIKI_COPY.endDoneFailed });
  });

  it("off a path: the existing 这一页我学完了, then the plain line", async () => {
    const s = await ready();
    await s.c.finishPage("p1");
    expect(methods(s.api)).toContain("done");
    expect(s.c.endBlockState("p1").phase).toEqual({ kind: "saved", text: WIKI_COPY.endPageDoneLine });
  });

  it("offline: says so in the block, sends nothing", async () => {
    const s = await ready();
    s.conn.emit("offline");
    const n = s.api.calls.length;
    await s.c.finishPage("p1");
    expect(s.api.calls.length).toBe(n);
    expect(s.c.endBlockState("p1").phase).toEqual({ kind: "error", text: WIKI_COPY.offline });
  });

});
