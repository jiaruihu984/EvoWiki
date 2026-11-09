import { describe, expect, it, vi } from "vitest";
import { WIKI_COPY } from "../copy";
import { newGoal } from "../controller-test-kit";
import { createEndBlock } from "./end-block";
import { harness } from "./test-kit";
import { graphBus } from "../graph-page/bus";
import { sectionLines } from "../note/format";
import { ApiError } from "../../api/errors";
import { setUiLanguage } from "../../i18n/index";

const q = <T extends Element>(el: ParentNode, sel: string): T => {
  const x = el.querySelector<T>(sel);
  if (x === null) throw new Error(`no ${sel}`);
  return x;
};
async function block() {
  const h = await harness();
  const b = createEndBlock("p1", h.hub);
  h.c.subscribe(() => b.update());
  return { ...h, b, el: b.el };
}
const PILLS = ["内容太简单", "基本合适", "理解有困难"];
const pill = (el: HTMLElement, label: string) => [...el.querySelectorAll<HTMLButtonElement>(".ew-chips > button.ew-pill")].find((b) => b.textContent === label)!;
const pressed = (el: HTMLElement) => PILLS.map((f) => pill(el, f).getAttribute("aria-pressed"));
const next = (el: HTMLElement) => q<HTMLButtonElement>(el, ".ew-act > button.ew-btn.ew-primary");
/** The error line: a .ew-hint after .ew-act. */
const line = (el: HTMLElement) => el.querySelector(".ew-act ~ .ew-hint")?.textContent ?? null;

describe("the end block = app.js endBlock", () => {
  it(".ew.ew-inline > .ew-endb: h3, .ew-chips (3 pills), hr, .ew-act (the primary only, pushed right); no form", async () => {
    const { el } = await block();
    expect(el.className).toBe("ew ew-inline evowiki-end-host");
    const b = q<HTMLElement>(el, ":scope > div.ew-endb");
    expect([...b.children].map((x) => `${x.tagName.toLowerCase()}${x.className ? "." + x.className : ""}`))
      .toEqual(["h3", "div.ew-chips", "hr", "div.ew-act"]);
    expect(q(b, "h3").textContent).toBe("这页内容对你有帮助吗？");
    expect([...b.querySelectorAll(".ew-pill")].map((x) => [x.textContent, x.getAttribute("aria-pressed"), (x as HTMLButtonElement).type]))
      .toEqual([["内容太简单", "false", "button"], ["基本合适", "false", "button"], ["理解有困难", "false", "button"]]);
    expect(b.querySelector("textarea")).toBeNull();
    expect(b.querySelector("input")).toBeNull();
    expect(q(b, ".ew-act").children).toHaveLength(1);
    expect(next(el).className).toBe("ew-btn ew-primary ew-round");
    expect(next(el).style.marginLeft).toBe("auto");
    expect(el.textContent).not.toContain("根据反馈修改");
    expect(el.textContent).not.toContain("将这次反馈用于优化后续内容");
    expect(line(el)).toBeNull();
  });

  it("one choice with aria-pressed; 内容太简单 / 理解有困难 are sent to the assistant, 基本合适 is not", async () => {
    const { el, sentFresh, api } = await block();
    const n = api.calls.length;
    pill(el, "基本合适").click();
    expect(pressed(el)).toEqual(["false", "true", "false"]);
    expect(sentFresh).toEqual([]);
    pill(el, "理解有困难").click();
    expect(pressed(el)).toEqual(["false", "false", "true"]);
    pill(el, "内容太简单").click();
    expect(pressed(el)).toEqual(["true", "false", "false"]);
    expect(sentFresh.map((x) => x.text)).toEqual(["这页理解有困难", "这页内容太简单"]);
    expect(api.calls.slice(n).map((x) => x.method)).not.toContain("pageFeedback");
  });

  it("primary off a path: 这一页学完了; on a path: 继续学习 →", async () => {
    const { el } = await block();
    expect(next(el).textContent).toBe(WIKI_COPY.endPageDone);
    const h = await harness();
    await newGoal(h.c, "学统计推断");
    await h.c.pathNext();
    const id = h.c.state.page.kind === "open" ? h.c.state.page.page.page_id : "";
    const b = createEndBlock(id, h.hub);
    expect(next(b.el).textContent).toBe(WIKI_COPY.endNext);
  });

  it("finishing a page replaces its action with a small noninteractive check, including after reopening", async () => {
    const { el, c, api } = await block();
    await c.finishPage("p1");
    expect(el.querySelector(".ew-act > button.ew-primary")).toBeNull();
    expect(el.querySelector(".ew-end-completed")?.textContent).toBe("✓");
    const n = api.calls.filter((call) => call.method === "done").length;
    await c.finishPage("p1");
    expect(api.calls.filter((call) => call.method === "done")).toHaveLength(n);
    await c.openWikiPage("p1");
    expect(el.querySelector(".ew-end-completed")?.textContent).toBe("✓");
    expect(el.querySelector(".ew-act > button.ew-primary")).toBeNull();
  });

  it("a completed goal shows its ordinary confirmation checks below the checkmark and answers through the existing evidence action", async () => {
    const h = await block();
    const read = h.api.openPage.bind(h.api);
    let finished = false;
    const done = h.api.done.bind(h.api);
    h.api.done = async (id) => { const out = await done(id); finished = true; return out; };
    h.api.openPage = async (id) => {
      const page = await read(id);
      const check = page.sections.find((s) => s.check !== null)!;
      return { ...page, done: finished, next_step: { kind: "goal_done", goal_id: "g1", goal: "统计", map_id: "g1", region_id: null,
        region_name: "", path_id: null, index: null, page_id: null, title: "", draft_id: null, progress: null, failure: null, review_first: null },
        sections: [...page.sections, ...(finished ? [1, 2].map((n) => ({ ...check, id: `goal_check_${n}` })) : [])] };
    };
    await h.c.openWikiPage("p1");
    expect(next(h.el).dataset.kind).toBe("goal-done");
    await h.c.finishPage("p1");
    expect(h.el.querySelectorAll(".ew-goal-checks .ew-check")).toHaveLength(2);
    const footer = h.el.querySelector(".ew-act")!;
    expect(footer.compareDocumentPosition(h.el.querySelector(".ew-goal-checks")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const grade = h.api.answerCheck.bind(h.api);
    const answer = vi.spyOn(h.api, "answerCheck").mockImplementation((id, _sid, choice, artifact) => grade(id, "s5", choice, artifact));
    const graphChanged = vi.fn();
    const off = graphBus.subscribe(graphChanged);
    try {
      h.el.querySelector<HTMLButtonElement>(".ew-goal-checks .ew-o")!.click();
      await vi.waitFor(() => expect(graphChanged).toHaveBeenCalledOnce());
      expect(answer).toHaveBeenCalledWith("p1", "goal_check_1", 0, expect.any(String));
      const n = h.api.calls.filter((call) => call.method === "done").length;
      await h.c.finishPage("p1");
      expect(h.api.calls.filter((call) => call.method === "done")).toHaveLength(n);
    } finally { off(); }
    expect(sectionLines("p1", { id: "goal_check_1", role: "check", heading: "", cue: "", body: "", folded: false })).toEqual([]);
  });

  it("an older completed goal without confirmation questions can retry generation; a failure keeps the action visible", async () => {
    const h = await block();
    const p = h.c.state.page;
    if (p.kind !== "open") throw new Error("no page");
    p.page.done = true;
    p.page.next_step = { kind: "goal_done", goal_id: "g1", goal: "统计", map_id: "g1", region_id: null,
      region_name: "", path_id: null, index: null, page_id: null, title: "", draft_id: null, progress: null, failure: null, review_first: null };
    h.b.update();
    h.api.failNext("done", new ApiError(503, "暂时出不了题"));
    await h.c.finishPage("p1");
    expect(h.api.calls.filter((call) => call.method === "done")).toHaveLength(1);
    expect(h.el.querySelector(".ew-end-completed")?.textContent).toBe("✓");
    expect(h.el.querySelector<HTMLButtonElement>(".ew-start-goal-checks")!.disabled).toBe(false);
    expect(line(h.el)).toBe(WIKI_COPY.endDoneFailed);
  });

  it("offers an explicit check start on an already completed goal, blocks duplicates, then removes it when checks arrive", async () => {
    const h = await block();
    const read = h.api.openPage.bind(h.api);
    const initial = h.c.state.page;
    if (initial.kind !== "open") throw new Error("no page");
    const terminal = { kind: "goal_done" as const, goal_id: "g1", goal: "统计", map_id: "g1", region_id: null,
      region_name: "", path_id: null, index: null, page_id: null, title: "", draft_id: null, progress: null, failure: null, review_first: null };
    let generated = false;
    h.api.openPage = async (id) => {
      const page = await read(id);
      const check = page.sections.find((s) => s.check !== null)!;
      return { ...page, done: true, next_step: terminal,
        sections: [...page.sections, ...(generated ? [{ ...check, id: "goal_check_1" }] : [])] };
    };
    const calls = h.api.calls.filter((c) => c.method === "done").length;
    await h.c.openWikiPage("p1");
    expect(h.api.calls.filter((c) => c.method === "done")).toHaveLength(calls);
    expect(h.el.querySelector(".ew-end-completed")?.textContent).toBe("✓");
    expect(h.el.querySelector(".ew-act > button.ew-primary")).toBeNull();
    const start = h.el.querySelector<HTMLButtonElement>(".ew-start-goal-checks")!;
    expect(start.textContent).toBe("开始确认题");
    setUiLanguage("en");
    try { expect(start.textContent).toBe("Start checks"); } finally { setUiLanguage("zh"); }
    let release = (): void => {};
    const waiting = new Promise<void>((resolve) => { release = resolve; });
    const done = h.api.done.bind(h.api);
    const post = vi.spyOn(h.api, "done").mockImplementation(async (id) => {
      await waiting; const out = await done(id); generated = true; return out;
    });
    start.click();
    expect(start.disabled).toBe(true);
    start.click();
    await h.c.finishPage("p1");
    expect(post).toHaveBeenCalledTimes(1);
    release();
    await vi.waitFor(() => expect(h.el.querySelector(".ew-start-goal-checks")).toBeNull());
    expect(h.el.querySelector(".ew-end-completed")?.textContent).toBe("✓");
    expect(h.el.querySelectorAll(".ew-goal-checks .ew-check")).toHaveLength(1);
    expect(post).toHaveBeenCalledTimes(1);
    await h.c.finishPage("p1");
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("offline: the primary button is disabled with the reason as the line", async () => {
    const { el, conn } = await block();
    conn.emit("offline");
    pill(el, "基本合适").click();
    expect(next(el).disabled).toBe(true);
    expect(line(el)).toBe(WIKI_COPY.offline);
  });

  it("draws nothing when the page is not the open one", async () => {
    const h = await harness();
    expect(createEndBlock("other", h.hub).el.children.length).toBe(0);
  });
});
