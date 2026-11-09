import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/errors";
import { FakeWikiApi } from "../fake";
import type { ExamOpenItemOut, ExamSummaryOut, PageExamOut } from "../schema";
import { createExamBlock, type ExamPort, type ExamSource, mountExamBlock, mountQuizAsset } from "./exam-block";
import { demoPageExam } from "./exam-fixture";
import { EXAM_COPY, examLine, examTagTip, examView, freshItemUi, reportOutcome, type ExamItemUi, type ExamResult } from "./exam-model";

const item = (o: Partial<ExamOpenItemOut> = {}): ExamOpenItemOut => ({
  item_id: "i1", number: "7", context: null, stem: "求 A 的特征值。", options: [], kind: "open", has_key: true, answer: "2 和 5",
  explanation: "解特征方程。", source_title: "期末试卷", page: 14, printed_page: "12", done: false, ...o,
});
const page = (o: Partial<PageExamOut> = {}): PageExamOut => ({ asset_id: null, quiz_count: 0, open_items: [], sources: ["期末试卷"], frequent: false, ...o });
const ui = (o: Partial<ExamItemUi> = {}) => (): ExamItemUi => ({ ...freshItemUi(), ...o });
const summary = (o: Partial<ExamSummaryOut> = {}): ExamSummaryOut => ({ state: "done", items: 12, attached: 9, unattached: 3, pending: 0,
  unreadable: 0, incomplete: 0, unattached_items: ["第 4 题：行列式", "第 9 题：二次型"], detail: null, ...o });

describe("exam-model: what the 真题练习 block shows", () => {
  it("nothing to show (no answer, no quiz and no question: the material deleted) → no block", () => {
    expect(examView(null, ui())).toBeNull();
    expect(examView(page(), ui())).toBeNull();
    expect(examView(page({ asset_id: "a1", quiz_count: 2 }), ui())).not.toBeNull();
    expect(examView(page({ open_items: [item()] }), ui())).not.toBeNull();
  });

  it("the title's ? names the materials, each once; none → no ?", () => {
    expect(examView(page({ open_items: [item()], sources: ["X", "Y", "X"] }), ui())!.tip).toBe("来自《X》《Y》的往年题");
    expect(examView(page({ open_items: [item()], sources: [] }), ui())!.tip).toBeNull();
  });

  it("a question with an answer: 看答案 first, then the answer + 我答对了 / 没答对", () => {
    const first = examView(page({ open_items: [item()] }), ui())!.items[0]!;
    expect(first).toMatchObject({ heading: "第 7 题", cite: "《期末试卷》p.12", reveal: true, answer: null, buttons: [] });
    const shown = examView(page({ open_items: [item()] }), ui({ shown: true }))!.items[0]!;
    expect(shown.reveal).toBe(false);
    expect(shown.answer).toBe("2 和 5");
    expect(shown.explanation).toBe("解特征方程。");
    expect(shown.buttons.map((b) => [b.label, b.pressed, b.disabled])).toEqual([["我答对了", false, false], ["没答对", false, false]]);
  });

  it("a question without an answer: only 我做完了 (no 看答案, no answer)", () => {
    const v = examView(page({ open_items: [item({ has_key: false, answer: null, explanation: null })] }), ui())!.items[0]!;
    expect(v.reveal).toBe(false);
    expect(v.answer).toBeNull();
    expect(v.buttons.map((b) => b.label)).toEqual(["我做完了"]);
  });

  it("the pressed choice stays pressed and every button of it is off", () => {
    const v = examView(page({ open_items: [item()] }), ui({ shown: true, pick: "wrong" }))!.items[0]!;
    expect(v.buttons.map((b) => [b.label, b.pressed, b.disabled])).toEqual([["我答对了", false, true], ["没答对", true, true]]);
  });

  it("reported earlier today (done, the choice unknown here): the answer and one pressed 今天做过了", () => {
    const v = examView(page({ open_items: [item({ done: true })] }), ui())!.items[0]!;
    expect(v.reveal).toBe(false);
    expect(v.answer).toBe("2 和 5");
    expect(v.buttons).toEqual([{ result: null, label: EXAM_COPY.doneToday, pressed: true, disabled: true }]);
  });

  it("while a report is out the buttons are off; a failed one says only 没记上，请重试 and they come back", () => {
    const busy = examView(page({ open_items: [item()] }), ui({ shown: true, sending: "right", failed: true }))!.items[0]!;
    expect(busy.buttons.every((b) => b.disabled)).toBe(true);
    expect(busy.error).toBeNull();
    const failed = examView(page({ open_items: [item()] }), ui({ shown: true, failed: true }))!.items[0]!;
    expect(failed.error).toBe("没记上，请重试");
    expect(failed.buttons.every((b) => !b.disabled)).toBe(true);
  });

  it("cite: the printed page, else the PDF page, else the title alone; no number → no heading; a choice question lists its options", () => {
    const v = (o: Partial<ExamOpenItemOut>) => examView(page({ open_items: [item(o)] }), ui())!.items[0]!;
    expect(v({ printed_page: null }).cite).toBe("《期末试卷》p.14");
    expect(v({ printed_page: null, page: null }).cite).toBe("《期末试卷》");
    expect(v({ source_title: "" }).cite).toBe("");
    expect(v({ number: null }).heading).toBe("");
    expect(v({ kind: "single", options: ["1", "2"], has_key: false, answer: null }).options).toEqual(["A. 1", "B. 2"]);
    expect(v({ context: "已知 A。" }).context).toBe("已知 A。");
  });

  it("a report is recorded on k / score / done; duplicate and 409 mean an earlier report was kept; anything else failed", () => {
    expect(reportOutcome({ ok: true, status: "score" })).toBe("recorded");
    expect(reportOutcome({ ok: true, status: "duplicate" })).toBe("already");
    expect(reportOutcome({ ok: false, error: new ApiError(409, "这道题刚刚已经记过了") })).toBe("already");
    expect(reportOutcome({ ok: false, error: new ApiError(500, "x") })).toBe("failed");
    expect(reportOutcome({ ok: false, error: new Error("offline") })).toBe("failed");
  });

  it("常考: a tag only for a non-zero count, its ? 「有 N 道往年题考到这里」", () => {
    expect(examTagTip(undefined)).toBeNull();
    expect(examTagTip(0)).toBeNull();
    expect(examTagTip(4)).toBe("有 4 道往年题考到这里");
  });

  it("the material card's 真题 line", () => {
    expect(examLine(null)).toBeNull();
    expect(examLine(summary({ state: "extracting" }))).toEqual({ text: "正在找题", failed: false, tip: [] });
    expect(examLine(summary())).toEqual({ text: "抽出 12 道题 · 3 道没挂上", failed: false, tip: ["第 4 题：行列式", "第 9 题：二次型"] });
    expect(examLine(summary({ unattached: 0, unattached_items: [] }))!.text).toBe("抽出 12 道题");
    expect(examLine(summary({ unreadable: 2, detail: "有 2 道没对上" }))).toEqual({ text: "抽出 12 道题 · 3 道没挂上 · 2 道读不清", failed: false,
      tip: ["第 4 题：行列式", "第 9 题：二次型", "有 2 道没对上"] });
    expect(examLine(summary({ state: "failed", detail: "题目没抽出来，请重试" }))).toEqual({ text: "题目没抽出来，请重试", failed: true, tip: [] });
    expect(examLine(summary({ state: "failed", detail: null }))!.text).toBe(EXAM_COPY.extractFailed);
  });
});

/** A controller stand-in over the fake API (its examSelf rules are the server's); `self` replaces examSelf. */
function harness(out: PageExamOut | null, self?: ExamSource["examSelf"]) {
  const api = new FakeWikiApi({ delayMs: 0 });
  if (out !== null) api.__setPageExam("p1", out);
  const pe = vi.fn((id: string) => api.pageExam(id));
  const src: ExamSource = { pageExam: pe, examSelf: self ?? ((id, r) => api.examSelf(id, r)) };
  const listeners = new Set<() => void>();
  const hub = { current: src as ExamSource | null, subscribe: (fn: () => void) => { listeners.add(fn); return () => listeners.delete(fn); } };
  const mounted: string[] = [];
  const port: ExamPort = { hub, ui: new Map(), mountQuiz: (el, id) => { mounted.push(id); el.append(Object.assign(document.createElement("div"), { className: "quiz-stub" })); return () => { mounted.push(`-${id}`); }; } };
  return { api, src, pe, hub, port, mounted, notify: () => { for (const l of listeners) l(); } };
}
const btns = (el: HTMLElement, id: string) => [...el.querySelectorAll<HTMLButtonElement>(`.ew-exi[data-item="${id}"] .ew-row > button`)];
const press = (el: HTMLElement, id: string, label: string) => btns(el, id).find((b) => b.textContent === label)!.click();
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("the 真题练习 block = app.js examBlock", () => {
  it("draws nothing while there is nothing to show (and reads the page once)", async () => {
    const h = harness(null);
    const b = createExamBlock("p1", h.port);
    await b.refresh();
    expect(b.el.className).toBe("ew ew-inline evowiki-exam-host");
    expect(b.el.childElementCount).toBe(0);
    expect(h.mounted).toEqual([]);
  });

  it("h3 真题练习 + ?, the quiz box with the asset mounted, one card per question (app.js EXAM)", async () => {
    const h = harness(demoPageExam());
    const b = createExamBlock("p1", h.port);
    await b.refresh();
    const blk = b.el.querySelector(".ew-examb")!;
    expect(blk.querySelector(".ew-thead > h3")!.textContent).toBe("真题练习");
    expect(blk.querySelector(".ew-thead .ew-tipbox")!.textContent).toBe("来自《线性代数期末试卷 2023》《考研数学一真题》的往年题");
    expect(blk.querySelector(".ew-examq > .quiz-stub")).not.toBeNull();
    expect(h.mounted).toEqual(["demo-exam"]);
    const cards = [...blk.querySelectorAll(".ew-exi")];
    expect(cards.map((c) => c.querySelector(".ew-exh")!.textContent)).toEqual(["第 7 题《线性代数期末试卷 2023》p.3", "第 12 题《考研数学一真题》p.12"]);
    expect(cards[0]!.querySelector("details.ew-exctx > summary")!.textContent).toBe("题目材料");
    expect(btns(b.el, "e7").map((x) => x.textContent)).toEqual(["看答案"]);
    expect(btns(b.el, "e12").map((x) => x.textContent)).toEqual(["我做完了"]);
  });

  it("看答案 → the answer + 我答对了 / 没答对; a report turns them off while out, then the pick stays pressed and the block is read again", async () => {
    let release!: () => void;
    const h = harness(demoPageExam());
    const real = (id: string, r: ExamResult) => h.api.examSelf(id, r);
    h.src.examSelf = (id, r) => new Promise((ok) => { release = () => { ok(real(id, r)); }; });
    const b = createExamBlock("p1", h.port);
    await b.refresh();
    press(b.el, "e7", "看答案");
    expect(b.el.querySelector('.ew-exi[data-item="e7"] .ew-exans')!.textContent).toContain("答案：λ₁ = 2");
    press(b.el, "e7", "我答对了");
    expect(btns(b.el, "e7").map((x) => x.disabled)).toEqual([true, true]);
    press(b.el, "e7", "我答对了"); // a second tap while out does nothing
    release();
    await flush(); await flush();
    expect(btns(b.el, "e7").map((x) => [x.textContent, x.disabled, x.getAttribute("aria-pressed")])).toEqual([["我答对了", true, "true"], ["没答对", true, null]]);
    expect(h.pe).toHaveBeenCalledTimes(2);
    expect(h.api.calls.filter((c) => c.method === "examSelf").map((c) => c.args)).toEqual([["e7", "right"]]);
  });

  it("409 / duplicate: the server kept an earlier report, so this pick is not shown; no error line, the block is read again", async () => {
    // a racing report got there first: the server has it (done), this one gets 409
    const h: ReturnType<typeof harness> = harness(demoPageExam(), async (id, r) => { await h.api.examSelf(id, r); throw new ApiError(409, "这道题刚刚已经记过了"); });
    const b = createExamBlock("p1", h.port);
    await b.refresh();
    press(b.el, "e12", "我做完了");
    await flush(); await flush();
    expect(b.el.querySelector(".ew-exi .ew-err")).toBeNull();
    expect(btns(b.el, "e12").map((x) => [x.textContent, x.getAttribute("aria-pressed")])).toEqual([["今天做过了", "true"]]);
    expect(h.pe).toHaveBeenCalledTimes(2);
  });

  it("duplicate after another window said 没答对: 我答对了 is not shown as pressed, the block reads 今天做过了", async () => {
    const h = harness(demoPageExam());
    const b = createExamBlock("p1", h.port);
    await b.refresh();
    await h.api.examSelf("e7", "wrong"); // the other window
    press(b.el, "e7", "看答案");
    press(b.el, "e7", "我答对了");
    await flush(); await flush();
    expect(btns(b.el, "e7").map((x) => [x.textContent, x.getAttribute("aria-pressed")])).toEqual([["今天做过了", "true"]]);
  });

  it("any other failure: 没记上，请重试 and the buttons come back; the retry can succeed", async () => {
    let fail = true;
    const h = harness(demoPageExam());
    const real = (id: string, r: ExamResult) => h.api.examSelf(id, r);
    h.src.examSelf = (id, r) => (fail ? Promise.reject(new ApiError(500, "boom")) : real(id, r));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const b = createExamBlock("p1", h.port);
    await b.refresh();
    press(b.el, "e12", "我做完了");
    await flush();
    const err = b.el.querySelector('.ew-exi[data-item="e12"] .ew-err')!;
    expect(err.textContent).toBe("没记上，请重试");
    expect(err.getAttribute("role")).toBe("alert");
    expect(btns(b.el, "e12")[0]!.disabled).toBe(false);
    fail = false;
    press(b.el, "e12", "我做完了");
    await flush(); await flush();
    expect(b.el.querySelector(".ew-exi .ew-err")).toBeNull();
    warn.mockRestore();
  });

  it("an item the server says was reported today shows the pressed 今天做过了; a failed re-read keeps what is shown", async () => {
    const out = demoPageExam();
    out.open_items[0]!.done = true;
    const h = harness(out);
    const b = createExamBlock("p1", h.port);
    await b.refresh();
    expect(btns(b.el, "e7").map((x) => [x.textContent, x.disabled, x.getAttribute("aria-pressed")])).toEqual([["今天做过了", true, "true"]]);
    h.pe.mockRejectedValueOnce(new Error("offline"));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    await b.refresh();
    expect(b.el.querySelectorAll(".ew-exi")).toHaveLength(2);
    warn.mockRestore();
  });

  it("a pick the server no longer has (the day turned: done false on the next read) is dropped: the buttons are live again", async () => {
    const h = harness(demoPageExam());
    const b = createExamBlock("p1", h.port);
    await b.refresh();
    press(b.el, "e12", "我做完了");
    await flush(); await flush();
    expect(btns(b.el, "e12")[0]!.disabled).toBe(true);
    h.api.__setPageExam("p1", demoPageExam()); // a new day: nothing reported today
    await b.refresh();
    expect(btns(b.el, "e12").map((x) => [x.textContent, x.disabled, x.getAttribute("aria-pressed")])).toEqual([["我做完了", false, null]]);
  });

  it("the material deleted (empty answer): the block and its quiz go away", async () => {
    const h = harness(demoPageExam());
    const b = createExamBlock("p1", h.port);
    await b.refresh();
    h.api.__setPageExam("p1", null);
    await b.refresh();
    expect(b.el.childElementCount).toBe(0);
    expect(h.mounted).toEqual(["demo-exam", "-demo-exam"]);
  });

  it("mountExamBlock: inserted before `before`, read again only when the controller changes; the cleanup removes it", async () => {
    const h = harness(demoPageExam());
    const parent = document.createElement("div");
    const heading = parent.appendChild(document.createElement("h2"));
    const off = mountExamBlock(parent, "p1", h.port, heading);
    await flush();
    expect(parent.firstElementChild!.className).toBe("ew ew-inline evowiki-exam-host");
    h.notify();
    expect(h.pe).toHaveBeenCalledTimes(1);
    h.hub.current = { ...h.src };
    h.notify();
    await flush();
    expect(h.pe).toHaveBeenCalledTimes(2);
    off();
    expect(parent.childElementCount).toBe(1);
  });
});

describe("mountQuizAsset: the asset JSON first, then the evoui block", () => {
  it("mounts `asset: <id>` after the fetch, also after a failed one; never after the cleanup", async () => {
    const mount = vi.fn(() => () => undefined);
    const el = document.createElement("div");
    let done!: () => void;
    mountQuizAsset(el, "a1", () => new Promise<void>((r) => { done = r; }), mount);
    expect(mount).not.toHaveBeenCalled();
    done();
    await flush();
    expect(mount).toHaveBeenCalledWith(el, "asset: a1");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const m2 = vi.fn(() => () => undefined);
    mountQuizAsset(el, "a2", () => Promise.reject(new Error("offline")), m2);
    await flush();
    expect(m2).toHaveBeenCalledWith(el, "asset: a2");
    const m3 = vi.fn(() => () => undefined);
    const off = mountQuizAsset(el, "a3", () => Promise.resolve(), m3);
    off();
    await flush();
    expect(m3).not.toHaveBeenCalled();
    warn.mockRestore();
  });
});
