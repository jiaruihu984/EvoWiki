import { describe, expect, it } from "vitest";
import { fakeActions } from "../render-test-kit";
import { INITIAL_WIKI_STATE, type WikiState } from "../state";
import type { WriteOut } from "../schema";
import type { ActivityOut, WhyOut, WriteActivity } from "./bot-schema";
import { paintRunWrites } from "./paint-writes";
import { runItem, whenText } from "./render-runs";

const NOW = new Date(2026, 9, 15, 12, 0);
const iso = (d: number, h = 8, m = 0): string => new Date(2026, 9, d, h, m).toISOString();
const act = (o: Partial<ActivityOut>): ActivityOut => ({
  trigger_id: "t1", run_id: null, kind: "review_due", title: "", reason: "r", at: iso(15), gate_outcome: "pass", gate_reason: null, gate_detail: null, result: "suggested",
  suggestion_title: "复习 5 张", suggestion_status: "accepted", cost_usd: 0, write: null, page_id: null, ...o,
});
const WHY: WhyOut = {
  trigger: { kind: "review_due", rule_id: "review_due", rule_version: 1, reason: "到期", evidence: [], created_at: "" },
  gate: { outcome: "pass", reason: null, at: null, detail: null }, outputs: [], reaction: null,
};
const st = (o: Partial<WikiState["bot"]> = {}): WikiState => ({ ...INITIAL_WIKI_STATE, bot: { ...INITIAL_WIKI_STATE.bot, ...o } });
const draw = (run: ActivityOut, s: WikiState = st()) => { const a = fakeActions(); const row = runItem(s, run, a, "run:k"); const x = document.createElement("div"); x.append(row); return { a, row, x }; };
const tip = (row: Element): string[] => [...row.querySelectorAll(":scope .ew-tipbox > span")].map((e) => e.textContent ?? "");

describe("个性化 · 活动记录: a Bot run (app.js logItem)", () => {
  it("a run row: .ew-pz-it with the title (a link when it goes somewhere) and the ? tooltip; no cost, no kind label, no 撤销", () => {
    const { row } = draw(act({}));
    expect(row.className).toBe("ew-pz-it");
    expect(row.dataset.chg).toBe("run:k");
    expect([...row.children].map((c) => c.className)).toEqual(["ew-pz-t", "ew-qtip"]);
    expect(row.querySelector(".ew-pz-t > button.ew-pz-go")?.textContent).toBe("复习 5 张");
    const q = row.querySelector<HTMLButtonElement>("button.ew-qbtn")!;
    expect(q.querySelector(".evowiki-sr-only")?.textContent).toBe("为什么做这件事");
    expect(row.querySelector(".ew-cost, .ew-btn.ew-sm")).toBeNull();
    expect(tip(row)).toHaveLength(2);
    expect(tip(row)[0]).toBe("r");
    expect(tip(row)[1]).toMatch(/ · 开始了$/);
  });
  it("where the title goes: a review run → 今天's review; a card about a page → the page; nothing to open → plain text", () => {
    const r = draw(act({}));
    r.row.querySelector<HTMLButtonElement>("[data-action=run-go]")!.click();
    expect(r.a.setTab).toHaveBeenCalledWith("today");
    expect(r.a.startReview).toHaveBeenCalledOnce();
    const p = draw(act({ kind: "wrong_answers", title: "补讲：投影", page_id: "p1" }));
    p.row.querySelector<HTMLButtonElement>("[data-action=run-go]")!.click();
    expect(p.a.openWikiPage).toHaveBeenCalledWith("p1");
    const plain = draw(act({ kind: "goal_idle", title: "继续学习" })).row;
    expect(plain.querySelector("[data-action=run-go]")).toBeNull();
    expect(plain.querySelector(".ew-pz-t > span")?.textContent).toBe("继续学习");
  });
  it("the tooltip's lines: the reason, then the rule, then when · reaction (the chain read once on the first ask)", () => {
    const lines = tip(draw(act({}), st({ why: { t1: { kind: "ready", value: WHY } } })).row);
    expect(lines.slice(0, 2)).toEqual(["r", "规则「复习到期」v1"]);
    const { a, row } = draw(act({}));
    const w = row.querySelector<HTMLElement>(".ew-qtip")!;
    w.dispatchEvent(new Event("mouseenter"));
    w.querySelector<HTMLButtonElement>("button")!.click();
    expect(a.botToggleWhy).toHaveBeenCalledExactlyOnceWith("t1");
    const none = draw(act({ trigger_id: null }));
    none.row.querySelector<HTMLElement>(".ew-qtip")!.dispatchEvent(new Event("mouseenter"));
    expect(none.a.botToggleWhy).not.toHaveBeenCalled();
  });
  it("whenText", () => {
    expect(whenText(iso(15, 8, 5), NOW)).toBe("今天 8:05");
    expect(whenText(iso(14), NOW)).toBe("昨天 8:00");
    expect(whenText(iso(12, 9, 5), NOW)).toBe("10月12日 9:05");
  });
  it("B5: a quiet weekly review is its title alone (no second line under it)", () => {
    const { row } = draw(act({ trigger_id: "tw", kind: "goal_review", title: "每周回顾：2 个目标都正常", result: "reviewed",
      gate_detail: "每周回顾：2 个目标这周都正常", suggestion_title: null, suggestion_status: null }));
    expect(row.querySelectorAll(".ew-muted").length).toBe(0);
    expect(row.querySelector(".ew-pz-t")?.textContent).toBe("每周回顾：2 个目标都正常");
  });

  describe("page writes", () => {
    const W: WriteActivity = { page_id: "p5", title: "主成分怎么选", folder: "降维", goal: "学会 PCA", n: 3, asked: null, origin: "idle",
      action_id: "act_w5", can_undo: true, undone: false, opened: false, calls: 4, outline_calls: 1, section_calls: 3 };
    const wrow = (o: Partial<ActivityOut> = {}, w: Partial<WriteActivity> = {}): ActivityOut => act({
      trigger_id: null, run_id: "wr5", kind: "idle_write", title: "写好了「主成分怎么选」", result: "written", cost_usd: 0.055, suggestion_title: null,
      suggestion_status: null, write: { ...W, ...w }, ...o });

    it("a written page: 创建了「X」页面 (a link to the page), the ? and 撤销", () => {
      const { a, row } = draw(wrow());
      expect(row.querySelector("[data-action=run-go]")?.textContent).toBe("创建了「主成分怎么选」页面");
      row.querySelector<HTMLButtonElement>("[data-action=run-go]")!.click();
      expect(a.goToChange).toHaveBeenCalledExactlyOnceWith("p5", null);
      row.querySelector<HTMLButtonElement>("[data-action=bot-write-undo]")!.click();
      expect(a.botDecide).toHaveBeenCalledWith("act_w5", "undo");
      expect(row.textContent).not.toContain("你：");
    });
    it("its tooltip: the reason, the details (kind, calls, cost), then when · what happened to the page; nothing read from the server", () => {
      const { a, row } = draw(wrow());
      const lines = tip(row);
      expect(lines.slice(0, 2)).toEqual(["「学会 PCA」路径上的第 3 页还没生成，趁空闲先生成",
        "空闲时提前生成页面 · 花了 $0.055"]);
      expect(lines[2]).toMatch(/ · 还没打开$/);
      row.querySelector<HTMLElement>(".ew-qtip")!.dispatchEvent(new Event("mouseenter"));
      expect(a.botToggleWhy).not.toHaveBeenCalled();
    });
    it("a user-requested write's tooltip names who asked", () => {
      expect(tip(draw(wrow({ kind: "user_write" }, { opened: true, asked: "新建页面「主成分怎么选」", goal: null, n: null })).row)[0])
        .toBe("你让助手做的：新建页面「主成分怎么选」");
    });
    it("an undone write: struck with 已撤销, no link, no 撤销", () => {
      const { row } = draw(wrow({}, { undone: true, can_undo: false }));
      expect(row.querySelector(".ew-pz-t")?.classList.contains("ew-off")).toBe(true);
      expect(row.querySelector(".ew-pz-tag")?.textContent).toBe("已撤销");
      expect(row.querySelector("[data-action=run-go], [data-action=bot-write-undo]")).toBeNull();
      expect(tip(row)[2]).toMatch(/ · 你撤销了$/);
    });
    it("no 撤销 without a ledger row to undo", () => {
      expect(draw(wrow({}, { action_id: null, can_undo: false })).row.querySelector("[data-action=bot-write-undo]")).toBeNull();
    });
    it("a running write: 正在创建「X」页面 and its live line (painted in place), no 撤销; a queued one has no line", () => {
      const live: WriteOut = { page_id: "p5", run_id: "r1", kind: "organize", state: "writing", section: 1, of: 4, cost_usd: 0.033, pass: null, started_at: null };
      const { row, x } = draw(wrow({ result: "writing", cost_usd: 0 }, { action_id: null, can_undo: false }), st({ writes: { p5: live } }));
      expect(row.querySelector("[data-action=run-go]")?.textContent).toBe("正在创建「主成分怎么选」页面");
      const line = row.querySelector<HTMLElement>(".ew-muted.ew-wline")!;
      expect([line.dataset.wrun, line.textContent]).toEqual(["p5", "生成中 2/4"]);
      expect(tip(row)[1]).toBe("空闲时提前生成页面 · 正在生成 · 已花 $0.033");
      paintRunWrites(x, new Map([["p5", { ...live, section: 2 }]]));
      expect(line.textContent).toBe("生成中 3/4");
      const q: WriteOut = { ...live, state: "queued", section: null, of: null };
      expect(draw(wrow({ result: "writing" }, { action_id: null, can_undo: false }), st({ writes: { p5: q } })).row.querySelector(".ew-wline")).toBeNull();
    });
  });
});
