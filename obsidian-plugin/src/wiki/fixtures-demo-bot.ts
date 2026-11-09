/**
 * The demo's Bot data (app.js `freshBot()` runs / skipped / cards / chips and ENGINE.log rows 1–2), in the /wiki/bot/* shapes.
 * Times are relative to `now` (the demo says 今天 / 昨天 / 10月12日 = 3 days back). The demo's third page (pj, 投影) is not in the
 * fixture's pages, so its chips are left out; the 补讲 card opens p1.
 */
import type { ActionOut, ActivityOut, BotSettings, SuggestionOut, WhyOut } from "./bot/bot-schema";
import type { RsiChangeOut } from "./schema";
import { DEFAULT_BOT_SETTINGS } from "./fixtures";

const END = "2099-01-01T00:00:00Z";
const PAST = "2000-01-01T00:00:00Z";

interface RunSpec {
  id: string; day: number; hm: [number, number]; kind: string; title: string; cost: number; react: string;
  trig: string; gate: string; out: string[];
}
/** `react` is the suggestion status the user's reaction left ("" = none yet). */
const RUNS: RunSpec[] = [
  { id: "r1", day: 0, hm: [8, 0], kind: "review_due", title: "复习 3 张", cost: 0, react: "",
    trig: "3 张卡今天到期（方差在量什么、协方差、特征值和特征向量）", gate: "通过：Bot 开着，复习开着，今天的卡 1 / 5", out: ["复习 3 张"] },
  { id: "r2", day: 0, hm: [8, 0], kind: "wrong_answers", title: "补讲：投影长度为什么要除以 |b|", cost: 0.004, react: "",
    trig: "7 天里「投影 × 计算」错了 3 道不同的题（10月9日 1 道，10月12日 2 道），3 道都标了同一个误区「忘了除以 |b|」，按误区归成一件事",
    gate: "通过：补讲开着，今天的卡 2 / 5，这个误区不在冷却中",
    out: ["补讲：投影长度为什么要除以 |b|"] },
  { id: "r4", day: 0, hm: [15, 22], kind: "chat", title: "pca（2/4）的 2 条小建议", cost: 0.002, react: "",
    trig: "你打开了 pca（2/4）；这一页的确认题你上次选了「[0, 2]」", gate: "通过：陪读建议开着，这一页今天 0 / 2 条",
    out: ["λ = 0.5 的方向会怎样？", "用 [[2, 1], [1, 2]] 自己求一遍特征向量"] },
  { id: "r5", day: 1, hm: [8, 0], kind: "review_due", title: "复习 5 张", cost: 0, react: "done",
    trig: "5 张卡昨天到期", gate: "通过：今天的卡 1 / 5", out: ["复习 5 张"] },
  { id: "r6", day: 1, hm: [8, 0], kind: "wrong_answers", title: "补讲：协方差为什么会是负的", cost: 0.003, react: "not_interested",
    trig: "7 天里「协方差 × 判断符号」错了 3 道不同的题", gate: "通过：今天的卡 2 / 5",
    out: ["补讲：协方差为什么会是负的"] },
  { id: "r7", day: 3, hm: [8, 0], kind: "goal_idle", title: "继续：pca 第 2 页 · 特征值和特征向量", cost: 0, react: "accepted",
    trig: "「学会 PCA」3 天没打开相关的页", gate: "通过：今天的卡 2 / 5", out: ["继续：pca 第 2 页"] },
];
interface SkipSpec { id: string; day: number; hm: [number, number]; kind: string; title: string; reason: string; code: string; why: string }
const SKIPPED: SkipSpec[] = [
  { id: "s1", day: 0, hm: [14, 10], kind: "chat", title: "pca（1/4）的小建议", reason: "你打开了 pca（1/4）", code: "cap_reached", why: "额度满：这一页今天已经给过 2 条" },
  { id: "s2", day: 0, hm: [8, 0], kind: "wrong_answers", title: "补讲：协方差为什么会是负的", reason: "7 天里「协方差 × 判断符号」错了 3 道不同的题", code: "suppressed",
    why: "你之前点过不感兴趣，冷却到 10月17日" },
  { id: "s3", day: 0, hm: [8, 0], kind: "review_due", title: "复习提醒（同一批卡）", reason: "3 张卡今天到期", code: "cooldown", why: "冷却中：这件事今天已经提过" },
  { id: "s4", day: 1, hm: [23, 30], kind: "goal_idle", title: "继续：贝叶斯公式（2/3）", reason: "「看懂贝叶斯公式」3 天没打开相关的页", code: "paused",
    why: "静默：Bot 暂停到今天 8:00" },
];

function at(now: Date, day: number, hm: [number, number]): string {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() - day, hm[0], hm[1]).toISOString();
}

export interface DemoBotData {
  suggestions: SuggestionOut[]; rsi: RsiChangeOut[]; actions: ActionOut[]; activity: ActivityOut[]; why: Record<string, WhyOut>; settings: BotSettings;
}

export function demoBotData(now: Date): DemoBotData {
  const today = at(now, 0, [8, 0]);
  const suggestions: SuggestionOut[] = [];
  const sugg = (p: Pick<SuggestionOut, "suggestion_id" | "kind" | "title" | "why" | "action"> & Partial<SuggestionOut>): void => {
    suggestions.push({ placement: "today", page_id: null, priority: 50, status: "pending", created_at: today, expires_at: END, trigger_id: null,
      preference_action_id: null, ...p });
  };
  // today's cards k1 (review) and k2 (补讲), the demo's cards[]
  sugg({ suggestion_id: "sg_review", kind: "review_due", title: "复习 3 张", why: "今天到期：方差在量什么、协方差、特征值和特征向量",
    action: { type: "start_practice" }, priority: 80, trigger_id: "tr_r1" });
  sugg({ suggestion_id: "sg_wrong", kind: "wrong_answers", title: "投影长度为什么要除以 |b|",
    why: "最近三道投影题都忘了除以 |b|", action: { type: "open_page", page_id: "p1", ask: "为什么投影长度要除以 |b|？" },
    priority: 70, trigger_id: "tr_r2" });
  // the demo's chips[], keyed to the fixture's pages (p1, p2)
  const chip = (id: string, page: string, text: string, reason: string, trig: string | null): void => sugg({
    suggestion_id: id, kind: "chat", title: text, why: reason, placement: "page", page_id: page, action: { type: "send_to_assistant", text }, trigger_id: trig });
  chip("sg_chat_p1_1", "p1", "方差和标准差有什么区别？", "你打开了这一页", null);
  chip("sg_chat_p2_1", "p2", "λ = 0.5 的方向会怎样？", "这一页的确认题你上次选了「[0, 2]」", "tr_r4");
  chip("sg_chat_p2_2", "p2", "用 [[2, 1], [1, 2]] 自己求一遍特征向量", "这一页的确认题你上次选了「[0, 2]」", "tr_r4");

  const activity: ActivityOut[] = [];
  const why: Record<string, WhyOut> = {};
  for (const r of RUNS) {
    const trig = "tr_" + r.id;
    const when = at(now, r.day, r.hm);
    const outputs: WhyOut["outputs"] = r.out.map((title, i) => {
      let s = suggestions.find((x) => x.trigger_id === trig && x.title === title);
      if (!s) {
        // an older run's card: reacted to (or past), so no longer listed
        sugg({ suggestion_id: `sg_${r.id}_${i}`, kind: r.kind, title, why: "", action: r.kind === "wrong_answers" ? { type: "open_page", page_id: "p1" } : { type: "start_practice" }, status: r.react, expires_at: PAST,
          trigger_id: trig, created_at: when });
        s = suggestions[suggestions.length - 1]!;
      }
      return { type: "suggestion", id: s.suggestion_id, kind: r.kind, title, status: r.react === "" ? "shown" : r.react };
    });
    activity.push({ trigger_id: trig, run_id: null, kind: r.kind, reason: r.trig, at: when, gate_outcome: "pass",
      gate_reason: null, result: "suggested", suggestion_title: r.out[0] ?? null, suggestion_status: r.react === "" ? "shown" : r.react,
      cost_usd: r.cost, title: r.title, gate_detail: r.gate, write: null, page_id: suggestions.find((x) => x.trigger_id === trig)?.page_id ?? null });
    why[trig] = {
      trigger: { kind: r.kind, rule_id: r.kind, rule_version: 1, reason: r.trig, evidence: [], created_at: when },
      gate: { outcome: "pass", reason: null, at: when, detail: r.gate },
      outputs, reaction: r.react === "" ? null : { status: r.react, reacted_at: when },
    };
  }
  for (const s of SKIPPED) {
    const trig = "tr_" + s.id;
    const when = at(now, s.day, s.hm);
    activity.push({ trigger_id: trig, run_id: null, kind: s.kind, reason: s.reason, at: when, gate_outcome: "skip", gate_reason: s.code,
      result: "skipped", suggestion_title: null, suggestion_status: null, cost_usd: 0, title: s.title, gate_detail: s.why, write: null, page_id: null });
    why[trig] = {
      trigger: { kind: s.kind, rule_id: s.kind, rule_version: 1, reason: s.reason, evidence: [], created_at: when },
      gate: { outcome: "skip", reason: s.code, at: when, detail: s.why }, outputs: [], reaction: null };
  }

  // ENGINE.log rows 1–2: the Bot's page edit (done, undoable) and the page merge waiting for approval
  const row = (p: Pick<ActionOut, "action_id" | "kind" | "scope" | "level" | "status" | "source" | "summary" | "why"> & Partial<ActionOut>): ActionOut => (
    { content: {}, page_id: null, detail: "", created_at: today, decided_at: null, expires_at: null, can_undo: false, diff: [], confirm: false, cost_usd: null, made: [], revise: null, result: {}, section_id: null, ...p });
  const actions: ActionOut[] = [
    row({ action_id: "act_merge1", kind: "page_merge", scope: "delete_merge", level: "ask", status: "proposed", source: "assistant",
      summary: "把「方差（旧）」并进 pca（1/4）", why: "两页都在讲方差在量什么，旧页 9月20日 以后没打开过", page_id: "p1",
      detail: "已合并。「方差（旧）」移进了回收站，撤销能放回来。",
      diff: [{ kind: "del", text: "页面「方差（旧）」" }, { kind: "add", text: "pca（1/4）「方差在量什么」末尾加 1 段：旧页里两组成绩的例子" }] }),
    row({ action_id: "act_edit1", kind: "assistant_edit", scope: "page_body", level: "auto_undo", status: "executed", source: "bot",
      summary: "在 pca（2/4）「怎么把它们求出来」末尾补一段：λ = 1 的特征向量怎么求", why: "你在助手里两次问「λ = 1 时 v 是哪个」", page_id: "p2", section_id: "s3",
      decided_at: today, can_undo: true,
      diff: [{ kind: "ctx", text: "…解出 λ = 3 和 λ = 1。下一页的协方差矩阵也是这样求的。" },
        { kind: "add", text: "λ = 1 时，(A − I)v = 0 就是 [[1, 1], [1, 1]] v = 0，得到 v = [1, −1]。验证：A [1, −1] = [1, −1]，方向不变，长度也不变。" }] }),
  ];
  const settings: BotSettings = structuredClone(DEFAULT_BOT_SETTINGS);
  settings.kinds = { ...settings.kinds, chat: true, idle_write: true };
  settings.cooldown_hours = 72;   // the demo writes 「6 天内不再提」 on a 不感兴趣 (B3 computes ceil(cooldown_hours × 2^count / 24) days)
  const edit = actions[1]!;
  const rsi: RsiChangeOut[] = [{ patch_id: edit.action_id, track: "G", status: "personal_accepted", summary: edit.summary, why: edit.why, signals: [], before: "",
    after: "", at: new Date(now.getTime() - 60_000).toISOString(), can_undo: true, reading: "", kind: "", rule_before: "", rule_after: "", user_edited: false, layer: 0, target: "知识库", outcome: "pending",
    effect: [], effect_line: "", pages_waiting: 0, pages_failed: 0, tool_live: false }];
  return { suggestions, rsi, actions, activity, why, settings };
}
