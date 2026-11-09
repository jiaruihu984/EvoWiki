import { setUiLanguage } from "../../i18n";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderEngine } from "../render-engine";
import { fakeActions } from "../render-test-kit";
import type { SourceOut, SourceProgress } from "../schema";
import { INITIAL_PERSONAL, INITIAL_SOURCE_UI, INITIAL_WIKI_STATE, type WikiState } from "../state";
import { progressOf, statusText, sourcesSection, updateSourceCard } from "./render-sources";
import { pageRanges, SOURCES_COPY } from "./sources-copy";

afterEach(() => { document.body.replaceChildren(); vi.useRealTimers(); setUiLanguage("zh"); });

const src = (o: Partial<SourceOut> = {}): SourceOut => ({
  id: "s1", title: "线性代数讲义.pdf", kind: "pdf", pages: 86, status: "reading", error: null, error_code: null, unreadable_pages: [], progress: null,
  note: null, origin: null, created_at: "2026-10-21T00:00:00Z", deleted: false, uses: [], can_retry: false, ...o,
});
const prog = (done: number, total = 12): SourceProgress => ({ stage: "embed", done, total, message: `正在读 ${done}/${total} 章` });
const state = (items: SourceOut[], o: Partial<WikiState> = {}): WikiState => ({
  ...INITIAL_WIKI_STATE, conn: "online", sources: { kind: "ready", value: items }, sourceUi: { ...INITIAL_SOURCE_UI, notes: {} },
  personal: { ...INITIAL_PERSONAL, sources: true }, ...o });
const draw = (s: WikiState): { root: HTMLElement; a: ReturnType<typeof fakeActions> } => {
  const a = fakeActions();
  const root = document.createElement("section");
  sourcesSection(root, s, a);
  document.body.append(root);
  return { root, a };
};
const q = <T extends Element>(root: ParentNode, sel: string): T => {
  const e = root.querySelector<T>(sel);
  if (e === null) throw new Error(`no ${sel}`);
  return e;
};

describe("个性化 · 资料 (the approved demo's sourcesBox / sourcesSection)", () => {
  it("is a .ew-pz-box headed 资料 after 活动记录, the last box (the settings are in 设置 → EvoWiki), with no caption under the heading", () => {
    const root = renderEngine(state([src()]), fakeActions());
    const sec = q<HTMLElement>(root, ".ew-pz-box > #sources");
    const box = sec.parentElement!;
    expect(box.querySelector(":scope > .ew-pz-entry > b")?.textContent).toBe(SOURCES_COPY.title);
    // 活动记录 is one card (.ew-pz-logbox) whose title row is the #pz-log toggle
    expect(box.previousElementSibling?.querySelector(":scope.ew-pz-logbox > #pz-log")).not.toBeNull();
    expect(box.nextElementSibling).toBeNull();
    expect(sec.children[0]?.className).toBe("ew-src-list");
  });

  it("folds like 活动记录 (app.js sourcesBox): folded by default, the bold 资料 and ›; opened, the list 10px under it; empty too", () => {
    for (const items of [[src()], []]) {
      const a = fakeActions();
      const shut = renderEngine(state(items, { personal: INITIAL_PERSONAL }), a);
      const entryOf = (r: ParentNode): HTMLButtonElement | undefined =>
        [...r.querySelectorAll<HTMLButtonElement>(".ew-pz-box > .ew-pz-entry")].find((e) => e.textContent === "资料›");
      const ent = entryOf(shut)!;
      expect([ent.getAttribute("aria-expanded"), ent.parentElement!.children.length]).toEqual(["false", 2]);   // the fold and +
      expect(shut.querySelector("#sources, .ew-src-empty")).toBeNull();
      ent.click();
      expect(a.personalView).toHaveBeenCalledWith({ sources: true });
      const open = renderEngine(state(items), fakeActions());
      const inner = q<HTMLElement>(entryOf(open)!.parentElement!, ":scope > div");
      expect([inner.style.marginTop, entryOf(open)?.getAttribute("aria-expanded")]).toEqual(["10px", "true"]);
    }
  });

  it("empty: the one muted line 还没有资料; nothing to add here (no 添加资料, no drop zone)", () => {
    const { root } = draw(state([]));
    expect(q(root, ".ew-src-empty").textContent).toBe("还没有资料");
    expect(root.querySelector("#src-add, #src-drop, .ew-src-top")).toBeNull();
    for (const w of ["添加资料", "从库里选", "从电脑里选"]) expect(root.textContent).not.toContain(w);
  });

  it("a card is .chg.ci.src with the name and kind · pages, the status line and the three small buttons (no 用 buttons)", () => {
    const { root } = draw(state([src({ id: "s2", title: "机器学习导论.pptx", kind: "pptx", pages: 42, status: "ready", progress: prog(8, 8) })]));
    const c = q(root, "[data-src=s2]");
    expect(c.className).toBe("ew-chg ew-ci ew-src");
    expect(q(c, ".ew-ci-top .ew-ci-t.ew-src-t").textContent).toBe("机器学习导论.pptx PPTX · 42 页");
    expect(q(c, ".ew-ci-t .ew-faint").textContent).toBe("PPTX · 42 页");
    expect(q(c, ".ew-ci-s.ew-src-st span").textContent).toBe("读好了 · 8 节");
    expect(c.querySelector(".ew-rvbar")).toBeNull();
    expect([...c.querySelectorAll(":scope > .ew-row > button")].map((b) => `${b.className}|${b.textContent}`)).toEqual([
      "ew-btn ew-sm|打开", "ew-btn ew-sm|重命名", "ew-btn ew-sm|删除"]);
  });

  it("kind label comes from the kind: PDF / PPTX / DOCX / XLSX / MD / TXT / 图片; no page count shows the kind alone", () => {
    const { root } = draw(state([
      src({ id: "a", kind: "docx", pages: null, status: "ready" }), src({ id: "b", kind: "text", pages: null, status: "ready" }),
      src({ id: "c", kind: "image", pages: null, status: "ready" }), src({ id: "d", kind: "md", pages: null, status: "ready" }),
      src({ id: "e", kind: "xlsx", pages: null, status: "ready" }),
    ]));
    expect([...root.querySelectorAll(".ew-src-t .ew-faint")].map((e) => e.textContent)).toEqual(["DOCX", "TXT", "图片", "MD", "XLSX"]);
  });

  it("reading: the status comes from the typed stage and counts and a bar at the percent; failed shows the reason; partial the pages", () => {
    const s = state([
      src({ id: "r", status: "reading" }),
      src({ id: "f", status: "failed", error: "这个文件打不开（加了密码？）" }),
      src({ id: "p", status: "partial", unreadable_pages: [5, 6, 7, 8, 9, 10, 11, 12, 13, 14] }),
    ], { sourceProgress: { r: prog(3) } });
    const { root } = draw(s);
    expect(q(root, "[data-src=r] .ew-src-st span").textContent).toBe("正在读取章节 · 3/12");
    expect(q<HTMLElement>(root, "[data-src=r] .ew-rvbar i").style.width).toBe("25%");
    const f = q(root, "[data-src=f] .ew-src-st");
    expect(f.className).toBe("ew-ci-s ew-src-st ew-err");
    expect(f.textContent).toBe("这个文件打不开（加了密码？）");
    expect(f.parentElement?.querySelector(".ew-rvbar")).toBeNull();
    const p = q(root, "[data-src=p] .ew-src-st");
    expect(p.className).toBe("ew-ci-s ew-src-st ew-warn");
    expect(p.textContent).toBe("第 5–14 页读不出字");
    expect(pageRanges([3, 1, 2, 7])).toBe("第 1–3、7 页");
  });

  it("用在：lists the goal titles the client knows (a use with an unknown or no goal is left out); no line without uses", () => {
    const use = (goal_id: string | null) => ({ use: "learn" as const, goal_id, page_id: null, session_id: null, id: `u${String(goal_id)}`, created_at: "x" });
    const catalog = { kind: "ready", value: { paths: [{ path_id: "g1", goal: "学会 PCA", pages: 1, done: 0, next_index: null }],
      maps: [{ map_id: "m1", goal: "考研数学", regions: 3, expanded: 0, known: 0, next_region: null }] } } as never;
    const { root } = draw(state([src({ id: "a", status: "ready", uses: [use("g1"), use("m1"), use("g9"), use(null)] }), src({ id: "b", status: "ready" })], { catalog }));
    const line = [...q(root, "[data-src=a]").querySelectorAll(".ew-ci-s")].map((e) => e.textContent).filter((t) => t?.startsWith("用在"));
    expect(line).toHaveLength(1);
    expect(line[0]).toContain("学会 PCA");
    expect(line[0]).toContain("考研数学");
    expect(q(root, "[data-src=b]").textContent).not.toContain("用在");
  });

  it("a server note (a late error on a usable source) is shown on the card", () => {
    const { root } = draw(state([src({ status: "ready", note: "没有摘要：当前没有可用的模型" })]));
    expect(q(root, "[data-role=src-note]").textContent).toBe("没有摘要：当前没有可用的模型");
  });

  it("a progress item updates the reading card IN PLACE: text and bar change, the card node stays", () => {
    const x = src({ status: "reading" });
    const { root } = draw(state([x], { sourceProgress: { s1: prog(1) } }));
    const card = q(root, "[data-src=s1]");
    expect(updateSourceCard(root, x, prog(7))).toBe(true);
    expect(q(root, "[data-src=s1]")).toBe(card);
    expect(q(card, ".ew-src-st span").textContent).toBe("正在读取章节 · 7/12");
    expect(q<HTMLElement>(card, ".ew-rvbar i").style.width).toBe("58%");
    expect(updateSourceCard(root, src({ id: "nope" }), prog(1))).toBe(false);
    expect(progressOf(state([x], { sourceProgress: { s1: prog(2) } }), x)?.done).toBe(2);
    expect(statusText(x, null)).toBe(SOURCES_COPY.readingStart);
  });

  it("delete asks first: the warning line, 确认删除 (small primary) and 取消; nothing is deleted by the first click", () => {
    const { root, a } = draw(state([src({ status: "ready" })]));
    q<HTMLButtonElement>(root, ".ew-row button:nth-child(3)").click();
    expect(a.sourceAskDelete).toHaveBeenCalledWith("s1");
    expect(a.sourceConfirmDelete).not.toHaveBeenCalled();
    const asked = draw(state([src({ status: "ready" })], { sourceUi: { ...INITIAL_SOURCE_UI, confirm: "s1", notes: {} } }));
    const c = q(asked.root, "[data-src=s1]");
    expect(q(c, ".ew-src-warn").textContent).toBe("页面会保留，出处会显示（已删除）");
    expect([...c.querySelectorAll(":scope > .ew-row > button")].map((b) => `${b.className}|${b.textContent}`)).toEqual([
      "ew-btn ew-sm ew-primary|确认删除", "ew-btn ew-sm|取消"]);
    q<HTMLButtonElement>(c, ".ew-primary").click();
    expect(asked.a.sourceConfirmDelete).toHaveBeenCalledWith("s1");
  });

  it("rename: an input labelled 资料名字 with the draft, 保存 (small primary); Enter saves, Escape cancels, typing keeps the draft", () => {
    const { root, a } = draw(state([src()], { sourceUi: { ...INITIAL_SOURCE_UI, rename: "s1", renameDraft: "旧名字", notes: {} } }));
    const i = q<HTMLInputElement>(root, "#src-rename");
    expect(i.getAttribute("aria-label")).toBe("资料名字");
    expect(i.value).toBe("旧名字");
    expect(q(root, ".ew-ci-top button").className).toBe("ew-btn ew-sm ew-primary");
    i.value = "新名字";
    i.dispatchEvent(new Event("input"));
    expect(a.sourceSetRenameDraft).toHaveBeenCalledWith("新名字");
    i.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    expect(a.sourceCommitRename).toHaveBeenCalledOnce();
    i.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(a.sourceCancelRename).toHaveBeenCalledOnce();
  });

  it("a file dropped on the section is not taken (materials come in through the assistant)", () => {
    const { root } = draw(state([src({ status: "ready" })]));
    const over = new Event("dragover", { cancelable: true });
    root.dispatchEvent(over);
    expect(over.defaultPrevented).toBe(false);
  });

  it("writes are off offline, unconfigured and in the demo; 打开 stays on", () => {
    const { root } = draw(state([src({ status: "ready" })], { conn: "offline" }));
    const bs = [...root.querySelectorAll<HTMLButtonElement>("[data-src] .ew-row button")];
    expect(bs.map((b) => b.disabled)).toEqual([false, true, true]);
    const demo = [...draw(state([src({ status: "ready" })], { demo: true })).root.querySelectorAll<HTMLButtonElement>("[data-src] .ew-row button")];
    expect(demo.map((b) => b.disabled)).toEqual([false, true, true]);
  });

  it("a failed import offers 重试 on its card; a card that needs none does not", () => {
    const { root, a } = draw(state([src({ status: "failed", error: "读不出来", can_retry: true })]));
    const again = [...root.querySelectorAll<HTMLButtonElement>("[data-src] .ew-row button")].find((b) => b.textContent === SOURCES_COPY.retry);
    again?.click();
    expect(a.sourceRetryImport).toHaveBeenCalledWith("s1");
    const ready = draw(state([src({ status: "ready" })])).root;
    expect([...ready.querySelectorAll("[data-src] .ew-row button")].some((b) => b.textContent === SOURCES_COPY.retry)).toBe(false);
  });

  it("a failed card says its reason once", () => {
    const { root } = draw(state([src({ status: "failed", error: "读不出来", note: "读不出来", can_retry: true })]));
    expect(root.querySelector("[data-role=src-note]")).toBeNull();
  });

  it("a lost progress stream says so on the card with 重试; a failed list read offers 重试", () => {
    const { root, a } = draw(state([src()], { sourceUi: { ...INITIAL_SOURCE_UI, notes: { s1: { text: SOURCES_COPY.streamLost, retry: true } } } }));
    q<HTMLButtonElement>(root, "[data-action=src-retry]").click();
    expect(a.sourceRetryFollow).toHaveBeenCalledWith("s1");
    const bad = draw(state([], { sources: { kind: "error", message: "x" } }));
    expect(bad.root.querySelector("[data-action=sources-reload]")).not.toBeNull();
  });
});

describe("资料 card: the 真题 line (app.js srcExamLine)", () => {
  const ex = { state: "done" as const, items: 12, attached: 9, unattached: 3, pending: 0, unreadable: 2, incomplete: 0,
    unattached_items: ["第 4 题：行列式按行展开", "第 9 题：二次型的规范形"], detail: null };
  it("done: 抽出 12 道题 · 3 道没挂上 · 2 道读不清, the unattached ones in the ?; before 用在", () => {
    const { root } = draw(state([src({ status: "ready" })], { sourceExam: { s1: ex } }));
    const line = q<HTMLElement>(root, '[data-role="src-exam"]');
    expect(line.className).toBe("ew-ci-s ew-src-exam");
    expect(q(line, ":scope > span").textContent).toBe("抽出 12 道题 · 3 道没挂上 · 2 道读不清");
    expect([...line.querySelectorAll(".ew-tipbox > span")].map((x) => x.textContent)).toEqual(ex.unattached_items);
    expect(q(line, ".ew-qbtn").querySelector(".evowiki-sr-only")?.textContent).toBe("哪些题没挂上");
  });
  it("extracting: 正在找题, no ?; failed: the server's line in the error colour; no summary: no line", () => {
    const { root } = draw(state([src({ status: "ready" }), src({ id: "s2", status: "ready" }), src({ id: "s3", status: "ready" })],
      { sourceExam: { s1: { ...ex, state: "extracting" }, s2: { ...ex, state: "failed", detail: "题目没抽出来，请重试" } } }));
    const lines = [...root.querySelectorAll<HTMLElement>('[data-role="src-exam"]')];
    expect(lines.map((l) => [l.textContent, l.className, l.querySelector(".ew-qtip") !== null])).toEqual([
      ["正在找题", "ew-ci-s ew-src-exam", false], ["题目没抽出来，请重试", "ew-ci-s ew-src-exam ew-err", false]]);
  });
});

describe("添加资料", () => {
  it("the + beside the fold opens the picker and does not fold, also folded; a picked file goes to sourcesAdd", () => {
    const a = fakeActions();
    const root = renderEngine(state([src()], { personal: { ...INITIAL_PERSONAL, sources: false } }), a);
    const plus = q<HTMLButtonElement>(root, '.ew-src-box > [data-action="src-add"]');
    expect(plus.getAttribute("aria-label")).toBe(SOURCES_COPY.add);
    expect(plus.title).toBe(SOURCES_COPY.add);
    expect(root.querySelector("input[type=file]")).toBeNull();
    const picked: HTMLInputElement[] = [];
    const click = vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(function (this: HTMLInputElement) { picked.push(this); });
    plus.click();
    expect(click).toHaveBeenCalled();
    click.mockRestore();
    expect(a.personalView).not.toHaveBeenCalled();
    const f = new File(["x"], "a.pdf");
    Object.defineProperty(picked[0]!, "files", { value: [f], configurable: true });
    picked[0]!.dispatchEvent(new Event("change"));
    expect(a.sourcesAdd).toHaveBeenCalledWith([f]);
  });
  it("files dropped on the list go to sourcesAdd", () => {
    const { root, a } = draw(state([]));
    const f = new File(["x"], "a.pdf");
    const drop = new Event("drop", { cancelable: true }) as DragEvent;
    Object.defineProperty(drop, "dataTransfer", { value: { types: ["Files"], files: [f] } });
    q(root, ".ew-src-list").dispatchEvent(drop);
    expect(drop.defaultPrevented).toBe(true);
    expect(a.sourcesAdd).toHaveBeenCalledWith([f]);
  });
});

describe("a spending limit on the card", () => {
  it("设置 in the card's status or note opens the settings", () => {
    const line = "今天的花费到了上限 $1.00，这份资料没有读完。明天 0 点恢复，也可以在设置里调高。";
    for (const o of [{ status: "failed" as const, error: line }, { status: "ready" as const, note: line }]) {
      const { root, a } = draw(state([src({ ...o, error_code: "budget_exceeded" })]));
      q<HTMLButtonElement>(root, '[data-action="open-settings"]').click();
      expect(a.openSettings).toHaveBeenCalled();
    }
  });
});

it("unstructured historical error text never creates a settings action", () => {
 const { root } = draw(state([src({ status: "failed", error: "设置 Settings budget", error_code: null })]));
 expect(root.querySelector('[data-action="open-settings"]')).toBeNull();
});

it("typed progress localizes in place while server messages and source titles remain untouched", () => {
 const x = src({ title: "User 中文 title", status: "reading" }); const p = { ...prog(3), message: "旧中文系统进度" };
 const { root } = draw(state([x], { sourceProgress: { s1: p } })); const card = q(root, "[data-src=s1]"); const line = q(card, ".ew-src-st span");
 setUiLanguage("en"); expect(line.textContent).toBe("Reading sections · 3/12"); expect(q(root, "[data-src=s1]")).toBe(card);
 expect(card.textContent).toContain("User 中文 title"); expect(p.message).toBe("旧中文系统进度");
 updateSourceCard(root, x, { ...p, done: 7 }); setUiLanguage("zh"); expect(line.textContent).toBe("正在读取章节 · 7/12");
});
