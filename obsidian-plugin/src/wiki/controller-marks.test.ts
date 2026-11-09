/** 「改了」 markers (touched): only this visit's feedback, patches and lazy updates; web PageView semantics. */
import { describe, expect, it } from "vitest";
import type { StreamOpts } from "./api";
import { firstPage, newGoal, setup, view } from "./controller-test-kit";
import { FakeWikiApi } from "./fake";
import { PCA_GOAL } from "./fixtures";
import type { ChangeOut, DeclareOut, Declared, FeedbackOut, OrganizeEvent, PageOut } from "./schema";

const change = (part: Partial<ChangeOut> = {}): ChangeOut => ({
  rewritten: [], inserted: [], moved: [], folded: [], unfolded: [], llm_calls: 0, other_pages_stale: [],
  unresolved_conflicts: [], conflicts: [], budget_exceeded: false, ...part,
});

/** declare returns a scripted ChangeOut (set `next` before declaring). */
class ScriptedDeclare extends FakeWikiApi {
  next: ChangeOut = change();
  override async declare(cell: string, v: Declared | null): Promise<DeclareOut> {
    const r = await super.declare(cell, v);
    return { ...r, change: this.next };
  }
}

describe("touched: generation and reorganize", () => {
  it("first generation: no section carries a marker", async () => {
    const { c } = setup();
    await firstPage(c);
    expect(view(c).touched).toEqual({});
  });

  it("reorganize clears earlier marks and adds none, even though the backend says every section was rewritten", async () => {
    const { c } = setup();
    await firstPage(c);
    await c.know("s3");
    expect(view(c).touched).toEqual({ s3: "folded" });
    await c.organize();
    expect(view(c).page.sections.some((x) => x.changed === "rewritten")).toBe(true);
    expect(view(c).touched).toEqual({});
  });

  it("a cached section_done adds no marker", async () => {
    class Cached extends FakeWikiApi {
      override organize(pageId: string, on: (e: OrganizeEvent) => void, o?: StreamOpts): Promise<void> {
        return super.organize(pageId, (e) => on(e.event === "section_done" ? { ...e, data: { ...e.data, cached: true } } : e), o);
      }
    }
    const { c } = setup({ api: new Cached() });
    await firstPage(c);
    await c.organize();
    expect(view(c).touched).toEqual({});
  });
});

describe("touched: actions on the current page", () => {
  it("我懂 on a foldable section → 已折叠 on it only", async () => {
    const { c } = setup();
    await firstPage(c);
    await c.know("s3");
    expect(view(c).touched).toEqual({ s3: "folded" });
  });

  it("我懂: a folded section the ChangeOut does not list is still 已折叠", async () => {
    class Quiet extends FakeWikiApi {
      override async feedbackKnow(pageId: string, sid: string): Promise<FeedbackOut> {
        const r = await super.feedbackKnow(pageId, sid);
        return { ...r, change: change() };
      }
    }
    const { c } = setup({ api: new Quiet() });
    await firstPage(c);
    await c.know("s3");
    expect(view(c).touched).toEqual({ s3: "folded" });
  });

  it("我懂 on a short section (nothing changed) → no marker", async () => {
    const { c } = setup();
    await firstPage(c);
    await c.know("s1");
    expect(view(c).touched).toEqual({});
  });

  it("a ChangeOut's inserted → 新加, moved → 改了; marks add up over the visit", async () => {
    const api = new ScriptedDeclare();
    const { c } = setup({ api });
    await firstPage(c);
    await c.know("s3");
    api.next = change({ inserted: ["s2"], moved: ["s1"] });
    await c.declareCell("c1.interpret", "know");
    expect(view(c).touched).toEqual({ s3: "folded", s2: "inserted", s1: "rewritten" });
  });

  it("moved then rewritten / inserted on the same section: the later list wins", async () => {
    const api = new ScriptedDeclare();
    const { c } = setup({ api });
    await firstPage(c);
    api.next = change({ moved: ["s1", "s2"], inserted: ["s2"] });
    await c.declareCell("c1.interpret", "know");
    expect(view(c).touched).toEqual({ s1: "rewritten", s2: "inserted" });
  });

  it("确认题 with a ChangeOut folding a section → that section is marked", async () => {
    class CheckFolds extends FakeWikiApi {
      override async answerCheck(pageId: string, sid: string, choice: number) {
        const r = await super.answerCheck(pageId, sid, choice);
        return { ...r, change: change({ folded: ["s4"] }) };
      }
    }
    const { c } = setup({ api: new CheckFolds() });
    await firstPage(c);
    await c.answerCheck("s5", 0);
    expect(view(c).touched).toEqual({ s4: "folded" });
  });

  it("a declaration while a page is open marks that page's sections; none open → nothing to mark", async () => {
    class DeclareFolds extends FakeWikiApi {
      override async declare(cell: string, v: Declared | null): Promise<DeclareOut> {
        const r = await super.declare(cell, v);
        return { ...r, change: change({ folded: ["s4"] }) };
      }
    }
    const { c } = setup({ api: new DeclareFolds() });
    await c.declareCell("c1.interpret", "know");
    expect(c.state.page.kind).toBe("none");
    await firstPage(c);
    await c.declareCell("c1.interpret", "know");
    expect(view(c).touched).toEqual({ s4: "folded" });
  });

  it("钉住 and 折叠 buttons add no marker", async () => {
    const { c } = setup();
    await firstPage(c);
    await c.togglePin("s2");
    await c.toggleFold("s3");
    await c.toggleFold("s3");
    expect(view(c).touched).toEqual({});
  });
});

describe("touched: page GETs", () => {
  /** openPage returns a scripted diff for `pid` once. */
  class Diffing extends FakeWikiApi {
    diffFor: string | null = null;
    override async openPage(pageId: string): Promise<PageOut> {
      const p = await super.openPage(pageId);
      if (pageId !== this.diffFor) return p;
      this.diffFor = null;
      return { ...p, diff: { cell_changes: [], patched: ["s4"], folded: ["s3"], unfolded: [] } };
    }
  }

  it("opening a page with a diff: patched → 改了, folded → 已折叠", async () => {
    const api = new Diffing();
    const { c } = setup({ api });
    await firstPage(c);
    await newGoal(c, PCA_GOAL);
    api.diffFor = "p1";
    await c.openWikiPage("p1");
    expect(view(c).touched).toEqual({ s4: "rewritten", s3: "folded" });
  });

  it("switching to another page clears the marks; a same-page refresh keeps them and adds its diff", async () => {
    const api = new Diffing();
    const { c } = setup({ api });
    await firstPage(c);
    await c.know("s3");
    await newGoal(c, PCA_GOAL); // new page p2: no marks
    expect(view(c).page.page_id).toBe("p2");
    expect(view(c).touched).toEqual({});
    await c.openWikiPage("p1"); // back: a fresh visit
    expect(view(c).touched).toEqual({});
    await c.know("s4"); // s4 is explain → folded
    api.diffFor = "p1";
    await c.declareCell("c1.interpret", "know"); // its refresh GET carries the diff
    expect(view(c).touched).toEqual({ s4: "rewritten", s3: "folded" });
  });
});
