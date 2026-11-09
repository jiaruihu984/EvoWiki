/**
 * Reading view and Live Preview: where the end block, the top line and the tags go, that Source mode and a note without the
 * 我的笔记 anchor get nothing, and that none of it is ever written to the note file.
 */
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, describe, expect, it, vi } from "vitest";
import { editorLivePreviewField } from "../../../test/obsidian-mock";
import { WIKI_COPY } from "../copy";
import { MINE_HEADING } from "../note/format";
import { noteHeadings } from "../section-menu";
import { endBlockExtension, endBlockPostProcessor } from "./end-block-view";
import { harness, type Harness } from "./test-kit";
import type { ExamPort } from "../exam/exam-block";
import { demoPageExam } from "../exam/exam-fixture";

vi.mock("../section-menu", async (importOriginal) => {
  const m = await importOriginal<typeof import("../section-menu")>();
  return { ...m, noteHeadings: vi.fn(m.noteHeadings) };
});

type Ctx = Parameters<ReturnType<typeof endBlockPostProcessor>>[1];
const views: EditorView[] = [];
afterEach(() => { for (const v of views.splice(0)) v.destroy(); vi.mocked(noteHeadings).mockClear(); });

/** The strings only the plugin-drawn parts say; none may ever be in the file. */
const DRAWN = [WIKI_COPY.endTitle, WIKI_COPY.feelShallow, WIKI_COPY.feelHard, WIKI_COPY.sectionTag,
  WIKI_COPY.topUndo, "本页已更新", "这一页由备用模型写成",
  WIKI_COPY.topUndoing, WIKI_COPY.noticeUndone, WIKI_COPY.topUndoFailed];
const expectNoDrawn = (text: string) => { for (const s of DRAWN) expect(text).not.toContain(s); };

function run(h: Harness, text: string, lineStart: number, el: HTMLElement, lineEnd = lineStart) {
  const addChild = vi.fn();
  endBlockPostProcessor(h.hub)(el, { getSectionInfo: () => ({ text, lineStart, lineEnd }), addChild } as unknown as Ctx);
  return addChild;
}
const lineOf = (text: string, line: string) => text.split("\n").indexOf(line);
const pill = (el: ParentNode, label: string) => [...el.querySelectorAll<HTMLButtonElement>(".ew-pill")].find((b) => b.textContent === label) ?? null;
/** A feedback patch made on the server (the assistant changed the page), then read and synced as the assistant's edit is. */
async function patchPage(h: Harness): Promise<void> {
  await h.api.pageFeedback("p1", { chips: ["too_hard"], text: "术语没先讲", section_id: null, apply: false });
  await h.c.pageEditedElsewhere("p1");
}
const h2 = (t: string) => { const el = document.createElement("div"); const x = document.createElement("h2"); x.textContent = t; el.append(x); return el; };

describe("Reading view", () => {
  it("the end block goes in front of the 我的笔记 heading, and only there", async () => {
    const h = await harness();
    const text = h.text();
    const el = h2("我的笔记");
    run(h, text, lineOf(text, MINE_HEADING), el);
    expect(el.firstElementChild?.className).toBe("ew ew-inline evowiki-end-host");
    expect(el.firstElementChild?.nextElementSibling?.tagName).toBe("H2");
    expect(el.querySelector(".ew-endb > h3")?.textContent).toBe(WIKI_COPY.endTitle);
    const other = h2("别的标题");
    run(h, text, lineOf(text, MINE_HEADING) + 3, other);
    expect(other.querySelector(".ew-endb")).toBeNull();
  });

  it("with an exam port: 真题练习 goes above the end block (exam host, end host, heading); its cleanup removes both", async () => {
    const h = await harness();
    const text = h.text();
    const el = h2("我的笔记");
    const exam: ExamPort = { hub: { current: { pageExam: () => Promise.resolve(demoPageExam()), examSelf: () => Promise.resolve({ status: "done" }) },
      subscribe: () => () => undefined }, ui: new Map(), mountQuiz: () => () => undefined };
    const addChild = vi.fn();
    endBlockPostProcessor(h.hub, exam)(el, { getSectionInfo: () => ({ text, lineStart: lineOf(text, MINE_HEADING), lineEnd: lineOf(text, MINE_HEADING) }), addChild } as unknown as Ctx);
    await new Promise((r) => setTimeout(r, 0));
    expect([...el.children].map((c) => c.className || c.tagName)).toEqual(["ew ew-inline evowiki-exam-host", "ew ew-inline evowiki-end-host", "H2"]);
    expect(el.querySelector(".ew-examb > .ew-thead > h3")?.textContent).toBe("真题练习");
    expectNoDrawn(text);
    expect(text).not.toContain("真题练习");
    (addChild.mock.calls[0]![0] as { unload(): void }).unload();
    expect([...el.children].map((c) => c.tagName)).toEqual(["H2"]);
  });

  it("a note without the 我的笔记 line gets no block (fail closed); neither does a file that is not a wiki note", async () => {
    const h = await harness();
    const text = h.text().replace(MINE_HEADING, "## 随手记");
    const el = h2("我的笔记");
    run(h, text, lineOf(h.text(), MINE_HEADING), el);
    expect(el.querySelector(".ew-endb")).toBeNull();
    const plain = h2("我的笔记");
    const add = run(h, "# 普通笔记\n\n## 我的笔记\n", 2, plain);
    expect(plain.querySelector(".ew-endb")).toBeNull();
    expect(add).not.toHaveBeenCalled();
  });

  it("the block follows the controller: it is current after a state change and its mount is removed with the child", async () => {
    const h = await harness();
    const text = h.text();
    const el = h2("我的笔记");
    const add = run(h, text, lineOf(text, MINE_HEADING), el);
    h.c.pickEndChip("p1", "hard");
    expect(pill(el, "理解有困难")?.getAttribute("aria-pressed")).toBe("true");
    (add.mock.calls[0]![0] as { unload(): void }).unload();
    expect(el.querySelector(".ew-endb")).toBeNull();
  });

  it("the file's bytes do not change by rendering or choosing; nothing the plugin draws is in the file", async () => {
    const h = await harness();
    const before = h.text();
    const el = h2("我的笔记");
    run(h, before, lineOf(before, MINE_HEADING), el);
    pill(el, "基本合适")!.click();
    pill(el, "理解有困难")!.click();
    expect(h.text()).toBe(before);
    expectNoDrawn(h.text());
  });

  it("after a page patch and 撤回 the file holds only the patch itself, none of the drawn strings; 撤回 restores the old bytes", async () => {
    const h = await harness();
    const before = h.text();
    await patchPage(h);
    const patched = h.text();
    expect(patched).not.toBe(before);
    expect(patched).toContain("（按你的反馈补充）");
    expectNoDrawn(patched);
    expect(patched.slice(patched.indexOf(MINE_HEADING))).toBe(before.slice(before.indexOf(MINE_HEADING)));
    await h.c.undoPageFeedback("p1");
    expectNoDrawn(h.text());
    expect(h.text()).toBe(before);
  });

  it("the top line goes into the element holding the first content line (.ew.ew-inline > .ew-topline, text · 撤回)", async () => {
    const h = await harness();
    await patchPage(h);
    const text = h.text();
    const lines = text.split("\n");
    const start = lines.indexOf("%% evowiki:start %%");
    let first = start + 1;
    while (lines[first]!.trim() === "") first++;
    const top = document.createElement("div");
    top.append(document.createElement("p"));
    run(h, text, first, top, first + 1);
    expect(top.firstElementChild?.className).toBe("ew ew-inline evowiki-topline-host");
    const line = top.querySelector<HTMLElement>(".ew-inline > div.ew-topline")!;
    expect(line.getAttribute("role")).toBe("status");
    expect(line.textContent).toMatch(/^本页已更新：.+ · 撤销$/);
    expect(line.querySelector("button.ew-lk")?.textContent).toBe("撤销");
    await h.c.undoPageFeedback("p1");
    expect(top.querySelector(".ew-topline")).toBeNull();
  });

  it("撤回 on the top line: the failure line keeps the button; success removes the line", async () => {
    const h = await harness();
    await patchPage(h);
    const text = h.text();
    const lines = text.split("\n");
    let first = lines.indexOf("%% evowiki:start %%") + 1;
    while (lines[first]!.trim() === "") first++;
    const top = document.createElement("div");
    top.append(document.createElement("p"));
    run(h, text, first, top, first);
    const { ApiError } = await import("../../api/errors");
    h.api.failNext("undoFeedback", new ApiError(500, "x"));
    top.querySelector<HTMLButtonElement>(".ew-topline .ew-lk")!.click();
    await vi.waitFor(() => expect(top.querySelector(".ew-topline")?.firstChild?.textContent).toBe(WIKI_COPY.topUndoFailed));
    expect(top.querySelector(".ew-topline .ew-lk")?.textContent).toBe("撤销");
    top.querySelector<HTMLButtonElement>(".ew-topline .ew-lk")!.click();
    await vi.waitFor(() => expect(top.querySelector(".ew-topline")).toBeNull());
    expect(h.notices).toContain(WIKI_COPY.noticeUndone);
  });
});

function editor(h: Harness, doc = h.text(), live = true) {
  const ext = endBlockExtension(h.hub);
  const exts = live ? [editorLivePreviewField, ext] : [editorLivePreviewField.init(() => false), ext];
  const view = new EditorView({ state: EditorState.create({ doc, extensions: exts }), parent: document.body });
  views.push(view);
  // jsdom has no layout: CodeMirror draws only ~500 estimated pixels from the top; scrolling to the end draws the note's tail too.
  view.dispatch({ effects: EditorView.scrollIntoView(view.state.doc.length) });
  return view;
}

describe("Live Preview", () => {
  it("block widgets: the top line after the START line and the end block between the END line and 我的笔记", async () => {
    const h = await harness();
    const v = editor(h);
    const end = v.dom.querySelector(".evowiki-end-widget > .ew.ew-inline > .ew-endb");
    expect(end).not.toBeNull();
    expect(v.dom.querySelector(".evowiki-topline-widget")).not.toBeNull();
    // the widget follows the END line directly (the 我的笔记 line is the next one in the document)
    expect(v.dom.querySelector(".evowiki-end-widget")?.previousElementSibling?.textContent).toBe("%% evowiki:end %%");
    expect(v.state.doc.toString()).toBe(h.text());
  });

  it("Source mode and a note without the anchor show nothing", async () => {
    const h = await harness();
    const src = editor(h, h.text(), false);
    expect(src.dom.querySelector(".evowiki-end-widget, .evowiki-topline-widget")).toBeNull();
    const noMine = editor(h, h.text().replace(MINE_HEADING, "## 随手记"));
    expect(noMine.dom.querySelector(".evowiki-end-widget")).toBeNull();
    const other = editor(h, "# 普通笔记\n\n## 我的笔记\n");
    expect(other.dom.querySelector(".evowiki-end-widget, .evowiki-topline-widget")).toBeNull();
  });

  it("typing below 我的笔记 keeps the widgets and does not re-read the note; editing the block re-reads it", async () => {
    const h = await harness();
    const v = editor(h);
    const host = v.dom.querySelector(".evowiki-end-widget");
    vi.mocked(noteHeadings).mockClear();
    for (const ch of "今天读完了") v.dispatch({ changes: { from: v.state.doc.length, insert: ch } });
    expect(noteHeadings).not.toHaveBeenCalled();
    expect(v.dom.querySelector(".evowiki-end-widget")).toBe(host);
    const at = v.state.doc.toString().indexOf("## ") + 3;
    v.dispatch({ changes: { from: at, insert: "x" } });
    expect(noteHeadings).toHaveBeenCalled();
  });

  it("the widget works in the editor: a chip click reaches the controller; the document is untouched", async () => {
    const h = await harness();
    const before = h.text();
    const v = editor(h);
    pill(v.dom, "内容太简单")!.click();
    expect(h.c.endBlockState("p1").draft.chips).toEqual(["shallow"]);
    expect(v.state.doc.toString()).toBe(before);
  });
});
