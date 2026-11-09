import { describe, expect, it } from "vitest";
import { ApiError } from "../api/errors";
import { WIKI_COPY } from "./copy";
import { firstPage, flush, methods, setup, until, view } from "./controller-test-kit";
import { FakeWikiApi } from "./fake";
import type { PageOut } from "./schema";

/** p1 headings (fixtures OUTLINE_P1). */
const H3 = "回顾：方差与标准差";
const H4 = "z = ±1 是什么意思";

/** Calls made after `n`. */
const since = (api: FakeWikiApi, n: number): string[] => methods(api).slice(n);

describe("know", () => {
  it("foldable section: POST feedback → note under that section → GET page + catalog → sync", async () => {
    const { api, sync, c } = setup();
    await firstPage(c);
    const n = api.calls.length;
    const status = view(c).status;
    await c.know("s3");
    expect(since(api, n)).toEqual(["feedbackKnow", "openPage", "catalog", "kGrid"]);
    expect(api.calls[n]?.args).toEqual(["p1", "s3", expect.stringMatching(/^art_[0-9a-f]{24}$/) as string]);
    const v = view(c);
    expect(v.sectionNotes).toEqual({
      s3: { text: `已折叠「${H3}」，全文还在折叠里（改了 1 段）`, tone: "ok" },
    });
    expect(v.status).toEqual(status);
    expect(v.page.sections.find((s) => s.id === "s3")?.folded).toBe(true);
    expect(sync.runs).toHaveLength(2);
    expect(sync.runs[1]?.sections.find((s) => s.id === "s3")?.folded).toBe(true);
    expect(c.state.busy).toBeNull();
  });

  it("short section: knowShort, nothing changed → no sync; K loaded → GET K too", async () => {
    const { api, sync, c } = setup();
    await firstPage(c);
    c.setTab("k");
    await until(() => c.state.k.kind === "ready");
    c.setTab("page");
    const n = api.calls.length;
    await c.know("s1");
    expect(since(api, n)).toEqual(["feedbackKnow", "openPage", "catalog", "kGrid"]);
    expect(view(c).sectionNotes.s1?.text).toBe("「为什么要标准化」本来就很短，不用折起，已标记为已掌握");
    expect(sync.runs).toHaveLength(1);
  });

  it("failure: error under the section, no GET, no retry", async () => {
    const { api, c } = setup();
    await firstPage(c);
    const n = api.calls.length;
    api.failNext("feedbackKnow", new ApiError(409, "secret"));
    await c.know("s3");
    expect(since(api, n)).toEqual(["feedbackKnow"]);
    expect(view(c).sectionNotes.s3).toEqual({ text: "页面刚被别的操作改过，请再试一次。", tone: "error" });
    expect(c.state.busy).toBeNull();
  });
});

describe("pin / fold", () => {
  it("pin: one POST, no GET, no sync; pinned → know / deeper / other send nothing; unpin", async () => {
    const { api, sync, c } = setup();
    await firstPage(c);
    const n = api.calls.length;
    await c.togglePin("s4");
    expect(since(api, n)).toEqual(["pin"]);
    expect(api.calls[n]?.args).toEqual(["p1", "s4", true]);
    expect(view(c).sectionNotes).toEqual({ s4: { text: `已锁定「${H4}」，以后不会被自动改动`, tone: "ok" } });
    expect(view(c).page.sections.find((s) => s.id === "s4")?.pinned).toBe(true);
    await c.know("s4");
    expect(since(api, n)).toEqual(["pin"]);
    await c.togglePin("s4");
    expect(since(api, n)).toEqual(["pin", "pin"]);
    expect(api.calls[n + 1]?.args).toEqual(["p1", "s4", false]);
    expect(view(c).sectionNotes.s4?.text).toBe(`已解除锁定「${H4}」`);
    expect(sync.runs).toHaveLength(1);
  });

  it("fold: one POST, no GET, syncs the locally written-back page; unfold; non-foldable sends nothing", async () => {
    const { api, sync, c } = setup();
    await firstPage(c);
    const n = api.calls.length;
    await c.toggleFold("s4");
    expect(since(api, n)).toEqual(["fold"]);
    expect(api.calls[n]?.args).toEqual(["p1", "s4", true]);
    expect(view(c).sectionNotes).toEqual({ s4: { text: `已折叠「${H4}」`, tone: "ok" } });
    expect(sync.runs).toHaveLength(2);
    expect(sync.runs[1]?.sections.find((s) => s.id === "s4")?.folded).toBe(true);
    await c.toggleFold("s4");
    expect(since(api, n)).toEqual(["fold", "fold"]);
    expect(view(c).sectionNotes.s4?.text).toBe(`已展开「${H4}」`);
    expect(sync.runs).toHaveLength(3);
    await c.toggleFold("s1");
    expect(since(api, n)).toEqual(["fold", "fold"]);
  });
});

describe("check", () => {
  it("answer: POST checks → local answer, no verdict line (user review) → GET page + catalog; again sends nothing", async () => {
    const { api, sync, c } = setup();
    await firstPage(c);
    const n = api.calls.length;
    await c.answerCheck("s5", 1);
    expect(since(api, n)).toEqual(["answerCheck", "openPage", "catalog", "kGrid"]);
    expect(api.calls[n]?.args).toEqual(["p1", "s5", 1, expect.stringMatching(/^art_[0-9a-f]{24}$/) as string]);
    const v = view(c);
    expect(v.sectionNotes).toEqual({});
    expect(v.page.sections.find((s) => s.id === "s5")?.check).toMatchObject({ answered: 1, correct_index: 1, correct: true });
    expect(sync.runs).toHaveLength(1);
    await c.answerCheck("s5", 0);
    expect(since(api, n)).toEqual(["answerCheck", "openPage", "catalog", "kGrid"]);
  });

  it("a non-check section sends nothing", async () => {
    const { api, c } = setup();
    await firstPage(c);
    const n = api.calls.length;
    await c.answerCheck("s4", 0);
    expect(api.calls.length).toBe(n);
  });
});
describe("rewrite", () => {
  it("stopPage: a page this controller does not stream (the chat's card after a reload): its run cancelled, kept as stopped", async () => {
    const { api, c } = setup();
    await firstPage(c);
    const n = api.calls.length;
    expect(await c.stopPage("pg_server")).toBe(true);
    expect(since(api, n)).toEqual(["cancelRun"]);
    expect(api.calls.find((x) => x.method === "cancelRun")?.args).toEqual(["pg_server"]);
    expect(c.writeRun("pg_server")).toBe("stopped");
  });

  it("stopPage: a 404 (the run already ended) still stops; another failure is said and nothing is stopped", async () => {
    for (const [status, stopped] of [[404, true], [500, false]] as const) {
      class Refuses extends FakeWikiApi {
        override cancelRun(pageId: string): Promise<void> { this.calls.push({ method: "cancelRun", args: [pageId] }); return Promise.reject(new ApiError(status, "x")); }
      }
      const { c } = setup({ api: new Refuses() });
      await firstPage(c);
      expect(await c.stopPage("pg_server")).toBe(stopped);
      expect(c.writeRun("pg_server")).toBe(stopped ? "stopped" : null);
    }
  });

  it("single-flight: while a write runs, every other page action sends nothing", async () => {
    const { api, c } = setup({ delayMs: 15 });
    await firstPage(c);
    const p = c.organize();
    await until(() => c.state.busy?.abortable === true);
    const n = api.calls.length;
    await Promise.all([c.know("s3"), c.toggleFold("s3"), c.togglePin("s3"),
      c.answerCheck("s5", 1), c.done(), c.expand(), c.retry({ kind: "organize" })]);
    expect(api.calls.length).toBe(n);
    await p;
  });
});

describe("done", () => {
  it("POST done → 本页已学完 on the status line; GET page + catalog", async () => {
    const { api, c } = setup();
    await firstPage(c);
    const n = api.calls.length;
    await c.done();
    expect(since(api, n)).toEqual(["done", "openPage", "catalog", "kGrid"]);
    expect(view(c).status).toEqual({ text: "本页已学完", tone: "ok" });
    expect(view(c).page.done).toBe(true);
  });
});

describe("expand", () => {
  it("only on a collapsed page; POST expand → status line → GET page + catalog → sync", async () => {
    const { api, c } = setup();
    await firstPage(c);
    let n = api.calls.length;
    await c.expand();
    expect(api.calls.length).toBe(n);
    api.__collapse("p1");
    // A fresh controller: its first view re-renders the note once (collapsed); expand syncs it again.
    const s2 = setup({ api });
    await s2.c.openWikiPage("p1"); await flush();
    expect(view(s2.c).page.collapsed).toBe(true);
    expect(s2.sync.runs.map((r) => r.collapsed)).toEqual([true]);
    n = api.calls.length;
    await s2.c.expand();
    expect(since(api, n)).toEqual(["expand", "openPage", "catalog", "kGrid"]);
    expect(view(s2.c).status?.text).toBe("已恢复成完整页；重新展开了「回顾：方差与标准差」「z = ±1 是什么意思」（改了 2 段）");
    expect(view(s2.c).page.collapsed).toBe(false);
    expect(s2.sync.runs.map((r) => r.collapsed)).toEqual([true, false]);
  });
});

describe("guards", () => {
  it("offline: every page action sends nothing and says offline where its result would go", async () => {
    const { api, conn, c } = setup();
    await firstPage(c);
    api.__collapse("p1");
    await c.openWikiPage("p1");
    conn.state = "offline";
    const n = api.calls.length;
    await c.know("s1");
    await c.toggleFold("s3");
    await c.togglePin("s4");
    await c.answerCheck("s5", 1);
    expect(api.calls.length).toBe(n);
    const off = { text: WIKI_COPY.offline, tone: "error" };
    const v = view(c);
    expect(v.sectionNotes).toEqual({ s1: off, s3: off, s4: off, s5: off });
    await c.done();
    expect(view(c).status).toEqual(off);
    await c.expand();
    expect(api.calls.length).toBe(n);
  });

  it("an unorganized page: page actions send nothing", async () => {
    const { api, c } = setup();
    const d = api.__goal("g");
    await api.answerDraft(d.draft_id ?? "", { answers: null, skip: true });
    await c.openWikiPage("p1");
    const n = api.calls.length;
    await Promise.all([c.know("s1"), c.toggleFold("s3"), c.togglePin("s1"), c.answerCheck("s5", 1), c.done(), c.expand()]);
    expect(api.calls.length).toBe(n);
  });

  it("section notes survive the refresh; notes of sections gone from the new page are dropped", async () => {
    class Dropping extends FakeWikiApi {
      drop = false;
      override async openPage(pageId: string): Promise<PageOut> {
        const p = await super.openPage(pageId);
        return this.drop ? { ...p, sections: p.sections.filter((s) => s.id !== "s1") } : p;
      }
    }
    const api = new Dropping();
    const { c } = setup({ api });
    await firstPage(c);
    await c.togglePin("s1");
    await c.togglePin("s2");
    api.drop = true;
    await c.know("s3");
    expect(Object.keys(view(c).sectionNotes).sort()).toEqual(["s2", "s3"]);
    expect(view(c).page.sections.map((s) => s.id)).not.toContain("s1");
  });
});

describe("page feedback (the 「⋯」 menu's 太浅了 / 有点难, the feedback dialog)", () => {
  it("menu 太浅了 / 有点难: one POST feedback for that section (pinned too) → Notice 记下了。; no page re-read", async () => {
    const { api, c, notices } = setup();
    await firstPage(c);
    await c.togglePin("s4");
    let n = api.calls.length;
    await c.feedback("s4", ["too_shallow"]);
    expect(since(api, n)).toEqual(["pageFeedback"]);
    expect(api.calls[n]?.args).toEqual(["p1", { chips: ["too_shallow"], text: "", section_id: "s4", apply: false, artifact_id: expect.stringMatching(/^art_[0-9a-f]{24}$/) as string }]);
    expect(notices).toEqual([WIKI_COPY.feelSaved]);
    expect(c.state.busy).toBeNull();
    n = api.calls.length;
    await c.feedback("s4", ["too_hard"]);
    expect(api.calls[n]?.args).toEqual(["p1", { chips: ["too_hard"], text: "", section_id: "s4", apply: false, artifact_id: expect.stringMatching(/^art_[0-9a-f]{24}$/) as string }]);
    expect(methods(api).some((m) => /guideline/i.test(m))).toBe(false);
  });

  it("menu feedback: offline → nothing sent, said in a Notice; 422 → plain sentence (never the detail) and the page is re-read", async () => {
    const { api, conn, c, notices } = setup();
    await firstPage(c);
    const n = api.calls.length;
    conn.emit("offline");
    await c.feedback("s4", ["too_hard"]);
    expect(api.calls.length).toBe(n);
    expect(notices).toEqual([WIKI_COPY.offline]);
    conn.emit("online");
    api.failNext("pageFeedback", new ApiError(422, "这一页没有这一段：s4"));
    await c.feedback("s4", ["too_hard"]);
    expect(since(api, n)).toEqual(["pageFeedback", "openPage"]);
    expect(notices[notices.length - 1]).toBe("这一段已经不在这一页了。");
    expect(notices.join("")).not.toContain("s4");
    // The section is gone: the note goes to the status line, not under a section that no longer exists (finding 8).
    expect(view(c).status).toEqual({ text: "这一段已经不在这一页了。", tone: "error" });
    expect(view(c).sectionNotes.s4?.text).not.toBe("这一段已经不在这一页了。");
    expect(c.state.busy).toBeNull();
  });
});
