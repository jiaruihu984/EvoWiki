/**
 * 用资料改这一页 in the page (UI principles 2026-10-21 §三): the character diff, where the marks go in a real wiki note, the
 * Reading-view and Live-Preview overlay, the sticky bar's 确认替换 / 取消, the top line's 撤销 and the 活动记录 row. The note
 * file is never written before 确认替换; after it, the note sync writes the page the server now has.
 */
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, describe, expect, it, vi } from "vitest";
import { editorLivePreviewField } from "../../../test/obsidian-mock";
import type { ActionOut, ReviseOut } from "../bot/bot-schema";
import { pageWaitActions } from "../page-waits";
import { createTopLine } from "../end-block/page-lines";
import { harness as pageHarness, type Harness } from "../end-block/test-kit";
import type { PageOut, SectionOut } from "../schema";
import { REVISE_COPY } from "./copy";
import { ApiError, OfflineError } from "../../api/errors";
import { DraftDecisions } from "./decisions";
import { paraDiff, paragraphs, revisePlan, wordDiff } from "./model";
import { applyHeader } from "../note-page/header";
import { createHeadingParts, headingView } from "../section-menu";
import { renderedPart } from "./marks";
import { Component } from "obsidian";
import { createBar, TrackWidget, mountReadingBlock, reviseEditorExtension, reviseReadingPostProcessor, watchReviseBar } from "./overlay";
import { appliedRevise, barTitle, pendingRevise } from "./rows";

const flush = async (): Promise<void> => { for (let i = 0; i < 20; i++) await new Promise((r) => setTimeout(r, 0)); };

/**
 * A small stand-in for Obsidian's MarkdownRenderer: fenced code → pre > code, `- ` lines → ul > li, `$$…$$` → display
 * math, other blocks → p; inline **bold**, `code`, $math$ (a .math span), [[target|text]] → a.internal-link; backslash
 * escapes dropped.
 */
const NL = String.fromCharCode(10);
function inline(parent: HTMLElement, t: string): void {
  const re = /\*\*([^*]+)\*\*|`([^`]+)`|\$([^$]+)\$|\[\[[^|\]]+\|([^\]]+)\]\]|\\(.)/g;
  let at = 0;
  for (const m of t.matchAll(re)) {
    parent.append(t.slice(at, m.index));
    if (m[1] !== undefined) parent.append(Object.assign(document.createElement("strong"), { textContent: m[1] }));
    else if (m[2] !== undefined) parent.append(Object.assign(document.createElement("code"), { textContent: m[2] }));
    else if (m[3] !== undefined) parent.append(Object.assign(document.createElement("span"), { className: "math math-inline", textContent: m[3] }));
    else if (m[4] !== undefined) parent.append(Object.assign(document.createElement("a"), { className: "internal-link", textContent: m[4] }));
    else parent.append(m[5]!);
    at = m.index + m[0].length;
  }
  parent.append(t.slice(at));
}
const miniMd = (md: string, into: HTMLElement): Promise<void> => {
  for (const b of paragraphs(md)) {
    if (b.startsWith("```")) {
      const pre = document.createElement("pre");
      pre.append(Object.assign(document.createElement("code"), { textContent: b.split(NL).slice(1, -1).join(NL) }));
      into.append(pre);
    } else if (b.startsWith("$$")) into.append(Object.assign(document.createElement("div"), { className: "math math-block", textContent: b.slice(2, -2) }));
    else if (b.startsWith("- ")) {
      const ul = document.createElement("ul");
      for (const l of b.split(NL)) { const li = document.createElement("li"); inline(li, l.replace(/^- /, "")); ul.append(li, NL); }
      into.append(ul, NL);
    } else { const p = document.createElement("p"); inline(p, b); into.append(p, NL); }
  }
  return Promise.resolve();
};
const render = (md: string, into: HTMLElement): Promise<void> => miniMd(md, into);
const views: EditorView[] = [];
afterEach(() => { for (const v of views.splice(0)) v.destroy(); document.body.replaceChildren(); });

describe("wordDiff (app.js wordDiff)", () => {
  it("marks phrases: an equal run under 4 characters between two changes goes into both sides", () => {
    expect(wordDiff("abcXdefYghi", "abcZdefWghi")).toEqual([["=", "abc"], ["-", "XdefY"], ["+", "ZdefW"], ["=", "ghi"]]);
    // 4 equal characters or more stay as they are
    expect(wordDiff("abcXdefgYhij", "abcZdefgWhij")).toEqual([["=", "abc"], ["-", "X"], ["+", "Z"], ["=", "defg"], ["-", "Y"], ["+", "W"], ["=", "hij"]]);
  });
  it("on the demo's sentence: both texts come back from the marks", () => {
    const a = "这时 v 叫 A 的特征向量，倍数 λ 叫特征值。λ = 2 表示这个方向被拉长到 2 倍；λ = 0.5 表示被压短一半。";
    const b = "λ 叫特征值，它是特征方程 det(A − λI) = 0 的根；v 叫对应的特征向量。λ = 2 表示这个方向被拉长到 2 倍。";
    const ops = wordDiff(a, b);
    expect(ops.filter((o) => o[0] !== "+").map((o) => o[1]).join("")).toBe(a);
    expect(ops.filter((o) => o[0] !== "-").map((o) => o[1]).join("")).toBe(b);
    expect(ops.some((o) => o[0] === "=" && o[1].includes("表示这个方向被拉长到 2 倍"))).toBe(true);
  });
  it("30k-char paragraphs with a few edits finish quickly and the marks rebuild both texts", () => {
    const base = Array.from({ length: 30000 }, (_, i) => "天地玄黄宇宙洪荒日月盈昃辰宿列张"[(i * 7 + (i >> 3)) % 16]).join("");
    const b = base.slice(0, 9000) + "改动甲" + base.slice(9010, 20000) + "乙" + base.slice(20001);
    const t0 = Date.now();
    const ops = wordDiff(base, b);
    expect(Date.now() - t0).toBeLessThan(2500);
    expect(ops.filter((o) => o[0] !== "+").map((o) => o[1]).join("")).toBe(base);
    expect(ops.filter((o) => o[0] !== "-").map((o) => o[1]).join("")).toBe(b);
    expect(ops.some((o) => o[0] === "=" && o[1].length > 5000)).toBe(true);
  });
  it("two wholly different 30k-char paragraphs fall back to one replaced block (fail closed), fast", () => {
    const a = "甲".repeat(15000) + "乙".repeat(15000), b = "丙丁".repeat(15000);
    const t0 = Date.now();
    expect(wordDiff(a, b)).toEqual([["-", a], ["+", b]]);
    expect(Date.now() - t0).toBeLessThan(3000);
    expect(wordDiff("", "新")).toEqual([["+", "新"]]);
  });
});

describe("paraDiff", () => {
  it("keeps equal paragraphs, pairs changed ones in order, never pairs a 和资料不同 note", () => {
    expect(paraDiff(["一", "二", "三"], ["一", "二改", "⟦和资料不同：讲义写错了⟧", "三"])).toEqual([
      { kind: "same", i: 0, j: 0 }, { kind: "change", i: 1, j: 1 }, { kind: "ins", j: 2 }, { kind: "same", i: 2, j: 3 },
    ]);
    expect(paraDiff(["一", "二"], ["一"])).toEqual([{ kind: "same", i: 0, j: 0 }, { kind: "del", i: 1 }]);
    // whitespace does not matter for equality; many paragraphs stay fast
    expect(paraDiff(["a b", "c"], ["ab", "c"])).toEqual([{ kind: "same", i: 0, j: 0 }, { kind: "same", i: 1, j: 1 }]);
    const many = Array.from({ length: 5000 }, (_, i) => `段${i}`);
    const t0 = Date.now();
    const ops = paraDiff(many, [...many.slice(0, 2500), "新", ...many.slice(2501)]);
    expect(Date.now() - t0).toBeLessThan(2500);
    expect(ops.filter((o) => o.kind === "same")).toHaveLength(4999);
    expect(ops.filter((o) => o.kind === "change")).toHaveLength(1);
    expect(paragraphs("甲\n\n```\na\n\nb\n```\n\n乙")).toEqual(["甲", "```\na\n\nb\n```", "乙"]);
  });
});

/**
 * The harness page without the fixture's merge waiting on it (a ledger wait has a bar of its own under the draft's,
 * page-waits.ts; these tests are about the draft's bar). The reject is taken out of the recorded calls.
 */
async function harness(o?: Parameters<typeof pageHarness>[0]): Promise<Harness> {
  const h = await pageHarness(o);
  await h.api.botDecide("act_merge1", "reject");
  h.api.calls.splice(h.api.calls.findIndex((x) => x.method === "botDecide"), 1);
  await h.c.botLedgerChanged();
  return h;
}

// ---- a draft on the harness page ----

const NEW_LINE = "这是按讲义改写的新的一段，说法更准确。";
const NOTE = "讲义 p.14 写的是「λ=0 时矩阵可逆」，应为不可逆。";

function target(h: Harness): { page: PageOut; sec: SectionOut } {
  const p = h.c.state.page;
  if (p.kind !== "open") throw new Error("no page");
  const sec = p.page.sections.find((x) => (x.role === "explain" || x.role === "compare") && !x.folded && x.body.trim() !== "");
  if (!sec) throw new Error("no section");
  return { page: p.page, sec };
}

function draftRow(h: Harness, over: Partial<ActionOut> = {}, rv: Partial<ReviseOut> = {}): ActionOut {
  const { page, sec } = target(h);
  const old = paragraphs(sec.body);
  const nb = [NEW_LINE, ...old.slice(1), `⟦和资料不同：${NOTE}⟧`].join("\n\n");
  return {
    action_id: "rv1", kind: "page_revise", scope: "page_body", level: "ask", status: "proposed", source: "assistant",
    summary: `按资料改「${page.title}」`, why: "", content: {}, page_id: page.page_id, detail: "", created_at: "2026-10-21T08:00:00Z",
    decided_at: null, expires_at: null, can_undo: false, diff: [], confirm: true, cost_usd: null, made: [], section_id: null,
    revise: { state: "ready", page_id: page.page_id, run_id: "run1", detail: "", sources: [{ source_id: "src1", title: "线性代数讲义.pdf", deleted: false }],
      sections: [{ section_id: sec.id, heading: sec.heading, old_heading: sec.heading, status: "changed", old_body: sec.body, new_body: nb }],
      unchanged: 2, rebuild: false, cite_issues: 0, stale: false, practice: null, ...rv },
    result: {}, ...over,
  };
}

async function withDraft(over: Partial<ActionOut> = {}, rv: Partial<ReviseOut> = {}) {
  const h = await harness();
  h.api.putBotAction(draftRow(h, over, rv));
  await h.c.botLedgerChanged();
  return h;
}

/** Reading view: one rendered block per paragraph of the note (like Obsidian's), each post-processed. */
function reading(h: Harness): HTMLElement[] {
  const text = h.text();
  const lines = text.split("\n");
  const blocks: HTMLElement[] = [];
  let start = -1;
  const pp = reviseReadingPostProcessor(h.hub, render);
  const end = (k: number): void => {
    if (start < 0) return;
    const el = document.createElement("div");
    const p = document.createElement("p");
    p.textContent = lines.slice(start, k + 1).join("\n");
    el.append(p);
    const info = { text, lineStart: start, lineEnd: k };
    pp(el, { getSectionInfo: () => info, addChild: vi.fn() } as unknown as Parameters<typeof pp>[1]);
    blocks.push(el);
    start = -1;
  };
  lines.forEach((l, k) => { if (l.trim() === "") end(k - 1); else if (start < 0) start = k; });
  end(lines.length - 1);
  return blocks;
}

describe("where the marks go (revisePlan)", () => {
  it("replaces only the changed paragraph of the section and puts the new 和资料不同 line after it", async () => {
    const h = await withDraft();
    const plan = revisePlan(h.c.state, target(h).page.page_id, h.text())!;
    expect(plan).not.toBeNull();
    const lines = h.text().split("\n");
    const first = paragraphs(target(h).sec.body)[0]!;
    const rep = plan.pieces.find((x) => x.at.kind === "replace")!;
    expect(rep.at.kind === "replace" && lines.slice(rep.at.from, rep.at.to + 1).join("\n")).toContain(first.slice(0, 6));
    const kinds = plan.pieces.flatMap((x) => x.parts.map((p) => p.kind));
    expect(kinds).toEqual(["p", "note"]);
    expect(plan.pieces.flatMap((x) => x.parts).find((p) => p.kind === "note")).toEqual({ kind: "note", text: NOTE, added: true });
  });

  it("no plan without a ready draft (writing, decided, another page)", async () => {
    const h = await withDraft({}, { state: "writing", sections: [] });
    expect(revisePlan(h.c.state, target(h).page.page_id, h.text())).toBeNull();
    expect(revisePlan(h.c.state, "other", h.text())).toBeNull();
  });
});

describe("Reading view and Live Preview overlay", () => {
  it("Reading view: the changed paragraph's block is hidden and shows 删去 / 新增 marks; the note file is not touched", async () => {
    const h = await withDraft();
    const before = h.text();
    const blocks = reading(h);
    await flush();
    const off = blocks.filter((b) => b.classList.contains("evowiki-rv-off"));
    expect(off).toHaveLength(1);
    const trk = off[0]!.querySelector(".evowiki-trk-host .ew-trk")!;
    expect(trk.querySelector("ins")!.textContent).not.toBe("");
    expect(trk.textContent).toContain("按讲义改写");
    // the new 和资料不同 line comes after the section's last paragraph, as a whole green line with its label
    const note = blocks.flatMap((b) => [...b.querySelectorAll(".evowiki-trk-host .ew-insblk")]);
    expect(note).toHaveLength(1);
    expect(note[0]!.querySelector(".ew-srcnote .ew-snl")!.textContent).toBe("和资料不同");
    expect(note[0]!.textContent).toBe(`和资料不同${NOTE}`);
    // every other block is as Obsidian drew it
    const marked = blocks.filter((b) => b.querySelector(".evowiki-trk-host"));
    expect(marked.length).toBeLessThanOrEqual(2);
    expect(blocks.length - marked.length).toBeGreaterThan(3);
    expect(h.text()).toBe(before);
    for (const word of [REVISE_COPY.confirm, NEW_LINE, NOTE, "删去", "新增"]) expect(h.text()).not.toContain(word);
  });

  it("the marks go when the draft is cancelled (取消 → reject); still nothing written", async () => {
    const h = await withDraft();
    const blocks = reading(h);
    const runs = h.sync.runs.length;
    await h.c.reviseDecide("rv1", "reject");
    expect(h.api.calls.filter((x) => x.method === "botDecide").map((x) => x.args)).toEqual([["rv1", "reject"]]);
    expect(blocks.some((b) => b.classList.contains("evowiki-rv-off") || b.querySelector(".evowiki-trk-host"))).toBe(false);
    expect(h.sync.runs.length).toBe(runs);
  });

  it("Live Preview: the paragraph's lines are replaced by the marked text (the document is unchanged); Source mode: nothing", async () => {
    const h = await withDraft();
    const doc = h.text();
    const lp = new EditorView({ state: EditorState.create({ doc, extensions: [editorLivePreviewField.init(() => true), reviseEditorExtension(h.hub, render)] }), parent: document.body });
    const src = new EditorView({ state: EditorState.create({ doc, extensions: [editorLivePreviewField.init(() => false), reviseEditorExtension(h.hub, render)] }), parent: document.body });
    views.push(lp, src);
    await flush();
    expect(lp.dom.querySelector(".evowiki-trk-widget .ew-trk ins")).not.toBeNull();
    expect(lp.dom.querySelector(".evowiki-trk-widget .ew-insblk")).not.toBeNull();
    expect(src.dom.querySelector(".evowiki-trk-widget")).toBeNull();
    expect(lp.state.doc.toString()).toBe(doc);
    // the draft is cancelled: the view plugin rebuilds the field and the marks go
    await h.c.reviseDecide("rv1", "reject");
    await flush();
    expect(lp.dom.querySelector(".evowiki-trk-widget")).toBeNull();
  });
});

describe("the sticky bar, 确认替换 and the top line", () => {
  it("bar: 按《X》改了 N 节, ? with the reason, 确认替换 / 取消; 从资料重写 only for a rebuild", async () => {
    const h = await withDraft();
    const pid = target(h).page.page_id;
    const bar = createBar(() => pid, h.hub, () => h.text());
    const b = bar.el.querySelector(".ew-revbar")!;
    expect(b.querySelector(".ew-rb-t")!.textContent).toBe("按《线性代数讲义》改了 1 节");
    expect(b.querySelector(".ew-chip")).toBeNull();
    expect([...b.querySelectorAll(".ew-tipbox > span")].map((x) => x.textContent)).toEqual(["你让我按《线性代数讲义》改这一页", "用了《线性代数讲义》", "确认前这一页不变"]);
    expect([...b.querySelectorAll(".ew-rb-acts button")].map((x) => x.textContent)).toEqual(["确认替换", "取消"]);
    expect(b.getAttribute("aria-label")).toBe("待确认的草稿");
    const h2 = await withDraft({}, { rebuild: true });
    const b2 = createBar(() => target(h2).page.page_id, h2.hub, () => h2.text()).el;
    expect(b2.querySelector(".ew-chip")!.textContent).toBe("从资料重写");
    expect(createBar(() => "other", h.hub, () => h.text()).el.childNodes.length).toBe(0);
    // a split showing another note (no marks there) never gets 确认替换 / 取消
    expect(createBar(() => pid, h.hub, () => "## 别的笔记\n正文\n").el.childNodes.length).toBe(0);
    expect(createBar(() => pid, h.hub, () => null).el.childNodes.length).toBe(0);
  });

  it("a stale draft (it cannot go in as it is): no sentence and no 确认替换, only 取消 and a quiet 重试 that writes it again", async () => {
    const h = await withDraft({}, { stale: true });
    const bar = createBar(() => target(h).page.page_id, h.hub, () => h.text());
    const b = bar.el.querySelector(".ew-revbar")!;
    expect(b.textContent).not.toContain("改过");
    expect([...b.querySelectorAll(".ew-rb-acts button")].map((x) => x.textContent)).toEqual(["取消", "重试"]);
    expect(b.querySelector("[data-op=approve]")).toBeNull();
    b.querySelector<HTMLButtonElement>("[data-op=redraft]")!.click();
    await flush();
    expect(h.api.calls.filter((x) => x.method === "botRedraft").map((x) => x.args)).toEqual([["rv1"]]);
    bar.update();                                          // the ledger read again: the draft fits the page now
    expect([...bar.el.querySelectorAll(".ew-rb-acts button")].map((x) => x.textContent)).toEqual(["确认替换", "取消"]);
    // 取消 still rejects a stale one
    const h2 = await withDraft({}, { stale: true });
    createBar(() => target(h2).page.page_id, h2.hub, () => h2.text()).el.querySelector<HTMLButtonElement>("[data-op=reject]")!.click();
    await flush();
    expect(h2.api.calls.filter((x) => x.method === "botDecide").map((x) => x.args)).toEqual([["rv1", "reject"]]);
  });

  it("bar ?: citations the check removed or marked are counted (有 N 处出处对不上，已去掉或标出), nothing when none", async () => {
    const h = await withDraft({}, { cite_issues: 2 });
    const b = createBar(() => target(h).page.page_id, h.hub, () => h.text()).el.querySelector(".ew-revbar")!;
    expect([...b.querySelectorAll(".ew-tipbox > span")].map((x) => x.textContent)).toEqual(["你让我按《线性代数讲义》改这一页", "用了《线性代数讲义》", "有 2 处出处对不上，已去掉或标出", "确认前这一页不变"]);
  });

  it("in a Markdown view the bar sits right under the header (Live Preview and Reading view), and the header stays put", async () => {
    const h = await withDraft();
    const pid = target(h).page.page_id;
    const view = document.createElement("div");
    const div = (cls: string, ...kids: HTMLElement[]): HTMLElement => { const d = document.createElement("div"); d.className = cls; d.append(...kids); return d; };
    view.append(div("markdown-source-view", div("cm-sizer", div("cm-contentContainer"))),
      div("markdown-reading-view", div("markdown-preview-view", div("markdown-preview-sizer", div("mod-header"), div("b1")))));
    document.body.append(view);
    const info = { pageId: "p", title: "T", goal: "", updated: "" };
    applyHeader(view, info);
    const stop = watchReviseBar(view, () => pid, h.hub, () => h.text());
    const lp = [...view.querySelector(".cm-sizer")!.children].map((c) => c.className);
    expect(lp).toEqual(["ew ew-inline evowiki-note-header", "ew ew-inline evowiki-revbar-host", "cm-contentContainer"]);
    const rd = [...view.querySelector(".markdown-preview-sizer")!.children].map((c) => c.className);
    expect(rd).toEqual(["mod-header", "ew ew-inline evowiki-revbar-host", "b1"]);
    applyHeader(view, info);
    expect([...view.querySelector(".cm-sizer")!.children].map((c) => c.className)).toEqual(lp);
    expect(view.querySelectorAll(".ew-revbar")).toHaveLength(2);
    stop();
    expect(view.querySelectorAll(".evowiki-revbar-host")).toHaveLength(0);
  });

  it("确认替换: approve (no draft body) → the note sync writes the page the server now has; 本页已按…更新 · 撤销 → undo", async () => {
    const h = await withDraft();
    const { page, sec } = target(h);
    const before = h.text();
    const runs = h.sync.runs.length;
    // what the server has after the swap
    const swapped: PageOut = { ...page, sections: page.sections.map((x) => (x.id === sec.id ? { ...x, body: `${NEW_LINE}\n\n⟦和资料不同：${NOTE}⟧` } : x)) };
    const peek = vi.spyOn(h.api, "getPage");
    const bar = createBar(() => page.page_id, h.hub, () => h.text());
    expect(h.text()).toBe(before);
    peek.mockImplementation(() => Promise.resolve(structuredClone(swapped)));
    bar.el.querySelector<HTMLButtonElement>("[data-op=approve]")!.click();
    await flush();
    expect(h.api.calls.filter((x) => x.method === "botDecide").map((x) => x.args)).toEqual([["rv1", "approve"]]);
    expect(h.sync.runs.length).toBe(runs + 1);
    expect(h.text()).toContain(NEW_LINE);
    expect(bar.el.childNodes.length).toBe(0);
    expect(pendingRevise(h.c.state, page.page_id)).toBeNull();
    expect(appliedRevise(h.c.state, page.page_id)?.action_id).toBe("rv1");
    // the top line
    const top = createTopLine(page.page_id, h.hub);
    const line = top.el.querySelector(".ew-topline")!;
    expect(line.textContent).toBe("本页已按《线性代数讲义》更新 1 节 · 撤销");
    peek.mockImplementation(() => Promise.resolve(structuredClone(page)));
    line.querySelector<HTMLButtonElement>(".ew-lk")!.click();
    await flush();
    expect(h.api.calls.filter((x) => x.method === "botDecide").map((x) => x.args)).toEqual([["rv1", "approve"], ["rv1", "undo"]]);
    expect(h.notices).toContain("已恢复到修改前的版本");
    expect(h.text()).not.toContain(NEW_LINE);
    top.update();
    expect(top.el.querySelector(".ew-topline")).toBeNull();
  });

  it("活动记录's 撤销 on a confirmed draft goes the same way (botDecide → reviseDecide: the note follows)", async () => {
    const h = await withDraft({ status: "executed", can_undo: true, decided_at: "2026-10-21T08:05:00Z" });
    const runs = h.sync.runs.length;
    const peek = vi.spyOn(h.api, "getPage");
    const { page, sec } = target(h);
    peek.mockImplementation(() => Promise.resolve({ ...structuredClone(page), sections: page.sections.map((x) => (x.id === sec.id ? { ...x, body: "撤销后的样子" } : x)) }));
    await h.c.botDecide("rv1", "undo");
    expect(h.api.calls.filter((x) => x.method === "botDecide").map((x) => x.args)).toEqual([["rv1", "undo"]]);
    expect(h.sync.runs.length).toBe(runs + 1);
  });
});

describe("a refused undo of an in-page draft", () => {
  it("409 from the ledger: the notice is the executor's reason (e.g. no practice writer), nothing synced", async () => {
    const h = await withDraft({ status: "executed", can_undo: true, decided_at: "2026-10-21T08:05:00Z" });
    const runs = h.sync.runs.length;
    h.api.failNext("botDecide", new ApiError(409, "加练习的写作还没有接上", "action_refused"));
    await h.c.botDecide("rv1", "undo");
    expect(h.notices[h.notices.length - 1]).toBe("加练习的写作还没有接上");
    expect(h.sync.runs.length).toBe(runs);
  });
});

describe("在页面里看 (the chat's card, 今天's 去确认)", () => {
  it("在页面里看 opens the page at the first marked section", async () => {
    const h = await withDraft();
    const opened = h.opened.length;
    await h.c.openRevise(target(h).page.page_id);
    expect(h.opened.length).toBeGreaterThan(opened);
    const at = h.locators[h.locators.length - 1];
    expect(typeof at).toBe("function");
    // the first changed section's own heading line
    expect(at!(h.text())).toBe(h.text().split("\n").indexOf(`## ${target(h).sec.heading}`));
  });
});

describe("fix round 1", () => {
  it("确认替换 whose note write fails: the page waits in 待同步 and a notice says 本页笔记更新失败 with 重试 (fail closed)", async () => {
    const h = await withDraft();
    const { page, sec } = target(h);
    const swapped: PageOut = { ...page, sections: page.sections.map((x) => (x.id === sec.id ? { ...x, body: NEW_LINE } : x)) };
    const peek = vi.spyOn(h.api, "getPage").mockImplementation(() => Promise.reject(new Error("down")));
    await h.c.reviseDecide("rv1", "approve");
    expect(h.text()).not.toContain(NEW_LINE);
    const n = h.actionNotices[h.actionNotices.length - 1]!;
    expect(n.text).toBe("本页笔记更新失败");
    expect(n.actions.map((x) => x.label)).toEqual(["重试"]);
    expect((h.c as unknown as { notes: { pendingSync: Set<string> } }).notes.pendingSync.has(page.page_id)).toBe(true);
    peek.mockImplementation(() => Promise.resolve(structuredClone(swapped)));
    n.actions[0]!.run();
    await flush();
    expect(h.text()).toContain(NEW_LINE);
  });

  it("确认替换 whose note sync was only deferred (another write started meanwhile): no failure notice; it syncs when the page lands", async () => {
    const h = await withDraft();
    const { page } = target(h);
    const { core, notes } = h.c as unknown as { core: { writeEpoch: number }; notes: { pendingSync: Set<string> } };
    vi.spyOn(h.api, "getPage").mockImplementation(() => { core.writeEpoch++; return Promise.resolve(structuredClone(page)); });
    const before = h.actionNotices.length;
    await h.c.reviseDecide("rv1", "approve");
    expect(h.actionNotices.length).toBe(before);
    expect(h.notices).not.toContain("本页笔记更新失败");
    expect(notes.pendingSync.has(page.page_id)).toBe(true);
  });

  it("after 确认替换 the changed section's heading says 已更新; 撤销 takes it away", async () => {
    const h = await withDraft();
    const { page, sec } = target(h);
    const ref = { pageId: page.page_id, heading: sec.heading, ordinal: 0 };
    expect(headingView(h.c.state, ref).revised).toBe(false);
    vi.spyOn(h.api, "getPage").mockImplementation(() => Promise.resolve(structuredClone(page)));
    await h.c.reviseDecide("rv1", "approve");
    expect(headingView(h.c.state, ref).revised).toBe(true);
    const parts = createHeadingParts(ref, h.hub, vi.fn());
    expect(parts.inline.querySelector("[data-role=revised]")!.textContent).toBe("已更新");
    await h.c.reviseDecide("rv1", "undo");
    expect(headingView(h.c.state, ref).revised).toBe(false);
    parts.update();
    expect(parts.inline.querySelector("[data-role=revised]")).toBeNull();
  });

  it("an added paragraph after a kept paragraph the note no longer has: the section is shown whole, never out of order", async () => {
    const h = await withDraft();
    const { sec } = target(h);
    const old = paragraphs(sec.body);
    expect(old.length).toBeGreaterThanOrEqual(2);
    h.api.putBotAction(draftRow(h, {}, { sections: [{ section_id: sec.id, heading: sec.heading, old_heading: sec.heading, status: "changed",
      old_body: sec.body, new_body: [...old, NEW_LINE].join("\n\n") }] }));
    await h.c.botLedgerChanged();
    const pid = target(h).page.page_id;
    // as written: the new paragraph goes after the last one
    const plain = revisePlan(h.c.state, pid, h.text())!;
    expect(plain.pieces.map((x) => x.at.kind)).toEqual(["after"]);
    // the last kept paragraph was edited by hand in the note: its place is not known → the whole section in marked text
    const edited = h.text().replace(old[old.length - 1]!, "完全不同的一段内容，和原来的说法没有一点关系。");
    const plan = revisePlan(h.c.state, pid, edited)!;
    expect(plan.pieces).toHaveLength(1);
    const p = plan.pieces[0]!;
    expect(p.at.kind).toBe("replace");
    expect(p.parts[p.parts.length - 1]).toEqual({ kind: "p", old: null, new: NEW_LINE });
  });

  it("Reading view: a block that runs past the replaced lines is hidden whole (never old text next to the marks)", async () => {
    const h = await withDraft();
    const text = h.text();
    const pid = target(h).page.page_id;
    const rep = revisePlan(h.c.state, pid, text)!.pieces.find((x) => x.at.kind === "replace")!;
    if (rep.at.kind !== "replace") throw new Error("no replace");
    const block = document.createElement("div");
    block.append(Object.assign(document.createElement("p"), { textContent: "旧的" }));
    mountReadingBlock(block, pid, () => ({ text, lineStart: rep.at.kind === "replace" ? rep.at.from : 0, lineEnd: (rep.at.kind === "replace" ? rep.at.to : 0) + 2 }), h.hub, render);
    expect(block.classList.contains("evowiki-rv-off")).toBe(true);
    expect(block.querySelector(".evowiki-trk-host .ew-trk")).not.toBeNull();
    // a block that only overlaps a replaced range from below is hidden too
    const tail = document.createElement("div");
    mountReadingBlock(tail, pid, () => ({ text, lineStart: rep.at.kind === "replace" ? rep.at.to : 0, lineEnd: (rep.at.kind === "replace" ? rep.at.to : 0) + 1 }), h.hub, render);
    expect(tail.classList.contains("evowiki-rv-off")).toBe(true);
  });

  it("活动记录's 撤销 of a confirmed draft: the ledger's undo (not the RSI undo), the buttons are off while it is out, then the note follows", async () => {
    const h = await withDraft({ status: "executed", can_undo: true, decided_at: "2026-10-21T08:05:00Z" });
    const { page } = target(h);
    vi.spyOn(h.api, "getPage").mockImplementation(() => Promise.resolve({ ...structuredClone(page), sections: page.sections.map((x, i) => (i === 0 ? { ...x, body: "撤销后" } : x)) }));
    const real = h.api.botDecide.bind(h.api);
    let during: unknown = null;
    vi.spyOn(h.api, "botDecide").mockImplementation((id, op) => { during = h.c.state.busy; return real(id, op); });
    const runs = h.sync.runs.length;
    await h.c.undoPatch("rv1");
    expect(during).toEqual({ what: "rsi", sid: "rv1", abortable: false });
    expect(h.c.state.busy).toBeNull();
    expect(h.api.calls.filter((x) => x.method === "undoPatch")).toHaveLength(0);
    expect(h.sync.runs.length).toBe(runs + 1);
  });
});

describe("live check 2026-10-21 fixes", () => {
  const S1_NEW = "身高用厘米、体重用千克，两列数字不能直接比较。**标准化**把每个数换成“离均值几个标准差”〔线性代数讲义.pdf p.3｜3〕，不同量纲就落到同一把尺子上。";

  async function openingDraft(source: ActionOut["source"] = "bot") {
    const h = await harness();
    const p = h.c.state.page;
    if (p.kind !== "open") throw new Error("no page");
    const s1 = p.page.sections[0]!;
    expect(s1.role).toBe("organizer");
    h.api.putBotAction({ ...draftRow(h, { source }), revise: { ...draftRow(h).revise!, sections: [{ section_id: s1.id, heading: "新的一句话结论",
      old_heading: s1.heading, status: "changed", old_body: s1.body, new_body: S1_NEW }] } });
    await h.c.botLedgerChanged();
    return { h, s1, pid: p.page.page_id };
  }

  it("V2: the page's opening section (no ## line in the note) gets its marks; nothing else is hidden", async () => {
    const { h, pid } = await openingDraft();
    const lines = h.text().split("\n");
    const plan = revisePlan(h.c.state, pid, h.text())!;
    expect(plan.pieces).toHaveLength(1);
    const at = plan.pieces[0]!.at;
    const line = lines.findIndex((l) => l.startsWith("身高用厘米"));
    expect(at).toEqual({ kind: "replace", from: line, to: line });
    // a renamed opening heading is not in the note: nothing to mark for it
    expect(plan.pieces[0]!.parts.map((x) => x.kind)).toEqual(["p"]);
    const blocks = reading(h);
    await flush();
    const off = blocks.filter((b) => b.classList.contains("evowiki-rv-off"));
    expect(off).toHaveLength(1);
    expect(off[0]!.textContent).toContain("身高用厘米");
    expect(blocks.find((b) => b.textContent?.includes("z 的公式"))!.classList.contains("evowiki-rv-off")).toBe(false);
  });

  it("V3 / V4: the marked paragraph keeps its Markdown (bold) and a material citation is drawn as the page draws it", async () => {
    const { h } = await openingDraft();
    const blocks = reading(h);
    await flush();
    const trk = blocks.flatMap((b) => [...b.querySelectorAll<HTMLElement>(".evowiki-trk-host .ew-trk")])[0]!;
    expect(trk.querySelector("p strong")!.textContent).toBe("标准化");
    expect(trk.querySelector("del")!.textContent).toContain("没法直接比");
    expect([...trk.querySelectorAll("ins")].map((x) => x.textContent).join("")).toContain("不能直接比较");
    // the citation is the note writer's text for it (the draft's material has no file: plain 名 p.N), never 〔…〕
    expect(trk.textContent).toContain("线性代数讲义.pdf p.3");
    expect(trk.textContent).not.toContain("〔");
    expect(h.text()).not.toContain("不能直接比较");
  });

  it("V3: lists, inline code and math keep their elements; a code block is shown old struck, new added", async () => {
    const one = async (a: string | null, b: string | null): Promise<HTMLElement> => {
      const box = renderedPart(a, b, render);
      await flush();
      return box;
    };
    const list = await one("- 第 1 行 [2, 1] 点乘：`2×1` 得 **2**\n- 第 2 行 $\lambda$ 不变", "- 第 1 行 [1, 2] 点乘：`1×1` 得 **1**\n- 第 2 行 $\lambda$ 不变");
    expect(list.querySelectorAll("ul > li")).toHaveLength(2);
    expect(list.textContent).not.toContain("- ");
    expect(list.querySelector("li code")).not.toBeNull();
    expect(list.querySelector("li strong")).not.toBeNull();
    expect(list.querySelector("li .math")!.textContent).toBe("\lambda");
    expect(list.querySelector("li del")).not.toBeNull();
    expect(list.querySelector("li ins")).not.toBeNull();
    // an inline formula that changed: the old one struck (a copy), the new one added, both still formulas
    const math = await one("所以 $x=1$ 成立", "所以 $x=2$ 成立");
    expect(math.querySelector("del .math")!.textContent).toBe("x=1");
    expect(math.querySelector("ins .math")!.textContent).toBe("x=2");
    const code = await one("```\nA = [[2, 1],\n     [1, 2]]\n```", "```\nA = [[1, 2],\n     [2, 1]]\n```");
    // a code block is marked inside, like text (follow-up 2026-10-22): one block, its dropped / added characters marked
    expect(code.querySelector(".ew-delblk, .ew-insblk")).toBeNull();
    expect(code.querySelectorAll("pre")).toHaveLength(1);
    const side = (drop: string): string => [...code.querySelector("pre code")!.childNodes].map((n) => (n as Element).tagName === drop ? "" : n.textContent).join("");
    expect(side("DEL")).toBe("A = [[1, 2],\n     [2, 1]]");
    expect(side("INS")).toBe("A = [[2, 1],\n     [1, 2]]");
    // added / dropped / kept paragraphs
    expect((await one(null, "新的")).querySelector(".ew-insblk p")!.textContent).toBe("新的");
    expect((await one("旧的", null)).querySelector(".ew-delblk p")!.textContent).toBe("旧的");
    const kept = await one("同样 **一段**", "同样 **一段**");
    expect(kept.querySelector("del, ins")).toBeNull();
    expect(kept.querySelector("strong")).not.toBeNull();
  });

  it("a block Obsidian post-processes twice shows its marks once", async () => {
    const { h, pid } = await openingDraft();
    const text = h.text();
    const line = text.split("\n").findIndex((l) => l.startsWith("身高用厘米"));
    const block = document.createElement("div");
    const info = () => ({ text, lineStart: line, lineEnd: line });
    mountReadingBlock(block, pid, info, h.hub, render);
    mountReadingBlock(block, pid, info, h.hub, render);
    await flush();
    expect(block.querySelectorAll(".evowiki-trk-host")).toHaveLength(1);
    expect(block.classList.contains("evowiki-rv-off")).toBe(true);
  });

  it("an old mount's late cleanup never undoes the newer mount; cleanups are idempotent and unload the renderer's owner", async () => {
    const { h, pid } = await openingDraft();
    const text = h.text();
    const line = text.split(NL).findIndex((l) => l.startsWith("身高用厘米"));
    const block = document.createElement("div");
    const info = () => ({ text, lineStart: line, lineEnd: line });
    const unload = vi.spyOn(Component.prototype, "unload");
    const first = mountReadingBlock(block, pid, info, h.hub, render);
    const second = mountReadingBlock(block, pid, info, h.hub, render);
    expect(unload).toHaveBeenCalledTimes(1);          // the first mount's owner, when the second replaced it
    first();
    first();
    await flush();
    expect(block.classList.contains("evowiki-rv-off")).toBe(true);
    expect(block.querySelectorAll(".evowiki-trk-host")).toHaveLength(1);
    expect(unload).toHaveBeenCalledTimes(1);
    second();
    second();
    expect(block.classList.contains("evowiki-rv-off")).toBe(false);
    expect(unload).toHaveBeenCalledTimes(2);
    unload.mockRestore();
  });

  it("Live Preview: the renderer's owner follows the widget DOM, so destroy on an equal widget instance unloads it once", () => {
    const unload = vi.spyOn(Component.prototype, "unload");
    const parts = [{ kind: "p" as const, old: "旧", new: "新" }];
    const dom = new TrackWidget("k", parts, render).toDOM();
    const other = new TrackWidget("k", parts, render);
    other.destroy(dom);
    other.destroy(dom);
    expect(unload).toHaveBeenCalledTimes(1);
    unload.mockRestore();
  });

  it("V5: the ? says who asked for the draft: Bot (the material covers this page), the chat (你让我按…), else neutral", async () => {
    const bar = async (source: ActionOut["source"]): Promise<string | null> => {
      const { h, pid } = await openingDraft(source);
      return createBar(() => pid, h.hub, () => h.text()).el.querySelector(".ew-tipbox > span")!.textContent;
    };
    expect(await bar("bot")).toBe("《线性代数讲义》讲到了这一页已有的内容");
    expect(await bar("assistant")).toBe("你让我按《线性代数讲义》改这一页");
    expect(await bar("rsi")).toBe("按《线性代数讲义》改这一页");
  });
});

describe("follow-up 2026-10-22: blocks that differ only in whitespace", () => {
  const F = "```";
  const one = async (a: string | null, b: string | null): Promise<HTMLElement> => {
    const box = renderedPart(a, b, render);
    await flush();
    return box;
  };

  it("a code block / display math re-spaced is shown unmarked; a real change in a code block is marked inside it", async () => {
    const code = await one(`${F}\nA = [[2, 1],\n     [1, 2]]\n${F}`, `${F}\nA = [[2,1],\n  [1,2]]\n${F}`);
    expect(code.querySelector(".ew-delblk, .ew-insblk, del, ins")).toBeNull();
    expect(code.querySelectorAll("pre")).toHaveLength(1);
    expect(code.querySelector("pre")!.textContent).toBe("A = [[2,1],\n  [1,2]]");
    const math = await one("$$ x = 1 $$", "$$x=1$$");
    expect(math.querySelector(".ew-delblk, .ew-insblk")).toBeNull();
    expect(math.querySelector(".math-block")).not.toBeNull();
    // the live run's (A - λI) → (A − λI) is a real change (U+2212 minus): only the minus is marked, inside the code block
    const real = await one(`${F}\n(A - λI) v = 0\n${F}`, `${F}\n(A − λI) v = 0\n${F}`);
    expect(real.querySelector(".ew-delblk, .ew-insblk")).toBeNull();
    expect([...real.querySelectorAll("pre code del")].map((x) => x.textContent)).toEqual(["-"]);
    expect([...real.querySelectorAll("pre code ins")].map((x) => x.textContent)).toEqual(["−"]);
    expect(real.querySelector("pre code")!.textContent).toBe("(A -− λI) v = 0");
    // what cannot be marked inside stays whole: changed display math, a code block in place of text
    const dm = await one("$$x=1$$", "$$x=2$$");
    expect([...dm.children].map((x) => x.className)).toEqual(["ew-delblk", "ew-insblk"]);
    const grew = await one("A v = 0", `${F}\nA v = λ v\n${F}`);
    expect([...grew.children].map((x) => x.className)).toEqual(["ew-delblk", "ew-insblk"]);
  });

  it("paragraphs pair whitespace-insensitively, so a re-spaced code block next to a changed paragraph is kept as it is", () => {
    const oldP = paragraphs(`旧的一句。\n\n${F}\nA = [[2, 1],\n     [1, 2]]\n${F}`);
    const newP = paragraphs(`新的一句。\n\n${F}\nA = [[2,1],\n [1,2]]\n${F}`);
    expect(paraDiff(oldP, newP)).toEqual([{ kind: "change", i: 0, j: 0 }, { kind: "same", i: 1, j: 1 }]);
  });
});

// ---- the assistant's practice draft (add_practice): the drafted block in the page before 确认替换 ----

const BLOCK_FENCE = ["```evoui", "{", " \"type\": \"evoui\",", " \"title\": \"配对：矩阵和它的特征值\"", "}", "```"].join("\n");

function practiceRow(h: Harness, over: Partial<ActionOut> = {}): ActionOut {
  const { page, sec } = target(h);
  return draftRow(h, { action_id: "pr1", kind: "page_practice", summary: `给「${page.title}」加交互`, why: "先自己配一配", ...over }, {
    run_id: null, sources: [], practice: "interactive",
    sections: [{ section_id: sec.id, heading: sec.heading, old_heading: sec.heading, status: "changed", old_body: sec.body,
      new_body: `${sec.body.trimEnd()}\n\n${BLOCK_FENCE}` }],
  });
}

describe("the assistant's practice draft in the page", () => {
  it("a scoped visual revision fetches its image and EvoUI preview with the correct asset kinds before confirmation", async () => {
    const retrieveAll = vi.fn(() => Promise.resolve());
    const h = await harness({ assets: { retrieveAll } });
    retrieveAll.mockClear();
    const { sec } = target(h);
    const picture = ["```evowiki-image", "asset: ia_picture", "alt: 均值示意图", "title: 均值", "license: CC0",
      "source: Commons", "source_url: https://commons.wikimedia.org/wiki/File:Mean.png", "width: 640", "height: 480", "```"].join("\n");
    const app = "```evoui\nasset: ia_visual\n```";
    const before = h.text();
    h.api.putBotAction(draftRow(h, {}, { sources: [], sections: [{ section_id: sec.id, heading: sec.heading,
      old_heading: sec.heading, status: "changed", old_body: sec.body, new_body: `${sec.body}\n\n${picture}\n\n${app}` }] }));
    await h.c.botLedgerChanged();
    await flush();
    expect(retrieveAll).toHaveBeenCalledWith(["ia_picture"], "image");
    expect(retrieveAll).toHaveBeenCalledWith(["ia_visual"], "evoui");
    const plan = revisePlan(h.c.state, target(h).page.page_id, h.text())!;
    expect(plan.pieces.flatMap((p) => p.parts).some((p) => p.kind === "p" && p.new?.includes("evowiki-image"))).toBe(true);
    expect(h.text()).toBe(before);
  });
  it("the block is an added part after its section, drawn by the note's own renderer; the note is not touched", async () => {
    const h = await harness();
    h.api.putBotAction(practiceRow(h));
    await h.c.botLedgerChanged();
    const before = h.text();
    const plan = revisePlan(h.c.state, target(h).page.page_id, h.text())!;
    expect(plan.actionId).toBe("pr1");
    const parts = plan.pieces.flatMap((x) => x.parts);
    expect(parts).toEqual([{ kind: "p", old: null, new: BLOCK_FENCE }]);
    expect(plan.pieces[0]!.at.kind).toBe("after");
    const blocks = reading(h);
    await flush();
    const ins = blocks.flatMap((b) => [...b.querySelectorAll(".evowiki-trk-host .ew-trk.ew-prac .ew-insblk")]);
    expect(ins).toHaveLength(1);
    expect(ins[0]!.querySelector("pre code")!.textContent).toContain("配对：矩阵和它的特征值");
    expect(blocks.some((b) => b.classList.contains("evowiki-rv-off"))).toBe(false);     // nothing of the page is hidden
    expect(h.text()).toBe(before);
  });

  it("bar: 加了一个交互 + ? (the reason, 确认前这一页不变) + 确认替换 / 取消; 确认替换 approves and the note follows", async () => {
    const h = await harness();
    h.api.putBotAction(practiceRow(h));
    await h.c.botLedgerChanged();
    const pid = target(h).page.page_id;
    const b = createBar(() => pid, h.hub, () => h.text()).el.querySelector(".ew-revbar")!;
    expect(b.querySelector(".ew-rb-t")!.textContent).toBe("加了一个交互");
    expect([...b.querySelectorAll(".ew-tipbox > span")].map((x) => x.textContent)).toEqual(["先自己配一配", "确认前这一页不变"]);
    expect([...b.querySelectorAll(".ew-rb-acts button")].map((x) => x.textContent)).toEqual(["确认替换", "取消"]);
    const runs = h.sync.runs.length;
    const { page, sec } = target(h);
    const added: PageOut = { ...page, sections: page.sections.map((x) => (x.id === sec.id ? { ...x, body: `${x.body}\n\n${BLOCK_FENCE}` } : x)) };
    vi.spyOn(h.api, "getPage").mockImplementation(() => Promise.resolve(structuredClone(added)));
    b.querySelector<HTMLButtonElement>("[data-op=approve]")!.click();
    await flush();
    expect(h.api.calls.filter((x) => x.method === "botDecide").map((x) => x.args)).toEqual([["pr1", "approve"]]);
    expect(h.sync.runs.length).toBe(runs + 1);              // the note now carries the block
    expect(h.text()).toContain("配对：矩阵和它的特征值");
  });

  it("a practice draft shown in the page is not a wait bar; a practice row without a draft is one (确认 / 取消 on the page)", async () => {
    const h = await harness();
    const row = practiceRow(h);
    const st = (r: typeof row) => ({ ...h.c.state, bot: { ...h.c.state.bot, actions: { kind: "ready" as const, value: [r] } } });
    expect(pageWaitActions(st(row), row.page_id)).toEqual([]);
    expect(pageWaitActions(st({ ...row, revise: null }), row.page_id).map((x) => x.action_id)).toEqual(["pr1"]);
    expect(barTitle({ ...row.revise!, practice: "card" })).toBe("加了记忆卡");
  });
});

// ---- one decision in two places: the page's bar and the chat's card (revise/decisions.ts) ----

describe("the page's bar and the chat's card are one decision", () => {
  const bar = (h: Harness): HTMLElement | null => createBar(() => target(h).page.page_id, h.hub, () => h.text()).el.querySelector<HTMLElement>(".ew-revbar");
  const acts = (b: HTMLElement): string[] => [...b.querySelectorAll<HTMLButtonElement>(".ew-rb-acts button")].map((x) => (x.textContent ?? "") + (x.disabled ? "(off)" : ""));
  async function practiceHarness(decisions: DraftDecisions): Promise<Harness> {
    const h = await harness({ decisions });
    h.api.putBotAction(practiceRow(h));
    await h.c.botLedgerChanged();
    return h;
  }

  it("确认替换 on the bar while the server is down: no notice, only a quiet 请重试 after the buttons (the card sees it too); 请重试 sends it again", async () => {
    const decisions = new DraftDecisions();
    const h = await practiceHarness(decisions);
    const spy = vi.spyOn(h.api, "botDecide").mockRejectedValueOnce(new OfflineError());
    bar(h)!.querySelector<HTMLButtonElement>("[data-op=approve]")!.click();
    await flush();
    expect(acts(bar(h)!)).toEqual(["确认替换", "取消", "请重试"]);
    expect(h.notices).toEqual([]);
    expect(decisions.get("pr1").failed).toBe("approve");
    bar(h)!.querySelector<HTMLButtonElement>("[data-op=retry]")!.click();
    await flush();
    expect(spy.mock.calls.map((x) => x[1])).toEqual(["approve", "approve"]);
    expect(decisions.get("pr1").settled?.status).toBe("executed");
    expect(bar(h)).toBeNull();
  });

  it("offline (the connection already knows): nothing is sent, 请重试 at once", async () => {
    const decisions = new DraftDecisions();
    const h = await practiceHarness(decisions);
    h.conn.emit("offline");
    bar(h)!.querySelector<HTMLButtonElement>("[data-op=reject]")!.click();
    await flush();
    expect(h.api.calls.filter((x) => x.method === "botDecide")).toEqual([]);
    expect(acts(bar(h)!)).toEqual(["确认替换", "取消", "请重试"]);
    expect(decisions.get("pr1").failed).toBe("reject");
  });

  it("decided from the card: the bar waits while it is out and is gone the moment it went through (before the next ledger read)", async () => {
    const decisions = new DraftDecisions();
    const h = await practiceHarness(decisions);
    decisions.start("pr1", "approve");
    expect(acts(bar(h)!)).toEqual(["确认替换(off)", "取消(off)"]);
    decisions.settle("pr1", { ...practiceRow(h), status: "executed" });
    expect(bar(h)).toBeNull();
    // the bar's own click while the card's decision is out sends nothing
    const d2 = new DraftDecisions();
    const h2 = await practiceHarness(d2);
    d2.start("pr1", "reject");
    bar(h2)!.querySelector<HTMLButtonElement>("[data-op=approve]")!.click();
    await flush();
    expect(h2.api.calls.filter((x) => x.method === "botDecide")).toEqual([]);
  });
});
