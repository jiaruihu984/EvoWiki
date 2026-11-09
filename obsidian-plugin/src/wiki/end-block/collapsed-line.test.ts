import { describe, expect, it } from "vitest";
import { WIKI_COPY } from "../copy";
import { harness } from "./test-kit";
import type { WikiState } from "../state";
import { pageReuse, topLine } from "./model";
import { createTopLine } from "./page-lines";

describe("a page another goal reuses", () => {
  const notes = [{ map_id: "m1", goal: "孔雀鱼", note: "孔雀鱼要 24 度" }, { map_id: "m2", goal: "斗鱼", note: "斗鱼要 26 度" }, { map_id: "m3", goal: "金鱼", note: "" }];
  const open = (pageGoal: { pageId: string; mapId: string } | null, written_for = "黑壳虾") =>
    ({ pageGoal, page: { kind: "open", page: { page_id: "p", written_for, goal_notes: notes } } }) as unknown as WikiState;

  it("from a goal shows that goal's note only", () => {
    expect(pageReuse(open({ pageId: "p", mapId: "m2" }), "p")?.notes.map((n) => n.goal)).toEqual(["斗鱼"]);
  });
  it("a goal the page fits as it is: where it was written, no note", () => {
    expect(pageReuse(open({ pageId: "p", mapId: "m3" }), "p")).toEqual({ writtenFor: "黑壳虾", notes: [] });
  });
  it("with no goal context shows every goal's, also when the goal was another page's", () => {
    expect(pageReuse(open(null), "p")?.notes.map((n) => n.goal)).toEqual(["孔雀鱼", "斗鱼"]);
    expect(pageReuse(open({ pageId: "q", mapId: "m2" }), "p")?.notes.length).toBe(2);
  });
  it("a page no goal reuses shows nothing", () => {
    expect(pageReuse(open(null, ""), "p")).toBeNull();
  });
});

describe("the collapsed-page line (恢复成完整页 entry point on the note)", () => {
  it("a collapsed page shows its card line (shelf_line) with one .ew-lk that expands it; a full page has no such line", async () => {
    const h = await harness();
    const pageId = h.c.state.page.kind === "open" ? h.c.state.page.page.page_id : "";
    expect(topLine(h.c.state, pageId)).toBeNull();
    h.api.__collapse(pageId);
    await h.c.followPage(pageId);                        // re-read: the page is now a reference card
    const card = h.c.state.page.kind === "open" ? h.c.state.page.page.shelf_line : "";
    expect(card).not.toBe("");
    const t = topLine(h.c.state, pageId);
    expect(t).toEqual({ kind: "collapsed", text: card, busy: false });
    const line = createTopLine(pageId, h.hub);
    const top = line.el.querySelector<HTMLElement>(":scope > .ew-topline")!;
    expect(top.dataset.kind).toBe("collapsed");
    expect(top.textContent).toBe(`${card} · 恢复成完整页`);
    const btn = top.querySelector<HTMLButtonElement>("button.ew-lk");
    expect(btn?.textContent).toBe("恢复成完整页");
    btn?.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(h.c.state.page.kind === "open" && h.c.state.page.page.collapsed).toBe(false);
    line.update();
    expect(line.el.children.length).toBe(0);
  });

  it("no card line: 这一页收起来了。", async () => {
    const h = await harness();
    const pageId = h.c.state.page.kind === "open" ? h.c.state.page.page.page_id : "";
    h.api.__collapse(pageId);
    await h.c.followPage(pageId);
    const s = h.c.state;
    const noLine = s.page.kind === "open" ? { ...s, page: { ...s.page, page: { ...s.page.page, shelf_line: " " } } } : s;
    expect(topLine(noLine, pageId)).toEqual({ kind: "collapsed", text: WIKI_COPY.topCollapsed, busy: false });
  });

  it("the line is drawn, never written: the note bytes are the same with it shown, and its text is not in the vault", async () => {
    const h = await harness();
    const pageId = h.c.state.page.kind === "open" ? h.c.state.page.page.page_id : "";
    h.api.__collapse(pageId);
    await h.c.followPage(pageId);
    await new Promise((r) => setTimeout(r, 20));
    const before = h.text();
    const line = createTopLine(pageId, h.hub);
    line.update();
    expect(line.el.querySelector(".ew-topline")).not.toBeNull();
    expect(h.text()).toBe(before);
    expect(h.text()).not.toContain(WIKI_COPY.topCollapsed);
    expect(h.text()).not.toContain(WIKI_COPY.expandPage);
  });
});
