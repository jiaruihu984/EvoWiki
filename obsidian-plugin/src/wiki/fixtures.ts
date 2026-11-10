import { INFER_PARAM_INFO, PROMPT_STEP_INFO } from "./schema";
/**
 * Scenario data for FakeWikiApi (the v5 prototype: z-score, then PCA). Concepts, questions and outlines
 * follow the E2 web window's fake data. Chinese text here is fixture content, not UI copy.
 */
import { createHash } from "node:crypto";
import type { ActionOut, ActivityOut, BotSettings, SuggestionOut, WhyOut } from "./bot/bot-schema";
import type { AssetMeta, Band, EngineParamOut, EnginePromptOut, PromptStepOut, RsiChangeOut, RsiOutcomeOut, SectionRole, SpecScope, Verdict } from "./schema";

/**
 * The demo interactive (one button + one counter): self-contained inline CSS / JS, no network. Colours come from the
 * host's theme variables (Task 8 writes their computed values on :root) with system-colour fallbacks that follow
 * prefers-color-scheme.
 */
export const DEMO_INTERACTIVE_HTML = `<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { color-scheme: light dark; }
body {
  margin: 0; padding: var(--size-4-4, 16px);
  font-family: var(--font-text, system-ui, sans-serif);
  background: var(--background-primary, Canvas); color: var(--text-normal, CanvasText);
}
.hint { color: var(--text-muted, GrayText); margin: 0 0 var(--size-4-3, 12px); }
.out { font-size: 1.6em; margin: var(--size-4-3, 12px) 0; }
button {
  font: inherit; cursor: pointer; padding: var(--size-4-2, 8px) var(--size-4-4, 16px);
  border: 1px solid var(--background-modifier-border, GrayText); border-radius: var(--radius-m, 8px);
  background: var(--interactive-accent, AccentColor); color: var(--text-on-accent, AccentColorText);
}
@media (prefers-color-scheme: dark) { .hint { opacity: 0.9; } }
</style>
</head>
<body>
<p class="hint">z 每加 1，这个数就离均值再远一个标准差。</p>
<button id="step" type="button">再远一个标准差</button>
<p class="out">z = <span id="z">0</span></p>
<script>
(function () {
  var z = 0;
  var out = document.getElementById("z");
  document.getElementById("step").addEventListener("click", function () {
    z += 1;
    out.textContent = String(z);
  });
})();
</script>
</body>
</html>
`;
/** deep/store.py sha256: hex digest of the UTF-8 bytes (computed, never hand-written: R2.2). */
export const sha256Hex = (html: string): string => createHash("sha256").update(html, "utf8").digest("hex");
export const DEMO_ASSET: AssetMeta = {
  asset_id: "demo-z-steps", page_id: "p1", section_id: "s4", interactive_id: "z-steps", title: "点一下，看 z 怎么变",
  height: 400, sha256: sha256Hex(DEMO_INTERACTIVE_HTML), kind: "html",
  check: { interactive_id: "z-steps", ok: true, console_errors: [], page_errors: [], readings: [], network_requests: 0, overflow_px: 0,
    probes_passed: 1, probes_total: 1, detail: "" },
};
/** deep/contracts.py embed_block + wikiapp/refine_apply.py asset_src, verbatim. */
export function embedBlock(meta: AssetMeta): string {
  const title = meta.title.replace(/\n/g, " ");
  return `\`\`\`embedded-html\ntype: embedded-html\nid: ${meta.interactive_id}\nsrc: assets/${meta.asset_id}.html\n`
    + `title: ${title}\nheight: ${meta.height}\n\`\`\``;
}
/** The fake's page writer (single writer): the `writer` event's model, PageOut.writer's model / at. */
export const FAKE_WRITER = { model: "fake:writer", at: "2026-10-08T08:00:00Z" } as const;

export interface CheckFixture { stem: string; options: string[]; answer: number }
export interface SectionFixture {
  id: string; heading: string; role: SectionRole; cells: string[]; cue: string; body: string; why: string;
  lookups?: string[]; check?: CheckFixture;
}
export interface OutlineFixture { profile: string; assumed: string[]; sections: SectionFixture[] }
export interface DraftQuestionFixture { cell: string; stem: string; options: string[]; answer: number }

export const CONCEPTS = {
  c1: { id: "c1", name: "z-score 标准化", aspects: ["term", "purpose", "notation", "procedure", "interpret"], prereqs: ["c2", "c3", "c4"] },
  c2: { id: "c2", name: "均值", aspects: ["term", "notation", "procedure"], prereqs: [] },
  c3: { id: "c3", name: "方差", aspects: ["term", "notation", "procedure"], prereqs: ["c2"] },
  c4: { id: "c4", name: "标准差", aspects: ["term", "notation", "procedure"], prereqs: ["c3"] },
  c5: { id: "c5", name: "PCA 主成分分析", aspects: ["term", "purpose", "procedure"], prereqs: ["c3", "c1"] },
} as const;
/** band → [m, u] */
export const MU = { known: [0.82, 0.12], likely: [0.6, 0.2], shaky: [0.4, 0.22], gap: [0.18, 0.2], none: [0.3, 0.29] } as const;
export const INITIAL_BANDS: Record<string, Band> = {
  "c1.term": "known", "c1.purpose": "likely", "c1.notation": "likely", "c1.procedure": "none", "c1.interpret": "gap",
  "c2.term": "known", "c2.notation": "known", "c2.procedure": "known",
  "c3.term": "known", "c3.notation": "shaky", "c3.procedure": "shaky",
  "c4.term": "known", "c4.notation": "likely", "c4.procedure": "shaky",
};
export const P1_TITLE = "数据标准化（z-score）";
export const DRAFT_QUESTIONS: DraftQuestionFixture[] = [
  { cell: "c1.interpret", stem: "某个学生的成绩 z = 1.5，这说明什么？", options: ["比平均分高 1.5 个标准差", "比平均分高 1.5 分", "排在前 15%", "考了 1.5 分"], answer: 0 },
  { cell: "c3.procedure", stem: "数据 2、4、6 的方差（除以 n）是多少？", options: ["8/3", "4", "2", "8"], answer: 0 },
  { cell: "c1.procedure", stem: "均值 70、标准差 10，分数 85 的 z 是多少？", options: ["1.5", "15", "0.15", "8.5"], answer: 0 },
];
export const OUTLINE_P1: OutlineFixture = {
  profile: "你会算均值，方差公式有点忘了，还没见过 z 的解读。",
  assumed: ["c2.term", "c2.notation", "c2.procedure"],
  sections: [
    { id: "s1", heading: "为什么要标准化", role: "organizer", cells: ["c1.purpose"], cue: "不同量纲先换成同一把尺子",
      body: "身高用厘米、体重用千克，两列数字没法直接比。**标准化**把每个数换成“离均值几个标准差”，不同量纲就落到同一把尺子上。",
      why: "z-score 的用途你可能会：开头一句带过" },
    { id: "s2", heading: "z 的公式", role: "line", cells: ["c1.notation"], cue: "z = (x − μ)/σ", body: "`z = (x − μ) / σ`",
      why: "z 的公式你现在是可能会 → 一行带过" },
    { id: "s3", heading: "回顾：方差与标准差", role: "recap", cells: ["c3.notation", "c3.procedure", "c4.procedure"],
      cue: "方差是平方偏差的平均，σ 是它的平方根",
      body: "方差是每个数与均值之差的平方再取平均：`σ² = Σ(x − μ)² / n`。标准差 σ 是方差的平方根，单位和原数据一样。⟦待查证：样本方差除以 n − 1⟧",
      why: "方差的计算你现在是模糊 → 提醒卡", lookups: ["样本方差除以 n − 1"] },
    { id: "s4", heading: "z = ±1 是什么意思", role: "explain", cells: ["c1.interpret", "c1.procedure"], cue: "z = ±1：离均值一个标准差",
      body: `z = 1 表示这个数比均值高出一个标准差，z = −1 表示低一个标准差。|z| 越大，这个数在整体里越少见。\n\n${embedBlock(DEMO_ASSET)}`,
      why: "z 的解读你还不会：仔细讲" },
    { id: "s5", heading: "确认一下", role: "check", cells: ["c1.interpret"], cue: "", body: "", why: "z 的解读你现在是不会 → 确认一下",
      check: { stem: "某次考试 z = −2 表示什么？", options: ["高于均值 2 分", "低于均值 2 个标准差", "排名倒数第 2", "无法判断"], answer: 1 } },
  ],
};
export const PCA_GOAL = "PCA";
export const OUTLINE_PCA: OutlineFixture = {
  profile: "你学过方差，PCA 还是新的。", assumed: [],
  sections: [
    { id: "s1", heading: "PCA 用来做什么", role: "organizer", cells: ["c5.purpose"], cue: "把很多相关的列压成少数几个方向",
      body: "数据有几十列、彼此又相关时，**PCA** 找出少数几个新方向，用它们代替原来的列，信息丢得最少。", why: "PCA 的用途还没测过：开头一句带过" },
    { id: "s2", heading: "回顾：方差", role: "recap", cells: ["c3.notation", "c3.procedure"], cue: "方差是平方偏差的平均",
      body: "方差 `σ² = Σ(x − μ)² / n` 衡量数据散开的程度。PCA 找的就是让投影后方差最大的方向。", why: "方差的计算你现在是模糊 → 提醒卡" },
    { id: "s3", heading: "主成分怎么求", role: "explain", cells: ["c5.term", "c5.procedure"], cue: "先标准化，再找方差最大的方向",
      body: "先把每列做 z-score 标准化，再算协方差矩阵，它的特征向量就是主成分，特征值越大的方向保留的方差越多。", why: "PCA 的计算还没测过：仔细讲" },
  ],
};
export const DEMO_ANSWER = "（演示回答）除以标准差是为了让单位变成“几个标准差”。";
/** A goal whose text contains this becomes a learning path (FakeWikiApi.__goal). */
export const PATH_GOAL_HINT = "统计推断";
export interface PathPageFixture { aim: string; targets: string[]; uses: string[] }
export const PATH_PAGES: PathPageFixture[] = [
  { aim: "学完这页你能说清均值和方差各自算的是什么", targets: ["c2", "c3"], uses: [] },
  { aim: "学完这页你能用标准差把数据换成 z-score", targets: ["c4", "c1"], uses: ["c3"] },
  { aim: "学完这页你能说出 PCA 每一步在做什么", targets: ["c5"], uses: ["c1", "c3"] },
];

/** The virtual v1's default rules (a few of teach/default_spec.py's, ids d01…, source "default"). */
export const DEFAULT_SPEC_ITEMS: readonly { item_id: string; scope: SpecScope; text: string }[] = [
  { item_id: "d01", scope: "any", text: "先用一两句人话说清「这是什么、解决什么问题」，再展开。" },
  { item_id: "d02", scope: "concept", text: "从学习者已经会的普通做法或日常经验讲起，指出它在哪里不够用，再引出新方法。" },
  { item_id: "d03", scope: "any", text: "学习者没见过的新词、新符号第一次出现前先用人话解释；不用一个陌生术语解释另一个。" },
  { item_id: "d04", scope: "concept", text: "尽早给一个最小但完整的例子，有具体输入和可观察的结果。" },
  { item_id: "d05", scope: "exercise", text: "确认题只考本页讲过的一个知识点，必须有唯一正确答案，错误选项对应真实的误解。" },
];

/** One measured point of a change's outcome (no anchor deltas: the plugin shows only the verdict and the effect line). */
export const outcomePoint = (changeId: string, at: string, verdict: Verdict): RsiOutcomeOut =>
  ({ change_id: changeId, at, verdict, deltas: [], note: "" });

/**
 * GET /wiki/rsi/changes, newest first. Changes apply by themselves (no awaiting_user any more): pm1 a layer-2 method
 * change (undo works in the fake), pb4 an old declined record (没有采用), pb3 in effect but its rule is not in the
 * virtual v1 (the fake starts unsaved), so undoing it is the 409 conflict the backend gives when the rule is gone
 * (spec_ops.revert), pa2 in effect and measured helpful (undo works), pb2 rolled back automatically after it made things
 * worse; candidate / gate_failed rows are there for the plugin's filter.
 */
export const RSI_CHANGES: readonly RsiChangeOut[] = [
  { patch_id: "pm1", track: "B", status: "personal_accepted", summary: "提议讲法改动时，先找出你卡在哪一步",
    why: "按卡住的那一步提出的改动，过去更常有效", signals: [], before: "", after: "",
    at: "2026-10-09T09:00:00Z", can_undo: true, reading: "", kind: "", rule_before: "", rule_after: "", user_edited: false,
    layer: 2, target: "提议方法", outcome: "pending", effect: [], effect_line: "数据还在收集：之后 3 个后续改动，满 10 个再判断", pages_waiting: 0, pages_failed: 0, tool_live: false },
  { patch_id: "pb4", track: "B", status: "rejected", summary: "讲新词之前先给一个生活里的例子",
    why: "你最近两次说新词没先讲清楚", signals: ["「标准差」还没讲就用上了", "例子太少，看不出在算什么"],
    before: "标准化把每个数换成离均值几个标准差，这样不同量纲就能放在一起比较。",
    after: "先想一个例子：身高 180 厘米、体重 80 千克，哪个更“突出”？直接比数字没意义。标准化把每个数换成离均值几个标准差，这样就能比了。",
    at: "2026-10-08T09:00:00Z", can_undo: false, reading: "你最近两次说新词没先讲清楚", kind: "讲法", rule_before: "按默认的讲法写。", rule_after: "讲新词之前先给一个生活里的例子", user_edited: false, layer: 0, target: "讲解规则", outcome: "pending", effect: [], effect_line: "", pages_waiting: 0, pages_failed: 0, tool_live: false },
  { patch_id: "pb3", track: "B", status: "personal_accepted", summary: "公式后面先用一组具体数字算一遍",
    why: "你说公式看不懂在算什么", signals: ["这个公式代进去是多少？"],
    before: "方差是每个数与均值之差的平方再取平均。",
    after: "方差是每个数与均值之差的平方再取平均。比如 2、4、6：均值 4，偏差平方 4、0、4，平均是 8/3。",
    at: "2026-10-07T09:00:00Z", can_undo: true, reading: "你说公式看不懂在算什么", kind: "讲法", rule_before: "按默认的讲法写。", rule_after: "公式后面先用一组具体数字算一遍", user_edited: false, layer: 0, target: "讲解规则",
    outcome: "no_effect", effect: [outcomePoint("pb3", "2026-10-09T09:00:00Z", "no_effect")], effect_line: "看不出差别", pages_waiting: 3, pages_failed: 0, tool_live: false },
  { patch_id: "pa2", track: "A", status: "personal_accepted", summary: "调整确认题难度的估计方式",
    why: "按你的答题记录调整", signals: [], before: "确认题预测误差 0.31", after: "确认题预测误差 0.24",
    at: "2026-10-06T09:00:00Z", can_undo: true, reading: "", kind: "", rule_before: "", rule_after: "", user_edited: false, layer: 0, target: "学习状态评估",
    outcome: "helpful", effect: [outcomePoint("pa2", "2026-10-07T09:00:00Z", "pending"), outcomePoint("pa2", "2026-10-08T09:00:00Z", "helpful"),
      outcomePoint("pa2", "2026-10-09T09:00:00Z", "helpful")], effect_line: "之后 24 道确认题：预测误差 0.31 → 0.24，有效", pages_waiting: 0, pages_failed: 0, tool_live: false },
  { patch_id: "pb2", track: "B", status: "rolled_back", summary: "每节末尾加一句小结", why: "你说一节读完抓不住重点",
    signals: ["读完不知道重点是什么"], before: "", after: "", at: "2026-10-05T09:00:00Z", can_undo: false, reading: "你说一节读完抓不住重点", kind: "讲法", rule_before: "按默认的讲法写。", rule_after: "每节末尾加一句小结", user_edited: false, layer: 0, target: "讲解规则",
    outcome: "harmful", effect: [outcomePoint("pb2", "2026-10-06T09:00:00Z", "pending"), outcomePoint("pb2", "2026-10-07T09:00:00Z", "harmful")],
    effect_line: "之后看不懂的比例 12% → 30%，变差，已自动撤回", pages_waiting: 0, pages_failed: 0, tool_live: false },
  { patch_id: "pa1", track: "A", status: "candidate", summary: "调整复习间隔的估计方式", why: "按你的复习记录调整",
    signals: [], before: "", after: "", at: "2026-10-04T09:00:00Z", can_undo: false, reading: "", kind: "", rule_before: "", rule_after: "", user_edited: false, layer: 0, target: "学习状态评估", outcome: "pending", effect: [], effect_line: "", pages_waiting: 0, pages_failed: 0, tool_live: false },
  { patch_id: "pb1", track: "B", status: "gate_failed", summary: "少用类比", why: "你说类比反而更乱",
    signals: ["这个比喻我没看懂"], before: "", after: "", at: "2026-10-03T09:00:00Z", can_undo: false, reading: "你说类比反而更乱", kind: "讲法", rule_before: "按默认的讲法写。", rule_after: "少用类比", user_edited: false, layer: 0, target: "讲解规则", outcome: "pending", effect: [], effect_line: "", pages_waiting: 0, pages_failed: 0, tool_live: false },
];

// ---- the 个性化 tab (GET /wiki/engine): verbatim from the approved demo, app.js ENGINE.prompts / ENGINE.params ----
/** app.js ENGINE.prompts, verbatim. */
export const ENGINE_PROMPTS: readonly EnginePromptOut[] = [
  { step: "拆目标和摸底", ver: "decompose@v1 · probe@v1", model: "DeepSeek · 好",
    text: "把学习者的目标拆成领域；每个前置领域只列这个目标真正用到的知识点，用顿号分隔。\n摸底从粗到细：先问学没学过，再只针对目标用到的部分出题；问几道、什么时候停由你根据回答决定。\n自报只是弱证据。" },
  { step: "列大纲", ver: "organizer@v7", model: "DeepSeek · 好",
    text: "按学习者现在掌握到哪安排每一段：会的折起来，记不清的给提醒，不会或没有证据的讲透。\n篇幅跟着主题走，东西多就多写。\n类比只用学习者确定会的东西或他的背景。" },
  { step: "写每一段", ver: "deep@v8", model: "Claude Sonnet",
    text: "每个新词第一次出现前先用人话解释；从学习者熟悉的东西讲起；例子要有能看到的结果。\n交互：先用组件库里对得上的组件，其次图表，都不合适再写积木块；不写 HTML。\n写完按自查清单逐条对照，不成立就回去改。" },
  { step: "出确认题", ver: "check@v3 · followup-check@v1", model: "DeepSeek · 快",
    text: "每题只考这一页的一个点，只有一个正确答案，3–6 个选项，外加「不知道」。\n确认题换题干也换选项，不重复原题。" },
  { step: "理解你的反馈", ver: "feedback-understand@v5", model: "DeepSeek · 好",
    text: "先用一句话复述学习者的意思；判断是不是缺了先修知识。\n是就只补那个概念；不是就把卡住的那一步拆细，不在末尾追加大段。\n「太简单了」不重复基础，直接往深里讲。" },
  { step: "复习卡", ver: "card-rewrite@v2", model: "DeepSeek · 快",
    text: "回想卡只问这一段讲的是什么；诊断测验每题对应一种可能的误解。" },
];
/**
 * app.js ENGINE.params as structured rows (labels and values verbatim; engineParamNote words the note). `path` is the
 * fake's own stand-in (the demo has none). The two claim rows' demo note 「默认 · 等确认题结果再调」 has no structured
 * form: they are default rows and read 「默认」.
 */
export const ENGINE_PARAMS: readonly EngineParamOut[] = [
  { path: "evidence_weight.quiz", ...INFER_PARAM_INFO["evidence_weight.quiz"], value: "1.0", default: true, tuned_at: null, why: null },
  { path: "evidence_weight.check", ...INFER_PARAM_INFO["evidence_weight.check"], value: "1.0", default: true, tuned_at: null, why: null },
  { path: "evidence_weight.self", ...INFER_PARAM_INFO["evidence_weight.self"], value: "0.6", default: true, tuned_at: null, why: null },
  { path: "evidence_weight.feedback", ...INFER_PARAM_INFO["evidence_weight.feedback"], value: "0.4", default: true, tuned_at: null, why: null },
  { path: "evidence_weight.ask", ...INFER_PARAM_INFO["evidence_weight.ask"], value: "0.3", default: true, tuned_at: null, why: null },
  { path: "evidence_weight.recall", ...INFER_PARAM_INFO["evidence_weight.recall"], value: "1.0", default: true, tuned_at: null, why: null },
  { path: "evidence_weight.slip_claim", ...INFER_PARAM_INFO["evidence_weight.slip_claim"], value: "0.3", default: true, tuned_at: null, why: null },
  { path: "evidence_weight.learned_claim", ...INFER_PARAM_INFO["evidence_weight.learned_claim"], value: "0.3", default: true, tuned_at: null, why: null },
  { path: "forgetting_scale", ...INFER_PARAM_INFO["forgetting_scale"], value: "× 1.0", default: false, tuned_at: "2026-10-09",
    why: "你隔 3 天复习大多还记得" },
  { path: "slip", ...INFER_PARAM_INFO["slip"], value: "0.1", default: true, tuned_at: null, why: null },
  { path: "guess_scale", ...INFER_PARAM_INFO["guess_scale"], value: "× 1.0", default: true, tuned_at: null, why: null },
  { path: "inference_strength", ...INFER_PARAM_INFO["inference_strength"], value: "1.0", default: true, tuned_at: null, why: null },
  { path: "start_known", ...INFER_PARAM_INFO["start_known"], value: "0.3", default: true, tuned_at: null, why: null },
];

// ---- Bot (/wiki/bot/*): scenario data from the approved demo (app.js freshBot) ----
/** The task-card / row labels by suggestion kind (app.js KIND); a kind not listed is not shown. */
export const BOT_KIND_LABEL: Readonly<Record<string, string>> = {
  review_due: "复习", wrong_answers: "补讲", open_question: "补讲", goal_idle: "继续目标", chat: "陪读建议", idle_write: "空闲时写页面",
  source_added: "资料更新",
  card_stale: "复习卡更新",
};
const BOT_AT = "2026-10-15T00:00:00Z";
const BOT_END = "2099-01-01T00:00:00Z";
const sugg = (part: Pick<SuggestionOut, "suggestion_id" | "kind" | "title" | "why" | "action"> & Partial<SuggestionOut>): SuggestionOut => (
  { placement: "today", page_id: null, priority: 50, status: "pending", created_at: BOT_AT, expires_at: BOT_END, trigger_id: null,
    preference_action_id: null, ...part });
/** Today's cards (review_due, wrong_answers) and one page chip set per demo page (p1: 1 chip, p2: 2 chips). */
export const BOT_SUGGESTIONS: readonly SuggestionOut[] = [
  sugg({ suggestion_id: "sg_review", kind: "review_due", title: "复习 3 张", why: "今天到期：方差在量什么、协方差、特征值和特征向量",
    action: { type: "start_practice" }, priority: 80, trigger_id: "tr_review" }),
  sugg({ suggestion_id: "sg_wrong", kind: "wrong_answers", title: "投影长度为什么要除以 |b|",
    why: "最近三道投影题都忘了除以 |b|", action: { type: "open_page", page_id: "p1", ask: "为什么投影长度要除以 |b|？" },
    priority: 70, trigger_id: "tr_wrong" }),
  sugg({ suggestion_id: "sg_chat_p1_1", kind: "chat", title: "方差和标准差有什么区别？", why: "你打开了这一页", placement: "page", page_id: "p1",
    action: { type: "send_to_assistant", text: "方差和标准差有什么区别？" }, trigger_id: "tr_chat_p1" }),
  sugg({ suggestion_id: "sg_chat_p2_1", kind: "chat", title: "λ = 0.5 的方向会怎样？", why: "这一页的确认题你上次选了「[0, 2]」", placement: "page",
    page_id: "p2", action: { type: "send_to_assistant", text: "λ = 0.5 的方向会怎样？" }, trigger_id: "tr_chat_p2" }),
  sugg({ suggestion_id: "sg_chat_p2_2", kind: "chat", title: "用 [[2, 1], [1, 2]] 自己求一遍特征向量", why: "这一页的确认题你上次选了「[0, 2]」",
    placement: "page", page_id: "p2", action: { type: "send_to_assistant", text: "用 [[2, 1], [1, 2]] 自己求一遍特征向量" },
    trigger_id: "tr_chat_p2" }),
];
const action = (part: Pick<ActionOut, "action_id" | "kind" | "scope" | "level" | "status" | "source" | "summary" | "why"> & Partial<ActionOut>): ActionOut => (
  { content: {}, page_id: null, detail: "", created_at: BOT_AT, decided_at: null, expires_at: null, can_undo: false, diff: [], confirm: false, cost_usd: null, made: [], revise: null, result: {}, section_id: null, ...part });
/** One proposed page merge (diff) and one executed Bot page edit (diff, undoable). */
export const BOT_ACTIONS: readonly ActionOut[] = [
  action({ action_id: "act_merge1", kind: "page_merge", scope: "delete_merge", level: "ask", status: "proposed", source: "assistant",
    summary: "把「方差」和「标准差」合并成一页", why: "这两页讲的几乎是同一件事", page_id: "p1",
    diff: [{ kind: "ctx", text: "方差是每个数与均值之差的平方再取平均。" }, { kind: "del", text: "标准差：另起一页，重复讲了方差的定义。" },
      { kind: "add", text: "标准差就是方差开平方，单位回到原来的单位。" }] }),
  action({ action_id: "act_edit1", kind: "assistant_edit", scope: "page_body", level: "auto_undo", status: "executed", source: "bot",
    summary: "在「特征值和特征向量」补了一个 λ = 1 的算例", why: "你问过 λ = 1 的情况，页面里没有", page_id: "p2",
    decided_at: BOT_AT, can_undo: true,
    diff: [{ kind: "ctx", text: "特征向量：被 A 作用后方向不变的向量。" },
      { kind: "add", text: "λ = 1 时，(A − I)v = 0 就是 [[1, 1], [1, 1]] v = 0，得到 v = [1, −1]。" }] }),
  action({ action_id: "act_write_p2", kind: "bot_write", scope: "new_page", level: "auto_undo", status: "executed", source: "bot",
    summary: "创建了「特征值和特征向量」页面", why: "空闲时写页面：「学会 PCA」的第 2 页", page_id: "p2", decided_at: BOT_AT, can_undo: true,
    content: { page_id: "p2", title: "特征值和特征向量", path_id: "path1" }, cost_usd: 0.055 }),
  action({ action_id: "act_write_p1", kind: "user_write", scope: "new_page", level: "auto_undo", status: "executed", source: "bot",
    summary: "创建了「方差」页面", why: "你要的页面：新建页面「方差」", page_id: "p1", decided_at: BOT_AT, can_undo: true,
    content: { page_id: "p1", title: "方差", origin: "open", asked: "新建页面「方差」" }, cost_usd: 0.071 }),
];
const activity = (part: Partial<ActivityOut> & Pick<ActivityOut, "kind" | "title" | "reason" | "result" | "at">): ActivityOut => (
  { trigger_id: null, run_id: null, gate_outcome: null, gate_reason: null, gate_detail: null, suggestion_title: null, suggestion_status: null,
    cost_usd: 0, write: null, page_id: null, ...part });
/** What the Bot did (suggested rows and silent ones). */
export const BOT_ACTIVITY: readonly ActivityOut[] = [
  activity({ trigger_id: "tr_review", kind: "review_due", reason: "3 张卡今天到期", result: "suggested", at: "2026-10-15T00:00:00Z",
    gate_outcome: "pass", gate_detail: "通过：Bot 开着，复习开着，今天的卡 1 / 5", suggestion_title: "复习 3 张", suggestion_status: "shown",
    title: "复习 3 张" }),
  activity({ trigger_id: "tr_wrong", kind: "wrong_answers", reason: "7 天里「投影 × 计算」错了 3 道不同的题", result: "suggested", page_id: "p1",
    at: "2026-10-15T00:00:00Z", gate_outcome: "pass", gate_detail: "通过：补讲开着，今天的卡 2 / 5，这个误区不在冷却中", suggestion_title: "补讲：投影长度为什么要除以 |b|",
    suggestion_status: "shown", cost_usd: 0.004, title: "补讲：投影长度为什么要除以 |b|" }),
  activity({ trigger_id: "tr_chat_p2", kind: "chat", reason: "你打开了 pca（2/4）", result: "suggested", at: "2026-10-14T07:22:00Z",
    gate_outcome: "pass", gate_detail: "通过：陪读建议开着，这一页今天 0 / 2 条", suggestion_title: "λ = 0.5 的方向会怎样？", suggestion_status: "shown",
    cost_usd: 0.002, title: "pca（2/4）的 2 条小建议" }),
  activity({ trigger_id: "tr_skip_chat", kind: "chat", reason: "你打开了 pca（1/4）", result: "skipped", at: "2026-10-14T06:10:00Z",
    gate_outcome: "skip", gate_reason: "cap_reached", title: "pca（1/4）的小建议", gate_detail: "额度满：这一页今天已经给过 2 条" }),
  activity({ trigger_id: "tr_skip_wrong", kind: "wrong_answers", reason: "7 天里「协方差 × 判断符号」错了 3 道不同的题", result: "skipped",
    at: "2026-10-14T00:00:00Z", gate_outcome: "skip", gate_reason: "suppressed", title: "补讲：协方差为什么会是负的",
    gate_detail: "你之前点过不感兴趣，冷却到 10月17日" }),
  // page writes (demo w5 / w3): the Bot's idle write, and one the learner asked the assistant for
  activity({ run_id: "wr_p2", kind: "idle_write", title: "创建了「特征值和特征向量」页面", reason: "特征值和特征向量", result: "written", at: "2026-10-15T01:14:00Z",
    cost_usd: 0.055, write: { page_id: "p2", title: "特征值和特征向量", folder: "线性代数", goal: "学会 PCA", n: 2, asked: null, origin: "idle", action_id: "act_write_p2",
      can_undo: true, undone: false, opened: false, calls: 4, outline_calls: 1, section_calls: 3 } }),
  activity({ run_id: "wr_p1", kind: "user_write", title: "创建了「方差」页面", reason: "方差", result: "written", at: "2026-10-15T01:12:00Z",
    cost_usd: 0.071, write: { page_id: "p1", title: "方差", folder: "统计", goal: null, n: null, asked: "新建页面「方差」", origin: "open", action_id: "act_write_p1",
      can_undo: true, undone: false, opened: true, calls: 5, outline_calls: 1, section_calls: 4 } }),
];
const whyOf = (kind: string, ruleId: string, reason: string, gate: string, out: WhyOut["outputs"]): WhyOut => ({
  trigger: { kind, rule_id: ruleId, rule_version: 1, reason, evidence: [], created_at: BOT_AT },
  gate: { outcome: "pass", reason: null, at: BOT_AT, detail: gate }, outputs: out, reaction: null });
/** The 为什么 chain by trigger id. */
export const BOT_WHY: Readonly<Record<string, WhyOut>> = {
  tr_review: whyOf("review_due", "review_due", "3 张卡今天到期（方差在量什么、协方差、特征值和特征向量）", "通过：Bot 开着，复习开着，今天的卡 1 / 5",
    [{ type: "suggestion", id: "sg_review", kind: "review_due", title: "复习 3 张", status: "shown" }]),
  tr_wrong: whyOf("wrong_answers", "wrong_answers", "7 天里「投影 × 计算」错了 3 道不同的题，都标了同一个误区「忘了除以 |b|」",
    "通过：补讲开着，今天的卡 2 / 5，这个误区不在冷却中",
    [{ type: "suggestion", id: "sg_wrong", kind: "wrong_answers", title: "补讲：投影长度为什么要除以 |b|", status: "shown" }]),
  tr_chat_p1: whyOf("chat", "chat_page_open", "你打开了「方差」这一页", "通过：陪读建议开着，这一页今天 0 / 2 条",
    [{ type: "suggestion", id: "sg_chat_p1_1", kind: "chat", title: "方差和标准差有什么区别？", status: "shown" }]),
  tr_chat_p2: whyOf("chat", "chat_page_open", "你打开了 pca（2/4）；这一页的确认题你上次选了「[0, 2]」", "通过：陪读建议开着，这一页今天 0 / 2 条",
    [{ type: "suggestion", id: "sg_chat_p2_1", kind: "chat", title: "λ = 0.5 的方向会怎样？", status: "shown" },
      { type: "suggestion", id: "sg_chat_p2_2", kind: "chat", title: "用 [[2, 1], [1, 2]] 自己求一遍特征向量", status: "shown" }]),
};
/** B3 defaults (bot/settings.py) with the spec's approval levels (page_body / new_page / structure 直接做可撤销, delete_merge / teaching_params / rsi 要我批准). */
export const DEFAULT_BOT_SETTINGS: BotSettings = {
  timezone: "Asia/Shanghai", paused: "no", paused_until: null,
  kinds: { review_due: true, wrong_answers: true, goal_idle: true, strategy_negative: true, source_added: true, card_stale: true, open_question: true, chat: true, idle_write: true,
    goal_review: true },
  today_cap: 5, page_cap: 2, wrong_count: 3, wrong_days: 7, idle_days: 3, open_question_days: 3, daily_hour: 8, daily_minute: 0,
  cooldown_hours: 20, failure_streak: 3, review_weekday: 0, weekly_debt_ratio: 2, weekly_debt_pages: 2, weekly_known_share: 0.5,
  weekly_park_days: 14, weekly_park_declines: 2, weekly_stuck_minutes: 60,
  levels: { user_notes: "never", page_body: "auto_undo", new_page: "auto_undo", delete_merge: "ask", structure: "auto_undo",
    teaching_params: "ask", rsi: "ask", suggestion: "auto", preference: "auto_undo", path: "auto_undo" },
};

// ---- 提示词设置 (GET /wiki/prompts): the demo ENGINE.prompts texts for steps llm/prompt_steps.STEPS has (names + descriptions generated) ----
const PS = (step: keyof typeof PROMPT_STEP_INFO, text: string): PromptStepOut => ({ step, ...PROMPT_STEP_INFO[step], text, default_text: text, edited: false, format_note: "" });
export const PROMPT_STEPS: readonly PromptStepOut[] = [
  PS("language_style", "用清楚、自然的语言解释；先说明结论，再按需要展开。"),
  PS("source_outline", "按照资料目录组织学习内容。"),
  PS("source_reference", "围绕学习目标引用资料中的相关内容。"),
  PS("understand", "识别资料的重点及必要基础知识。"),
  PS("blocks", "根据学习目标拆解学习路径，安排主题和学习顺序。"),
  PS("page_fit", "判断已有页面是否适合当前学习目标。"),
  PS("reading_unit", "识别当前资料段落的重点和前置知识。"),
  PS("source_match", "找出新资料与已有页面的关联。"),
  PS("visual_guide", "用图示说明难以只用文字表达的关系。"),
  PS("domain_naming", "用明确的学科名称归类学习内容。"),
  PS("source_summary", "概括资料重点并保留来源信息。"),
  PS("decompose", "把目标拆成一个个点：一页能讲清的就是一个点，不为了显得有结构往下拆。已经在 Wiki 里的点直接用，不重复建。"),
  PS("path", "一行一个点，一页讲一个点。每页写一个短名字和一句它回答的问题，用学习者自己的话，比如「模型是不是在背答案」。"),
  PS("outline", "按学习者现在掌握到哪安排每一段：会的折起来，记不清的给提醒，不会的讲透。"),
  PS("write", "每个新词第一次出现前先用人话解释；从学习者熟悉的东西讲起；例子要有能看到的结果。要记的做成闪卡，要练的做成练习。"),
  PS("check", "每题只考这一页的一个点，只有一个正确答案，3–6 个选项，外加「不知道」。"),
  PS("followup", "考同一个点，换题干、换选项，不能和原题太像。"),
  PS("card", "一张卡只问一件事，问法精确，答案稳定；回想卡问这一段讲的是什么。"),
  PS("assistant", "先给答案，再看情况追问。答错的题先说错在哪，再换个讲法。要改东西时先给草稿，等学习者确认。"),
  PS("compact", "把来龙去脉、定下的事、这次对话里的要求和没做完的事交给后面的回答；只把有原话为证的懂了、没懂记下来。"),
  PS("feedback", "先用一句话复述学习者的意思；判断是不是缺了先修知识，是就只补那个。"),
  PS("graph_group_naming", "每组一个短名字，用学习者能懂的词。"),
];
