import { describe, expect, it } from "vitest";
import type { ActivityOut, WhyOut } from "./bot-schema";
import { skipReason, whyLines } from "./why-text";

const trig = { kind: "review_due", rule_id: "review_due", rule_version: 2, reason: "有 5 张卡到期", evidence: [], created_at: "" };
const rule: WhyOut = {
  trigger: trig, gate: { outcome: "pass", reason: null, at: null, detail: null },
  outputs: [{ type: "suggestion", id: "s1", kind: "review_due", title: "复习 5 张", status: "accepted" }],
  reaction: { status: "accepted", reacted_at: null },
};
const act = (o: Partial<ActivityOut>): ActivityOut => ({
  trigger_id: "t", run_id: null, kind: "goal_idle", title: "t", reason: "r", at: "2026-10-15T08:00:00Z", gate_outcome: "skip", gate_reason: "cooldown", gate_detail: null,
  result: "skipped", suggestion_title: null, suggestion_status: null, cost_usd: 0, write: null, page_id: null, ...o,
});

describe("whyLines", () => {
  it("a rule-only card", () => {
    expect(whyLines(rule)).toEqual({
      触发: "规则「复习到期」v2：有 5 张卡到期", 条件: "通过",
      产出: "今天卡「复习 5 张」", 你的反应: "开始了",
    });
  });
  it("产出: a pending / shown 补讲 card says where it opens; two or more chat outputs collapse to a count", () => {
    const out = (kind: string, status: string, title = "t") => ({ type: "suggestion" as const, id: title, kind, title, status });
    expect(whyLines({ ...rule, outputs: [out("wrong_answers", "shown", "补讲")] }).产出).toBe("今天卡「补讲」，开始后打开原页并请助手补讲");
    expect(whyLines({ ...rule, outputs: [out("wrong_answers", "accepted", "补讲")] }).产出).toBe("今天卡「补讲」");
    expect(whyLines({ ...rule, outputs: [out("chat", "shown", "a"), out("chat", "shown", "b")] }).产出).toBe("助手里的 2 条小建议");
    expect(whyLines({ ...rule, outputs: [out("chat", "shown", "a")] }).产出).toBe("助手里的小建议「a」");
  });
  it("a skipped gate names its reason", () => {
    expect(whyLines({ ...rule, gate: { outcome: "skip", reason: "paused", at: null, detail: null } }).条件).toBe("跳过：Bot 已暂停");
  });
  it("defer and unknown gate outcomes", () => {
    expect(whyLines({ ...rule, gate: { outcome: "defer", reason: "busy", at: null, detail: null } }).条件).toBe("推迟：正在忙");
    expect(whyLines({ ...rule, gate: { outcome: null, reason: null, at: null, detail: null } }).条件).toBe("还没判断");
  });
  it("an accepted chat card reads the same as the row's 你：…", () => {
    const w: WhyOut = { ...rule, outputs: [{ type: "suggestion", id: "a", kind: "chat", title: "问问 X", status: "accepted" }], reaction: { status: "accepted", reacted_at: null } };
    expect(whyLines(w).你的反应).toBe("点了「问问 X」");
  });
  it("skipReason prefers gate_detail", () => {
    expect(skipReason(act({}))).toBe("今天已经提醒过");
    expect(skipReason(act({ gate_detail: "刚推过一张" }))).toBe("刚推过一张");
  });
});

describe("B5c: the chain of the Bot's own RSI cycle ends in the RSI change", () => {
  it("rsi_change outputs", () => {
    const w: WhyOut = {
      trigger: { ...trig, kind: "strategy_negative", rule_id: "strategy_negative", reason: "连续 3 次对讲解的负反馈" },
      gate: { outcome: "pass", reason: null, at: null, detail: "改了一条讲解规则" },
      outputs: [{ type: "rsi_change", id: "pb_1", kind: "rsi_B", title: "先举例再给定义", status: "personal_accepted" }],
      reaction: null,
    };
    const l = whyLines(w);
    expect(l.触发).toBe("规则「连续说讲解不清楚」v2：连续 3 次对讲解的负反馈");
    expect(l.条件).toBe("改了一条讲解规则");
    expect(l.产出).toBe("讲解规则的改动「先举例再给定义」");
  });
});
