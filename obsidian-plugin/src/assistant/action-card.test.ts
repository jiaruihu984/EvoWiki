import Sortable from "sortablejs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { sortDrag } from "../ui/reorder-test-kit";
import { ApiError, OfflineError } from "../api/errors";
import { cardRig } from "../../test/card-rig";
import { setup } from "../wiki/controller-test-kit";
import type { ActionEvent } from "./types";
import { buildActionCard } from "./action-card";
import { cardResult, newActionCard, type ActionCardState } from "./card-state";
import type { WriteOut } from "../wiki/schema";

const SPLIT: ActionEvent = {
  type: "action", goal_title: null, replaces: "", page_id: null, action_id: "act1", kind: "page_split", title: "把「特征值与特征向量」拆成 3 页", status: "proposed", confirm: true, can_undo: false,
  items: [
    { key: "q1", title: "特征向量：不被掰弯的方向", covers: "先用拉伸照片的例子看哪些方向不转", page_id: null, n: null, level: null },
    { key: "q2", title: "特征值：拉长了几倍", covers: "λ 是多少、正负和 0 各说明什么", page_id: null, n: null, level: null },
    { key: "q3", title: "怎么求：det(A − λI) = 0", covers: "从 Av = λv 推到行列式，再手算一个 2×2", page_id: null, n: null, level: null },
  ],
};
const w = (page_id: string, over: Partial<WriteOut> = {}): WriteOut => ({ page_id, run_id: "r1", kind: "organize", state: "queued", pass: null, section: null, of: null, started_at: null, cost_usd: 0, ...over });
const flush = async (): Promise<void> => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
const drop = (el: Element, type: string): void => { el.dispatchEvent(new Event(type, { bubbles: true, cancelable: true })); };
const titles = (rig: ReturnType<typeof cardRig>): string[] => rig.all(".ew-alist.ew-drag li .ew-t").map((e) => e.textContent ?? "");

afterEach(() => { vi.useRealTimers(); document.body.replaceChildren(); });

describe("the draft (asst.js actCard / draftList)", () => {
  it("is a .ew-propcard: <b> title, ol.alist.drag of draggable rows, the 添加页面 row, then 确认拆分 (primary) / 取消", async () => {
    const rig = cardRig();
    await rig.ask([SPLIT]);
    const card = rig.q(".ew-amsg > .ew-propcard")!;
    expect([...card.children].map((c) => c.tagName.toLowerCase() + "." + c.className)).toEqual(["b.", "div.ew-draft", "div.ew-row"]);
    expect(card.querySelector("b")!.textContent).toBe("建议拆分「特征值与特征向量」");
    const li = rig.all(".ew-alist.ew-drag > li");
    expect(li).toHaveLength(3);
    expect(li.every((x) => x.getAttribute("draggable") === "true" && x.tabIndex === 0)).toBe(true);
    expect(li[0]!.dataset.k).toBe("0");
    expect(li[0]!.getAttribute("aria-label")).toBe("第 1 页：特征向量：不被掰弯的方向，拖动或用 Alt + 上下键调整顺序");
    expect([...li[0]!.children].map((c) => c.className)).toEqual(["ew-n", "", "ew-x"]);
    expect(li[0]!.querySelector(".ew-t")!.textContent).toBe("特征向量：不被掰弯的方向");
    expect(li[0]!.querySelector(".ew-s")!.textContent).toBe("先用拉伸照片的例子看哪些方向不转");
    const x = li[0]!.querySelector<HTMLButtonElement>(".ew-x")!;
    expect([x.textContent, x.title, x.getAttribute("aria-label")]).toEqual(["×", "", "去掉「特征向量：不被掰弯的方向」"]);
    const add = card.querySelector(".ew-row.ew-aadd")!;
    const inp = add.querySelector("input")!;
    expect([inp.placeholder, inp.getAttribute("aria-label"), add.querySelector("button")!.textContent]).toEqual(["添加页面", "添加页面", "添加"]);
    const row = card.querySelector(":scope > .ew-row")!;
    const [ok, no] = [...row.children] as HTMLButtonElement[];
    expect([ok!.className, ok!.textContent, no!.className, no!.textContent]).toEqual(["ew-btn ew-primary", "确认拆分", "ew-btn", "取消"]);
    rig.dispose();
  });

  it("carries no explanatory sentence and no 挪了 / 你挪的 tag: the card's text is only the title, the rows and the buttons", async () => {
    const rig = cardRig();
    await rig.ask([SPLIT]);
    const card = rig.q(".ew-propcard")!;
    expect(card.textContent).toBe(
      "建议拆分「特征值与特征向量」01特征向量：不被掰弯的方向先用拉伸照片的例子看哪些方向不转×02特征值：拉长了几倍λ 是多少、正负和 0 各说明什么×"
      + "03怎么求：det(A − λI) = 0从 Av = λv 推到行列式，再手算一个 2×2×添加确认拆分取消");
    expect(card.textContent).not.toMatch(/挪了|你挪的|为什么要你批准|还在 Wiki 里|改好了/);
    expect(card.querySelector(".ew-tg")).toBeNull();
    rig.dispose();
  });

  it("drag a row (SortableJS) to the end: the draft reorders locally (nothing is sent), the card is drawn again from it", async () => {
    const rig = cardRig();
    await rig.ask([SPLIT]);
    const ol = rig.q(".ew-alist.ew-drag")!;
    expect(Sortable.get(ol)?.option("forceFallback")).toBe(true);    // pointer drag: no HTML5 drag-and-drop
    sortDrag(ol, 0, 2);
    expect(titles(rig)).toEqual(["特征值：拉长了几倍", "怎么求：det(A − λI) = 0", "特征向量：不被掰弯的方向"]);
    expect(rig.all(".ew-alist.ew-drag li .ew-n").map((e) => e.textContent)).toEqual(["01", "02", "03"]);
    expect(rig.all(".ew-alist.ew-drag li").map((e) => e.dataset.k)).toEqual(["0", "1", "2"]);
    expect(rig.api.actionCalls).toEqual([]);
    expect(rig.all(".ew-sort-ghost, .ew-sort-drag")).toHaveLength(0);
    rig.dispose();
  });

  it("the panel's drop (files / library text → 请从电脑拖文件进来) is never taken by the rows: no native row drag, a file passes through", async () => {
    const rig = cardRig();
    await rig.ask([SPLIT]);
    const li = rig.all(".ew-alist.ew-drag li");
    const start = new Event("dragstart", { bubbles: true, cancelable: true });
    li[0]!.dispatchEvent(start);
    expect(start.defaultPrevented).toBe(true);                     // the row's text never becomes a dropped text/plain
    const panelDrops: Event[] = [];
    rig.r.root.addEventListener("drop", (e) => panelDrops.push(e));
    const over = new Event("dragover", { bubbles: true, cancelable: true });
    li[2]!.dispatchEvent(over);
    expect(over.defaultPrevented).toBe(false);
    drop(li[2]!, "drop");
    expect(panelDrops).toHaveLength(1);
    expect(titles(rig)).toEqual(["特征向量：不被掰弯的方向", "特征值：拉长了几倍", "怎么求：det(A − λI) = 0"]);
    rig.dispose();
  });

  it("Alt + ↓ / ↑ moves the row and keeps the focus on it; other keys and the ends do nothing", async () => {
    const rig = cardRig();
    await rig.ask([SPLIT]);
    const key = (k: number, init: KeyboardEventInit): void => {
      rig.all(".ew-alist.ew-drag li")[k]!.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init }));
    };
    rig.all(".ew-alist.ew-drag li")[0]!.focus();
    key(0, { key: "ArrowDown", altKey: true });
    expect(titles(rig)[1]).toBe("特征向量：不被掰弯的方向");
    expect((document.activeElement as HTMLElement).dataset.k).toBe("1");
    key(1, { key: "ArrowDown" });                                  // no Alt
    key(1, { key: "ArrowUp", altKey: true });
    expect(titles(rig)[0]).toBe("特征向量：不被掰弯的方向");
    expect((document.activeElement as HTMLElement).dataset.k).toBe("0");
    key(0, { key: "ArrowUp", altKey: true });                      // the first row cannot go up
    expect(titles(rig)[0]).toBe("特征向量：不被掰弯的方向");
    rig.dispose();
  });

  it("× drops a page; 添加页面 adds one at the bottom (Enter or 添加) and the box keeps the focus; 确认 is off with no page", async () => {
    const rig = cardRig();
    await rig.ask([SPLIT]);
    rig.all<HTMLButtonElement>(".ew-alist .ew-x")[1]!.click();
    expect(titles(rig)).toEqual(["特征向量：不被掰弯的方向", "怎么求：det(A − λI) = 0"]);
    const inp = (): HTMLInputElement => rig.q<HTMLInputElement>(".ew-aadd input")!;
    inp().value = "  特征值的和  ";
    inp().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(titles(rig)).toEqual(["特征向量：不被掰弯的方向", "怎么求：det(A − λI) = 0", "特征值的和"]);
    expect(document.activeElement).toBe(inp());
    expect(inp().value).toBe("");
    inp().value = "迹";
    rig.q<HTMLButtonElement>(".ew-aadd button")!.click();
    expect(titles(rig)[titles(rig).length - 1]).toBe("迹");
    inp().value = "   ";                                            // blank: nothing added
    rig.q<HTMLButtonElement>(".ew-aadd button")!.click();
    expect(titles(rig)).toHaveLength(4);
    for (let i = 0; i < 4; i++) rig.q<HTMLButtonElement>(".ew-alist .ew-x")!.click();
    expect(rig.q<HTMLButtonElement>(".ew-propcard > .ew-row .ew-primary")!.disabled).toBe(true);
    rig.dispose();
  });
});

describe("确认 / 取消 (the Bot ledger)", () => {
  it("确认 sends the revised draft: a kept page by key, an added one by title with isNew; the card becomes the page list + 查看详情 / 撤销", async () => {
    const rig = cardRig();
    rig.api.actionMade.set("act1", [{ key: "q1", page_id: "pg_a", n: 2, folder: "" }, { key: "q3", page_id: "pg_c", n: 3, folder: "" },
      { key: "new-0", page_id: "pg_new", n: 4, folder: "" }]);
    rig.api.actionRows.set("act1", { action_id: "act1", kind: "page_split", scope: "new_page", level: "ask", status: "proposed", source: "assistant",
      summary: SPLIT.title, why: "", content: { pages: [{ key: "q1", title: "特征向量：不被掰弯的方向" }, { key: "q3", title: "怎么求：det(A − λI) = 0" },
        { key: "new-0", title: "特征值的和" }] }, page_id: null, detail: "", created_at: "", decided_at: null, expires_at: null, can_undo: false,
      diff: [], confirm: true, cost_usd: null, made: [], revise: null, result: {}, section_id: null });
    await rig.ask([SPLIT]);
    rig.all<HTMLButtonElement>(".ew-alist .ew-x")[1]!.click();     // drop q2
    rig.q<HTMLInputElement>(".ew-aadd input")!.value = "特征值的和";
    rig.q<HTMLButtonElement>(".ew-aadd button")!.click();
    rig.q<HTMLButtonElement>(".ew-propcard > .ew-row .ew-primary")!.click();
    await flush();
    expect(rig.api.actionCalls.filter((c) => c.op === "approve")).toEqual([{ op: "approve", id: "act1",
      draft: { pages: [{ key: "q1" }, { key: "q3" }, { title: "特征值的和", isNew: true }] } }]);
    const card = rig.q(".ew-propcard")!;
    expect(card.querySelector(".ew-alist.ew-drag")).toBeNull();
    expect(card.querySelector(".ew-aadd")).toBeNull();
    expect([...card.children].map((c) => c.tagName.toLowerCase() + "." + c.className)).toEqual(["div.ew-thead", "ol.ew-alist", "div.ew-afoot"]);
    expect(card.querySelector(".ew-thead")!.textContent).toBe("正在拆分「特征值与特征向量」进行中");   // the made pages have no run yet: nothing is claimed done
    // the list is the ledger's: the kept pages and the added one, with the pages they made
    expect(rig.all(".ew-propcard .ew-alist li .ew-t").map((e) => e.textContent)).toEqual(["特征向量：不被掰弯的方向", "怎么求：det(A − λI) = 0", "特征值的和"]);
    expect(rig.all<HTMLElement>(".ew-propcard .ew-ws").map((e) => e.dataset.w)).toEqual(["pg_a", "pg_c", "pg_new"]);
    expect(card.querySelector(".ew-alist .ew-n")).toBeNull();                       // rows carry no numbers
    expect(card.querySelector(".ew-afoot > .ew-faint")!.textContent).toBe("已完成 0/3");
    const acts = card.querySelector(".ew-afoot > .ew-aacts")!;
    expect([...acts.children].map((b) => b.textContent)).toEqual(["查看详情", "撤销"]);
    expect(card.querySelector(".ew-res")).toBeNull();
    rig.dispose();
  });

  it("an unedited draft is confirmed without a draft body", async () => {
    const rig = cardRig();
    await rig.ask([SPLIT]);
    rig.q<HTMLButtonElement>(".ew-propcard > .ew-row .ew-primary")!.click();
    await flush();
    expect(rig.api.actionCalls).toEqual([{ op: "approve", id: "act1" }]);
    rig.dispose();
  });

  it("取消 rejects the row and leaves 「未做更改」 under the title", async () => {
    const rig = cardRig();
    await rig.ask([SPLIT]);
    const [, no] = rig.all<HTMLButtonElement>(".ew-propcard > .ew-row .ew-btn");
    no!.click();
    await flush();
    expect(rig.api.actionCalls).toEqual([{ op: "reject", id: "act1" }]);
    const card = rig.q(".ew-propcard")!;
    expect(card.querySelector("b")!.textContent).toBe("建议拆分「特征值与特征向量」");
    expect(card.querySelector(".ew-res")!.textContent).toBe("未做更改");
    expect(card.querySelector(".ew-alist")).toBeNull();
    expect(card.querySelector(".ew-aacts")).toBeNull();
    rig.dispose();
  });

  it("撤销 of a done card leaves 「已撤销」; a re-listed card (an undo turn) updates in place, never twice", async () => {
    const rig = cardRig();
    await rig.ask([{ ...SPLIT, status: "executed", can_undo: true, items: SPLIT.items.map((i) => ({ ...i, page_id: `pg_${i.key}` })) }]);
    expect(rig.all(".ew-propcard")).toHaveLength(1);
    // a later turn's undo tool re-lists the card with the new status
    await rig.ask([{ ...SPLIT, status: "undone", can_undo: false }], "撤销刚才那个");
    expect(rig.all(".ew-propcard")).toHaveLength(1);
    expect(rig.q(".ew-propcard .ew-res")!.textContent).toBe("已撤销");
    expect(rig.q(".ew-propcard .ew-alist")).toBeNull();
    rig.dispose();
  });

  it("a refused click shows the server's reason and the buttons work again; nothing is silent", async () => {
    const rig = cardRig();
    await rig.ask([SPLIT]);
    rig.api.actionReplies.push({ throws: new ApiError(409, "这一页已经改过了，草稿作废") });
    rig.q<HTMLButtonElement>(".ew-propcard > .ew-row .ew-primary")!.click();
    await flush();
    expect(rig.q(".ew-propcard > .ew-muted:last-child")!.textContent).toBe("没改成：这一页已经改过了，草稿作废。");
    expect(rig.q<HTMLButtonElement>(".ew-propcard > .ew-row .ew-primary")!.disabled).toBe(false);
    rig.dispose();
  });

  it("a row the ledger could not run (failed) says 没改成 with its reason", async () => {
    const rig = cardRig();
    await rig.ask([{ ...SPLIT, status: "failed" }]);
    expect(rig.q(".ew-propcard .ew-res")!.textContent).toBe("没改成。");
    rig.dispose();
  });
});

describe("a done card: the pages and their writing status", () => {
  const DONE: ActionEvent = { ...SPLIT, status: "executed", can_undo: true,
    items: SPLIT.items.map((i) => ({ ...i, page_id: `pg_${i.key}` })) };

  it("plist rows: no number, button.t opens the page, .s, span.ws; 查看详情 / 撤销 in .aacts", async () => {
    const rig = cardRig();
    await rig.ask([DONE]);
    const li = rig.all(".ew-propcard .ew-alist:not(.ew-drag) > li");
    expect(li).toHaveLength(3);
    expect([...li[0]!.children].map((c) => c.className)).toEqual(["", "ew-ws"]);
    const t = li[1]!.querySelector<HTMLButtonElement>("button.ew-t")!;
    expect(t.textContent).toBe("特征值：拉长了几倍");
    t.click();
    expect(rig.spies.showPage).toHaveBeenCalledWith("pg_q2");
    expect(li[1]!.querySelector(".ew-s")!.textContent).toBe("λ 是多少、正负和 0 各说明什么");
    expect(li[0]!.querySelector<HTMLElement>(".ew-ws")!.dataset.w).toBe("pg_q1");
    const undo = rig.all<HTMLButtonElement>(".ew-aacts button")[1]!;
    expect(undo.textContent).toBe("撤销");
    undo.click();
    await flush();
    expect(rig.api.actionCalls[rig.api.actionCalls.length - 1]).toEqual({ op: "undo", id: "act1" });
    rig.dispose();
  });

  it("queued → planning → writing n/m → done → undone, painted in place from /wiki/writes (polled every 1100 ms, only while a row is writing)", async () => {
    vi.useFakeTimers();
    const rig = cardRig();
    rig.api.writesNow = [w("pg_q1", { state: "queued" }), w("pg_q2", { state: "queued" }), w("pg_q3", { state: "queued" })];
    await rig.ask([DONE]);
    await vi.advanceTimersByTimeAsync(0);
    const ws = (): HTMLElement[] => rig.all(".ew-ws");
    expect(ws().map((e) => e.textContent)).toEqual(["等待中", "等待中", "等待中"]);
    expect(rig.api.actionCalls.filter((c) => c.op === "writes")[0]).toEqual({ op: "writes", ids: ["pg_q1", "pg_q2", "pg_q3"] });
    const first = ws()[0]!;
    rig.api.writesNow = [w("pg_q1", { state: "planning", pass: 1 }), w("pg_q2", { state: "queued" }), w("pg_q3", { state: "queued" })];
    await vi.advanceTimersByTimeAsync(1100);
    expect(ws()[0]!.textContent).toBe("生成中");                    // planning shows as writing
    expect(rig.q(".ew-propcard .ew-thead")!.textContent).toBe("正在拆分「特征值与特征向量」进行中");
    expect(rig.q(".ew-afoot .ew-faint")!.textContent).toBe("已完成 0/3");
    expect(ws()[0]).toBe(first);                                    // the same span: no rebuild
    expect(ws()[0]!.querySelector("i")).not.toBeNull();
    rig.api.writesNow = [w("pg_q1", { state: "writing", section: 1, of: 4 }), w("pg_q2", { state: "queued" }), w("pg_q3", { state: "queued" })];
    await vi.advanceTimersByTimeAsync(1100);
    expect(ws()[0]!.textContent).toBe("生成中");                    // no numbers inside a card
    expect([...ws()[0]!.childNodes].map((n) => n.nodeName)).toEqual(["I", "#text"]);
    expect(ws()[1]!.textContent).toBe("等待中");
    rig.api.writesNow = [w("pg_q1", { state: "done" }), w("pg_q2", { state: "undone" }), w("pg_q3", { state: "done" })];
    await vi.advanceTimersByTimeAsync(1100);
    expect(ws().map((e) => e.textContent)).toEqual(["✓ 已完成", "已撤销", "✓ 已完成"]);
    expect(rig.q(".ew-propcard .ew-thead")!.textContent).toBe("已拆分「特征值与特征向量」已完成");
    expect(rig.q(".ew-afoot .ew-faint")!.textContent).toBe("已完成 2/3");
    const calls = rig.api.actionCalls.filter((c) => c.op === "writes").length;
    await vi.advanceTimersByTimeAsync(5000);
    expect(rig.api.actionCalls.filter((c) => c.op === "writes")).toHaveLength(calls);   // nothing is writing: the poll stopped
    rig.dispose();
  });

  it("a split's pages hold nothing yet: 未生成, head 已拆分, no 已完成 pill or count; a written page counts; opening one is seen on a later read", async () => {
    vi.useFakeTimers();
    const rig = cardRig();
    const unwritten = (id: string): WriteOut => ({ page_id: id, run_id: null, kind: "organize", state: "unwritten", pass: null, section: null, of: null, started_at: null, cost_usd: 0 });
    rig.api.writesNow = ["pg_q1", "pg_q2", "pg_q3"].map(unwritten);
    await rig.ask([DONE]);
    await vi.advanceTimersByTimeAsync(0);
    expect(rig.all(".ew-ws").map((e) => e.textContent)).toEqual(["未生成", "未生成", "未生成"]);
    expect(rig.q(".ew-propcard .ew-thead b")!.textContent).toBe("已拆分「特征值与特征向量」");
    expect(rig.q<HTMLElement>(".ew-propcard .ew-chip")!.hidden).toBe(true);
    expect(rig.q(".ew-afoot .ew-faint")!.textContent).toBe("");
    rig.api.writesNow = [{ ...unwritten("pg_q1"), state: "done" }, unwritten("pg_q2"), unwritten("pg_q3")];
    await vi.advanceTimersByTimeAsync(30000);
    expect(rig.all(".ew-ws").map((e) => e.textContent)).toEqual(["✓ 已完成", "未生成", "未生成"]);
    expect(rig.q(".ew-afoot .ew-faint")!.textContent).toBe("已完成 1/3");
  });
  it("a page with no run is ✓ 已完成 once its status was read; a failed read shows nothing, keeps the card 进行中, is kept quiet and retried later", async () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const rig = cardRig();
    rig.api.writesReplies.push({ throws: new ApiError(500, "x") });
    await rig.ask([DONE]);
    await vi.advanceTimersByTimeAsync(0);
    expect(rig.all(".ew-ws").map((e) => e.textContent)).toEqual(["", "", ""]);
    expect(rig.q(".ew-propcard .ew-chip")!.textContent).toBe("进行中");
    expect(warn).toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(5000);
    expect(rig.api.actionCalls.filter((c) => c.op === "writes")).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(15000);                       // the read worked, a page of this session may still get its run: a few more tries
    expect(rig.all(".ew-ws").map((e) => e.textContent)).toEqual(["✓ 已完成", "✓ 已完成", "✓ 已完成"]);   // then: no run = finished
    expect(rig.q(".ew-propcard .ew-chip")!.textContent).toBe("已完成");
    warn.mockRestore();
    rig.dispose();
  });

  it("offline reads keep page statuses quiet and explain why the rule editor cannot load", async () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const rig = cardRig();
    rig.api.writesReplies.push({ throws: new OfflineError() }, { throws: new OfflineError() });
    rig.api.listActions = () => Promise.reject(new OfflineError());
    await rig.ask([DONE, { type: "action", goal_title: null, replaces: "", page_id: null, action_id: "act9", kind: "rule", title: "规则", status: "proposed", confirm: true, can_undo: false, items: [] }]);
    await vi.advanceTimersByTimeAsync(5000);
    expect(rig.all(".ew-ws").map((e) => e.textContent)).toEqual(["", "", ""]);
    const rule = rig.q<HTMLElement>(".ew-propcard[data-action=act9]")!;
    expect(rule.textContent).toContain("未连接到服务器");
    expect(rule.querySelector<HTMLTextAreaElement>(".ew-rule-draft")!.disabled).toBe(true);
    expect(rule.querySelector<HTMLButtonElement>(".ew-primary")!.disabled).toBe(true);
    warn.mockRestore();
    rig.dispose();
  });

  it("no poll for a draft or a card without pages", async () => {
    vi.useFakeTimers();
    const rig = cardRig();
    await rig.ask([SPLIT]);
    await vi.advanceTimersByTimeAsync(3000);
    expect(rig.api.actionCalls.filter((c) => c.op === "writes")).toHaveLength(0);
    rig.dispose();
  });
});

describe("kinds without a page list", () => {
  const RULE: ActionEvent = { type: "action", goal_title: null, replaces: "", page_id: null, action_id: "act2", kind: "rule", title: "「先给一个能算出结果的小例子，再给定义和公式」改成所有页面都适用",
    status: "proposed", confirm: true, can_undo: false, items: [] };

  const ruleRow = () => ({ action_id: "act2", kind: "rule", scope: "teaching_params", level: "ask", status: "proposed", source: "assistant" as const,
    summary: RULE.title, why: "", content: { text: "每章先给例子，再解释公式" }, page_id: null, detail: "", created_at: "", decided_at: null,
    expires_at: null, can_undo: false, diff: [], confirm: true, cost_usd: null, made: [], revise: null, result: {}, section_id: null });

  it("cannot approve a rule before its original text has loaded", async () => {
    const rig = cardRig();
    await rig.ask([RULE]); await flush();
    expect(rig.q<HTMLTextAreaElement>(".ew-rule-draft")!.disabled).toBe(true);
    expect(rig.q<HTMLButtonElement>(".ew-propcard > .ew-row .ew-primary")!.disabled).toBe(true);
    await rig.ctl.confirmAction("act2");
    expect(rig.api.actionCalls.filter((c) => c.op === "approve")).toEqual([]);
    rig.dispose();
  });

  it("keeps the user's rule and caret through redraws and old ledger polls, then approves exactly the edited text", async () => {
    const rig = cardRig();
    const row = ruleRow();
    rig.api.actionRows.set("act2", row);
    await rig.ask([RULE]); await flush();
    rig.api.actionCalls.length = 0;
    const editor = rig.q<HTMLTextAreaElement>(".ew-rule-draft")!;
    expect(editor.value).toBe(row.content.text);
    expect(editor.disabled).toBe(false);
    editor.focus();
    editor.value = "每章先解释符号，再给例子";
    editor.setSelectionRange(4, 6);
    drop(editor, "input");
    const turn = rig.ctl.state.conv!.turns[0]!;
    turn.answer += "继续说明"; turn.rev++;
    rig.r.update(rig.ctl.state);
    expect(rig.q(".ew-rule-draft")).toBe(editor);
    expect(document.activeElement).toBe(editor);
    expect([editor.selectionStart, editor.selectionEnd]).toEqual([4, 6]);
    rig.ctl.syncLedger([row]);
    expect(editor.value).toBe("每章先解释符号，再给例子");
    expect(turn.actions[0]!.ruleText).toBe("每章先解释符号，再给例子");
    editor.value = "   "; drop(editor, "input");
    expect(rig.q<HTMLButtonElement>(".ew-propcard > .ew-row .ew-primary")!.disabled).toBe(true);
    await rig.ctl.confirmAction("act2");
    expect(rig.api.actionCalls).toEqual([]);
    const edited = "每章先解释符号，再用一个小例子演示";
    editor.value = edited; drop(editor, "input");
    expect(rig.q<HTMLButtonElement>(".ew-propcard > .ew-row .ew-primary")!.disabled).toBe(false);
    rig.q<HTMLButtonElement>(".ew-propcard > .ew-row .ew-primary")!.click(); await flush();
    expect(rig.api.actionCalls).toEqual([{ op: "approve", id: "act2", draft: { text: edited } }]);
    expect(rig.api.actionRows.get("act2")!.content.text).toBe(edited);
    expect(rig.q(".ew-rule-draft")).toBeNull();
    expect(rig.q(".ew-rule-text")!.textContent).toBe(edited);
    rig.dispose();
  });

  it("keeps an edited rule after an offline approval and retries with that same draft", async () => {
    const rig = cardRig();
    rig.api.actionRows.set("act2", ruleRow());
    await rig.ask([RULE]); await flush();
    rig.api.actionCalls.length = 0;
    const editor = rig.q<HTMLTextAreaElement>(".ew-rule-draft")!;
    editor.value = "每章先讲清楚每个符号"; drop(editor, "input");
    rig.api.actionReplies.push({ throws: new OfflineError() });
    await rig.ctl.confirmAction("act2");
    expect(rig.q<HTMLTextAreaElement>(".ew-rule-draft")!.value).toBe("每章先讲清楚每个符号");
    await rig.ctl.confirmAction("act2");
    expect(rig.api.actionCalls).toEqual(Array.from({ length: 2 }, () => ({ op: "approve", id: "act2", draft: { text: "每章先讲清楚每个符号" } })));
    expect(rig.q(".ew-rule-text")!.textContent).toBe("每章先讲清楚每个符号");
    rig.dispose();
  });

  it("a waiting rule shows 改动对比 from the ledger (no list, no 添加页面) and 确认 / 取消", async () => {
    const rig = cardRig();
    rig.api.actionRows.set("act2", { action_id: "act2", kind: "rule", scope: "teaching_params", level: "ask", status: "proposed", source: "assistant",
      summary: RULE.title, why: "", content: {}, page_id: null, detail: "", created_at: "", decided_at: null, expires_at: null, can_undo: false,
      diff: [{ kind: "del", text: "只在讲概念时" }, { kind: "add", text: "所有页面都适用" }], confirm: true, cost_usd: null, made: [], revise: null, result: {}, section_id: null });
    await rig.ask([RULE]);
    await flush();
    const card = rig.q(".ew-propcard")!;
    expect(card.querySelector(".ew-alist")).toBeNull();
    const diff = card.querySelector(".ew-diff")!;
    expect(diff.getAttribute("aria-label")).toBe("改动对比");
    expect([...diff.children].map((d) => d.className + ":" + d.textContent)).toEqual(["ew-dl ew-del:−只在讲概念时", "ew-dl ew-add:+所有页面都适用"]);
    expect([...card.querySelectorAll(":scope > .ew-row .ew-btn")].map((b) => b.textContent)).toEqual(["确认修改", "取消"]);
    rig.dispose();
  });

  it("a done rule has 查看详情 (opens the 参数 row) before 撤销", async () => {
    const rig = cardRig();
    await rig.ask([{ ...RULE, status: "executed", can_undo: true }]);
    const acts = rig.q(".ew-aacts")!;
    expect([...acts.children].map((b) => b.className + ":" + b.textContent)).toEqual(["ew-btn ew-link:查看详情", "ew-btn ew-link:撤销"]);
    (acts.children[0] as HTMLButtonElement).click();
    expect(rig.spies.showInParams).toHaveBeenCalledWith("act2");
    rig.dispose();
  });
});

describe("the ledger changed in 参数 (the same row)", () => {
  it("the card follows a status it did not decide itself, in place", async () => {
    const rig = cardRig();
    await rig.ask([SPLIT]);
    const row = (status: string, extra: object = {}) => ({ action_id: "act1", kind: "page_split", scope: "new_page", level: "ask", status, source: "assistant" as const,
      summary: SPLIT.title, why: "", content: { pages: [{ key: "q1", title: "特征向量：不被掰弯的方向", page_id: "pg_x" }] }, page_id: null, detail: "",
      created_at: "", decided_at: null, expires_at: null, can_undo: status === "executed", diff: [], confirm: true, cost_usd: null, made: [], revise: null, result: {}, section_id: null, ...extra });
    rig.ctl.syncLedger([row("executed", { made: [{ key: "q1", page_id: "pg_x", n: 1, folder: "" }] })]);
    expect(rig.all(".ew-propcard .ew-aacts button")[1]!.textContent).toBe("撤销");
    expect(rig.q<HTMLElement>(".ew-propcard .ew-ws")!.dataset.w).toBe("pg_x");
    rig.ctl.syncLedger([row("undone")]);
    expect(rig.q(".ew-propcard .ew-res")!.textContent).toBe("已撤销");
    rig.dispose();
  });

  it("a done row that later says why it stopped (a merge whose original changed) shows that line, 撤销 kept", async () => {
    const rig = cardRig();
    await rig.ask([SPLIT]);
    const done = { action_id: "act1", kind: "page_split", scope: "new_page", level: "ask", status: "executed", source: "assistant" as const,
      summary: SPLIT.title, why: "", content: { pages: [{ key: "q1", title: "特征向量：不被掰弯的方向", page_id: "pg_x" }] }, page_id: null, detail: "",
      created_at: "", decided_at: null, expires_at: null, can_undo: true, diff: [], confirm: true, cost_usd: null, made: [{ key: "q1", page_id: "pg_x", n: 1, folder: "" }],
      revise: null, result: {}, section_id: null };
    rig.ctl.syncLedger([done]);
    expect(rig.all(".ew-propcard .ew-muted").map((e) => e.textContent)).not.toContain("原页没有移走");
    rig.ctl.syncLedger([{ ...done, detail: "原页没有移走" }]);
    expect(rig.all(".ew-propcard .ew-muted").map((e) => e.textContent)).toContain("原页没有移走");
    expect(rig.all(".ew-propcard .ew-aacts button")[1]!.textContent).toBe("撤销");
    rig.dispose();
  });

  it("a draft the learner is editing keeps its edits when the ledger is re-read unchanged", async () => {
    const rig = cardRig();
    await rig.ask([SPLIT]);
    rig.all<HTMLButtonElement>(".ew-alist .ew-x")[0]!.click();
    rig.ctl.syncLedger([{ action_id: "act1", kind: "page_split", scope: "new_page", level: "ask", status: "proposed", source: "assistant", summary: "", why: "",
      content: {}, page_id: null, detail: "", created_at: "", decided_at: null, expires_at: null, can_undo: false, diff: [], confirm: true, cost_usd: null, made: [], revise: null, result: {}, section_id: null }]);
    expect(titles(rig)).toHaveLength(2);
    rig.dispose();
  });
});

describe("a row decided in 参数 (WikiController.botDecide) and the chat card are one record", () => {
  it("the card follows the rows the wiki state holds after 批准 in 参数, then 撤销 there", async () => {
    const wiki = setup();
    await wiki.c.loadEngine();
    const rows = (): never => { const a = wiki.c.state.bot.actions; if (a.kind !== "ready") throw new Error(a.kind); return a.value as never; };
    const rig = cardRig();
    await rig.ask([{ ...SPLIT, action_id: "act_merge1" }]);
    rig.ctl.syncLedger(rows());                                   // proposed in both: nothing changes
    expect(rig.q(".ew-propcard .ew-draft")).not.toBeNull();
    await wiki.c.botDecide("act_merge1", "approve");
    rig.ctl.syncLedger(rows());
    expect(rig.q(".ew-propcard .ew-draft")).toBeNull();
    expect(rig.all(".ew-propcard .ew-aacts button")[1]!.textContent).toBe("撤销");
    await wiki.c.botDecide("act_merge1", "undo");
    rig.ctl.syncLedger(rows());
    expect(rig.q(".ew-propcard .ew-res")!.textContent).toBe("已撤销");
    rig.dispose();
  });
});

describe("「这个我早会了」: the one confirm question", () => {
  const KNOWN: ActionEvent = { type: "action", goal_title: null, replaces: "", page_id: null, action_id: "k1", kind: "node_known", title: "「抽样」记成会了", status: "proposed", confirm: true, can_undo: false, items: [] };
  it("after 确认 the question shows as a one-question quiz; a wrong answer goes to /confirm and its message is shown", async () => {
    const { clearEwNotices, ewNotices } = await import("../../test/ew-notices");
    clearEwNotices();
    const rig = cardRig();
    rig.api.knownItems.push({ stem: "从总体里随机抽出的一部分叫什么？", options: ["样本", "总体"] });
    rig.api.knownReplies.push({ correct: false, correct_index: 0, undone: true, message: "这道确认题没答对，我把「抽样」的“已会”撤回了。" });
    await rig.ask([KNOWN]);
    await rig.ctl.confirmAction("k1"); await flush();
    expect(rig.api.actionCalls.map((c) => c.op).filter((o) => o !== "list")).toEqual(["approve", "knownItem"]);
    expect(rig.q(".ew-check .ew-stem")!.textContent).toBe("从总体里随机抽出的一部分叫什么？");
    await rig.ctl.answerQuiz("known:k1", 1); await flush();
    expect(rig.api.actionCalls[rig.api.actionCalls.length - 1]).toEqual({ op: "knownAnswer", id: "k1", choice: 1 });
    expect(ewNotices()).toEqual(["这道确认题没答对，我把「抽样」的“已会”撤回了。"]);
    rig.dispose();
  });
  it("nothing to answer (404: answered already) shows no question", async () => {
    const rig = cardRig();
    await rig.ask([KNOWN]);
    await rig.ctl.confirmAction("k1"); await flush();
    expect(rig.api.actionCalls.map((c) => c.op).filter((o) => o !== "list")).toEqual(["approve", "knownItem"]);
    expect(rig.q(".ew-check")).toBeNull();
    rig.dispose();
  });
});

describe("round 1 fixes", () => {
  const row = (status: string, extra: object = {}) => ({ action_id: "act1", kind: "page_split", scope: "new_page", level: "ask", status, source: "assistant" as const,
    summary: SPLIT.title, why: "", content: { pages: SPLIT.items.map((i) => ({ key: i.key, title: i.title })) }, page_id: null, detail: "", created_at: "",
    decided_at: null, expires_at: null, can_undo: status === "executed", diff: [], confirm: true, cost_usd: null, made: [], revise: null, result: {}, section_id: null, ...extra });
  const made = [{ key: "q1", page_id: "pg_a", n: 2, folder: "" }, { key: "q2", page_id: "pg_b", n: 3, folder: "" }, { key: "q3", page_id: "pg_c", n: 4, folder: "" }];

  it("a stale `proposed` row read after a local 确认 does not push the card back; only forward moves are taken", async () => {
    const rig = cardRig();
    rig.api.actionMade.set("act1", made);
    await rig.ask([SPLIT]);
    rig.q<HTMLButtonElement>(".ew-propcard > .ew-row .ew-primary")!.click();
    await flush();
    expect(rig.all(".ew-propcard .ew-aacts button")[1]!.textContent).toBe("撤销");
    rig.ctl.syncLedger([row("proposed")]);                         // the 参数 list was read before the approve
    expect(rig.all(".ew-propcard .ew-aacts button")[1]!.textContent).toBe("撤销");
    expect(rig.q(".ew-propcard .ew-draft")).toBeNull();
    rig.ctl.syncLedger([row("executed", { made })]);               // same status: nothing to redraw
    rig.ctl.syncLedger([row("rejected")]);                         // executed can only go to undone
    expect(rig.all(".ew-propcard .ew-aacts button")[1]!.textContent).toBe("撤销");
    rig.ctl.syncLedger([row("undone")]);
    expect(rig.q(".ew-propcard .ew-res")!.textContent).toBe("已撤销");
    rig.dispose();
  });

  it("an unchanged ledger does not redraw the turn (same elements), and a half-typed 添加页面 survives a redraw", async () => {
    const rig = cardRig();
    await rig.ask([SPLIT]);
    const inp = (): HTMLInputElement => rig.q<HTMLInputElement>(".ew-aadd input")!;
    inp().value = "还没打完"; inp().focus();
    rig.ctl.syncLedger([row("proposed")]);
    expect(inp().value).toBe("还没打完");
    // another edit of the same card redraws the turn: the typed text stays
    rig.all<HTMLButtonElement>(".ew-alist .ew-x")[0]!.click();
    expect(inp().value).toBe("还没打完");
    expect(document.activeElement).toBe(inp());
    rig.dispose();
  });

  it("a draft card does not take the ledger's diff (only cards without a page list do)", async () => {
    const rig = cardRig();
    await rig.ask([SPLIT]);
    rig.ctl.syncLedger([row("proposed", { diff: [{ kind: "add", text: "x" }] })]);
    expect(rig.q(".ew-propcard .ew-diff")).toBeNull();
    rig.dispose();
  });

  it("right after 确认 the made pages show 等待中 although the first read found no run; polling goes on until each is done", async () => {
    vi.useFakeTimers();
    const rig = cardRig();
    rig.api.actionMade.set("act1", made);
    await rig.ask([SPLIT]);
    rig.api.writesReplies.push([], [w("pg_a"), w("pg_b"), w("pg_c")],
      [w("pg_a", { state: "writing", section: 0, of: 3 }), w("pg_b"), w("pg_c")], [w("pg_a", { state: "done" }), w("pg_b", { state: "done" }), w("pg_c", { state: "failed" })]);
    rig.q<HTMLButtonElement>(".ew-propcard > .ew-row .ew-primary")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(rig.all(".ew-ws").map((e) => e.textContent)).toEqual(["", "", ""]);           // read 1: no run yet
    await vi.advanceTimersByTimeAsync(1100);
    expect(rig.all(".ew-ws").map((e) => e.textContent)).toEqual(["等待中", "等待中", "等待中"]);
    await vi.advanceTimersByTimeAsync(1100);
    expect(rig.all(".ew-ws")[0]!.textContent).toBe("生成中");
    await vi.advanceTimersByTimeAsync(1100);
    expect(rig.all(".ew-ws").map((e) => e.textContent)).toEqual(["✓ 已完成", "✓ 已完成", "生成失败"]);
    const n = rig.api.actionCalls.filter((c) => c.op === "writes").length;
    await vi.advanceTimersByTimeAsync(5000);
    expect(rig.api.actionCalls.filter((c) => c.op === "writes")).toHaveLength(n);
    rig.dispose();
  });

  it("nothing is read while the panel is not on screen; it resumes when it shows", async () => {
    vi.useFakeTimers();
    const rig = cardRig();
    let on = false;
    rig.ctl.observeVisible(() => on);
    rig.api.writesNow = [w("pg_q1", { state: "writing", section: 0, of: 2 }), w("pg_q2"), w("pg_q3")];
    await rig.ask([{ ...SPLIT, status: "executed", can_undo: true, items: SPLIT.items.map((i) => ({ ...i, page_id: `pg_${i.key}` })) }]);
    await vi.advanceTimersByTimeAsync(4000);
    expect(rig.api.actionCalls.filter((c) => c.op === "writes")).toHaveLength(0);
    on = true;
    await vi.advanceTimersByTimeAsync(1200);
    expect(rig.api.actionCalls.filter((c) => c.op === "writes").length).toBeGreaterThan(0);
    rig.dispose();
  });

  it("the page-name list of a made page uses the draft key: an item that made no page is not listed", async () => {
    const rig = cardRig();
    rig.api.actionMade.set("act1", [made[0]!, made[2]!]);
    await rig.ask([SPLIT]);
    rig.q<HTMLButtonElement>(".ew-propcard > .ew-row .ew-primary")!.click();
    await flush();
    expect(rig.all<HTMLElement>(".ew-propcard .ew-ws").map((e) => e.dataset.w)).toEqual(["pg_a", "pg_c"]);
    rig.dispose();
  });

  it("a merge's live row shows the folder line; no notice follows 确认 (the card says it)", async () => {
    const rig = cardRig();
    const notes: string[] = [];
    const obs = new MutationObserver(() => { notes.push(...[...document.querySelectorAll(".ew-notice span")].map((e) => e.textContent ?? "")); });
    obs.observe(document.body, { childList: true, subtree: true });
    rig.api.actionRows.set("m1", row("proposed", { action_id: "m1", kind: "page_merge", content: { title: "方差和协方差", pages: [{ key: "pv" }, { key: "pc" }] },
      made: [{ key: null, page_id: "pg_m", n: null, folder: "统计" }] }));
    rig.api.actionReplies.push(row("executed", { action_id: "m1", kind: "page_merge", content: { title: "方差和协方差", pages: [{ key: "pv" }, { key: "pc" }] },
      made: [{ key: null, page_id: "pg_m", n: null, folder: "统计" }] }));
    await rig.ask([{ type: "action", goal_title: null, replaces: "", page_id: null, action_id: "m1", kind: "page_merge", title: "把「方差」和「协方差」合成一页「方差和协方差」", status: "proposed", confirm: true,
      items: [{ key: "pv", title: "方差", page_id: null, covers: null, n: null, level: null }, { key: "pc", title: "协方差", page_id: null, covers: null, n: null, level: null }], can_undo: false }]);
    rig.q<HTMLButtonElement>(".ew-propcard > .ew-row .ew-primary")!.click();
    await flush();
    await Promise.resolve();
    obs.disconnect();
    expect(rig.q(".ew-propcard .ew-alist .ew-t")!.textContent).toBe("方差和协方差");
    expect(rig.q(".ew-propcard .ew-alist .ew-s")!.textContent).toBe("在「统计」文件夹里");
    expect(rig.q(".ew-propcard .ew-thead b")!.textContent).toBe("正在合并「方差」和「协方差」");   // the new page has no run yet
    expect(notes).toEqual([]);
    rig.dispose();
  });
});

describe("a preference the assistant recorded (kind stated_rule): one quiet line, not a card", () => {
  const NOTE: ActionEvent = { type: "action", goal_title: null, replaces: "", page_id: null, action_id: "act7", kind: "stated_rule", title: "记下了：页面里多配几个能动手的交互组件",
    status: "executed", confirm: false, can_undo: true, items: [] };

  it("reads 记下了：<principle> with a 撤销 link; 撤销 undoes it and the line says 已撤销：<principle>", async () => {
    const rig = cardRig();
    await rig.ask([NOTE], "这页没有组件，我喜欢组件多一点的页面");
    expect(rig.q(".ew-propcard")).toBeNull();
    expect(rig.q(".ew-asec")).toBeNull();                        // no 内容建议 label above it
    const line = rig.q(".ew-noteline")!;
    expect(line.querySelector(".ew-nt")!.textContent).toBe("记下了：页面里多配几个能动手的交互组件");
    const undo = line.querySelector<HTMLButtonElement>("button")!;
    expect([undo.textContent, undo.className]).toEqual(["撤销", "ew-btn ew-link"]);
    undo.click();
    await flush();
    expect(rig.api.actionCalls[rig.api.actionCalls.length - 1]).toEqual({ op: "undo", id: "act7" });
    const after = rig.q(".ew-noteline")!;
    expect(after.classList.contains("ew-done")).toBe(true);
    expect(after.textContent).toBe("已撤销：页面里多配几个能动手的交互组件");
    expect(after.querySelector("button")).toBeNull();
    rig.dispose();
  });
});

describe("查看详情 opens the action's result (user 2026-10-28: 「那肯定是看这个操作的结果」)", () => {
  const card = (kind: string, over: Partial<ActionCardState> = {}): ActionCardState => ({
    ...newActionCard({ goal_title: null, action_id: "a1", kind, title: "t", status: "executed", confirm: false, can_undo: true, detail: "", replaces: "", items: [] } as unknown as ActionEvent),
    ...over });
  it("cardResult: a goal → its run; a rule → 讲解方式; an RSI row → 参数; a page (at its section) / the first new page; else none", () => {
    expect(cardResult(card("goal_new", { at: { pageId: null, sectionId: null, mapId: "m1" } }))).toEqual({ kind: "goal", actionId: "a1", mapId: "m1" });
    expect(cardResult(card("stated_rule"))).toEqual({ kind: "rules" });
    expect(cardResult(card("rule"))).toEqual({ kind: "rules" });
    expect(cardResult(card("rsi_patch"))).toEqual({ kind: "params" });
    expect(cardResult(card("page_practice", { at: { pageId: "p1", sectionId: "s3", mapId: null } }))).toEqual({ kind: "page", pageId: "p1", sectionId: "s3" });
    const pages = [{ key: "k1", title: "新一", page_id: "pn1", covers: null, n: null, level: null }, { key: "k2", title: "新二", page_id: "pn2", covers: null, n: null, level: null }];
    expect(cardResult(card("page_split", { items: pages, at: { pageId: "p_old", sectionId: null, mapId: null } }))).toEqual({ kind: "page", pageId: "pn1", sectionId: null });
    expect(cardResult(card("assistant_edit", { at: { pageId: "p2", sectionId: null, mapId: null } }))).toEqual({ kind: "page", pageId: "p2", sectionId: null });
    for (const k of ["settings", "review_postpone", "node_known", "goal_deadline"]) expect(cardResult(card(k, { at: { pageId: null, sectionId: null, mapId: null } }))).toBeNull();
  });
  it("a card read back with its conversation (ActionCard page_id / section_id) opens its page at that section", () => {
    const back = newActionCard({ goal_title: null, action_id: "a2", kind: "page_practice", title: "已修改「节点与边」", status: "executed", confirm: true, can_undo: true,
      detail: "", replaces: "", items: [], result: {}, page_id: "pg1", section_id: "s3" });
    expect(cardResult(back)).toEqual({ kind: "page", pageId: "pg1", sectionId: "s3" });
  });
  it("the done card shows 查看详情 only with a result, and it calls showResult (not 参数)", () => {
    const h = { confirm: vi.fn(), cancel: vi.fn(), undo: vi.fn(), move: vi.fn(), remove: vi.fn(), add: vi.fn(), showPage: vi.fn(), showInParams: vi.fn(), showResult: vi.fn() };
    const done = buildActionCard(card("page_practice", { at: { pageId: "p1", sectionId: "s3", mapId: null } }), h);
    done.querySelector<HTMLButtonElement>("[data-action=details]")!.click();
    expect(h.showResult).toHaveBeenCalledWith("a1");
    expect(h.showInParams).not.toHaveBeenCalled();
    const none = buildActionCard(card("settings", { at: { pageId: null, sectionId: null, mapId: null } }), h);
    expect(none.querySelector("[data-action=details]")).toBeNull();
    expect(none.textContent).toContain("撤销");
  });
});

describe("a split of a graph point (node_split)", () => {
  const hooks = { confirm: () => {}, cancel: () => {}, undo: () => {}, remove: () => {}, move: () => {}, add: () => {}, showPage: () => {}, showInParams: () => {} } as never;
  const event = (over: object = {}) => ({ type: "action", goal_title: null, replaces: "", page_id: null, action_id: "s1", kind: "node_split", title: "把「磨豆机」拆成 2 个点", status: "proposed", confirm: true, can_undo: false,
    items: [{ key: "p1", title: "刀片磨", page_id: null, covers: "靠刀片打碎", n: null, level: null }, { key: "p2", title: "刀盘磨", page_id: null, covers: null, n: null, level: null }], ...over }) as never;
  const yes = (c: HTMLElement) => [...c.querySelectorAll("button")].find((b) => b.textContent === "确认拆分")!;

  it("lists the named points to edit, a point's line only when the model wrote one, and needs two to confirm", () => {
    const card = newActionCard(event());
    const c = buildActionCard(card, hooks);
    expect(c.querySelector("b")!.textContent).toBe("把「磨豆机」拆成 2 个点");
    expect([...c.querySelectorAll("li .ew-s")].map((x) => x.textContent)).toEqual(["靠刀片打碎"]);
    expect(c.querySelector("input")!.placeholder).toBe("添加知识点");
    expect(yes(c).disabled).toBe(false);
    card.draft.pop();
    expect(yes(buildActionCard(card, hooks)).disabled).toBe(true);
  });

  it("done: says how many, lists them, and no page writing status or count", () => {
    const c = buildActionCard(newActionCard(event({ status: "executed", can_undo: true })), hooks);
    expect(c.querySelector("b")!.textContent).toBe("已把「磨豆机」拆成 2 个点");
    expect(c.textContent).toContain("刀盘磨");
    expect(c.querySelector(".ew-ws")!.textContent).toBe("");
    expect(c.querySelector(".ew-afoot .ew-faint")!.textContent).toBe("");
  });
});

describe("a change that took effect at once", () => {
  it("is not labelled 内容建议", async () => {
    const rig = cardRig();
    const done: ActionEvent = { type: "action", goal_title: null, replaces: "", page_id: null, action_id: "act9", kind: "path_blocks", title: "改名", status: "executed", confirm: false, can_undo: true, items: [] };
    await rig.ask([done], "把最后一块改名");
    expect(rig.q(".ew-asec")).toBeNull();
  });
});
