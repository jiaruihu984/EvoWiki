/**
 * Dev-only fixture (plugin data `fixture: "demo"`, settings.ts): the REAL wiki controller, assistant and usage section
 * run on fakes seeded with the approved full demo's data (the product specification), so
 * real Obsidian can be compared side by side with the demo. Not demo mode: no banner, writes enabled, notes are written
 * to the vault (the test vault: scripts/demo-vault script).
 *
 * What app.js has and where it lives here:
 * - K (regions 线性代数 / 统计 / 降维, 8 concepts, bands, prereqs): GET /wiki/k (one `term` cell per concept, so the R3.1
 *   concept band is the demo's band).
 * - PAGES p1 / p2 with freshNote() / renderNote('p1'): GET /wiki/pages/{id}. 更新于 is not on PageOut: the note writer
 *   stamps it (the vault script writes the demo's times).
 * - CARDS: GET /wiki/review/today; confirmCard(): the confirmation question after 粗心选错了 / 我学会了.
 * - 「做完就接着学：pca 第 3 页 · 协方差矩阵」: the catalog's learning path (page 3's aim 协方差矩阵).
 * - Recommendations 机器学习 / 奇异值分解 / 相关系数: not derivable from the demo's K (frontier() would give its own
 *   frontier), so DEMO_RECOMMENDATIONS is handed to the controller as an override (fixture only).
 * - ENGINE.log: GET /wiki/rsi/changes; SET.rules + exemplars: GET /wiki/teach; usage: DemoUsageClient.
 * - freshAsst(): DemoAssistantApi (a stored session replayed; the next question gets the demo's scripted answer).
 * - Scripted outcomes: page feedback (the computable example in 「特征值和特征向量」 and a waiting 讲法 change), the check (A is right), the three follow-ups, the confirmation question, rewrites, 撤回.
 */
import type { FileOut } from "./schema";
import type {
  ActionOut, ActivityOut, BadgeOut, BotSettings, BotGoalOut, PauseUntil, Reaction, ReadingEntry, ReviewReaction, ReviewReminderOut, BotReadingOut, SourceReviseOut, SuggestionOut, WhyOut,
} from "./bot/bot-schema";
import { AbortedError, ApiError } from "../api/errors";
import type { Decision } from "../assistant/client";
import { FakeAssistantApi, type FakeStep } from "../assistant/fake-api";
import { sessionKey } from "../assistant/session-store";
import type { ActionDraft, AssistantEvent, MessageIn, ProposalDecisionOut, SessionOut } from "../assistant/types";
import {
  demoApprove, demoBlocksReply, demoBlocksStart, type DemoBlocksConv, demoConfirmedBlocks, demoFlowEvents, demoFlowOf,
} from "./fixtures-demo-asst";
import { dropBlockGoal, registerBlockGoal, TOPICS } from "./learn/goal-fake";
import type { WriteOut } from "./schema";
import type { StreamOpts, WikiApi } from "./api";
import { FakeWikiApi, provenanceOf, type RuleKind } from "./fake";
import { demoBotData } from "./fixtures-demo-bot";
import { DEMO_IX_HTML } from "./fixtures-demo-ix";
import { DEMO_IMG_B64 } from "./fixtures-demo-img";
import { DEMO_EXAM_ASSET, DEMO_EXAM_JSON, demoPageExam } from "./exam/exam-fixture";
import { embedBlock, outcomePoint, sha256Hex } from "./fixtures";
import {
  type AnswersIn, type AssetMeta, type Band,
  type ProgressOut, type StepOut, type CatalogOut, type CellBeliefOut, type CellChangeOut, type CellOut, type ChangeOut, type CheckAnswerOut,
  type CheckFollowupIn, type CheckFollowupOut, type ComponentOut, type ConfirmAnswerIn, type ConfirmAnswerOut,
  type ConfirmCardOut, type Declared, type DeclareOut,
  type DoneOut, type EngineOut, type PromptStepOut, type Exemplar, type ExemplarIn, type ShelveOut, type FeedbackMarkOut, type FeedbackOut,
  type FeedbackUndoOut, type GoalDraftOut, type GuidelineIn, type GuidelineOut,
  type GuidelinesOut, IDK, type KConceptOut,
  type KGridOut, MAX_EXEMPLARS, MAX_SPEC_ITEMS, type HandEditIn, type HandEditOut, type SignalCreatedOut, type OrganizeEvent, type PageCardOut,
  type PageFeedbackIn, type PageFeedbackOut, type PageOut, type PathNextOut, type PathOut, type PathPageDoneOut,
  type ReviewGradeIn, type ReviewGradeOut, type ReviewItemOut, type ReviewTargetOut,
  type ReviewTodayOut, type RsiChangeOut, type RsiSwitch, type SectionOut, type SectionRole, type SettingsIO,
  type SettingsPut, type RuleDecisionOut, type SpecItem, type SpecItemIn, type SpecItemPatchIn, type TeachSpecOut,
  splitCell, type GraphOut, type PathEditIn, type PathEditOut, type SplitOut,
  type SectionEditIn, type SourceDetail, type SourceEvent, type SourceOut, type SourceUploadIn, type SourceUseIn, type SourceUseOut,
  type ExamSelfIn, type ExamSelfOut, type PageExamOut,
} from "./schema";
import { CURRENT_GRAPH, GraphFixture } from "./graph-page/fixture";
import type { NodeActionOut } from "./graph-page/parse";
import type { Recommendation } from "./starmap";
import { UsageClient } from "./usage";
import type { MemoryConflictOut } from "./memory-conflicts";
import type { UsageOut, UsageSettingsIn, UsageSettingsOut } from "../generated/service";

/** The plugin-data value that switches the fixture on. */
export const DEMO_FIXTURE = "demo" as const;
/** layer2.outcomes.effect_line of a change the user undid (the backend's final point note user_undo). */
const USER_UNDO_LINE = "你撤销了";
export type FixtureName = typeof DEMO_FIXTURE;

// ---------------------------------------------------------------- K

export interface DemoRegion { id: string; name: string; at: [number, number]; prereqs: string[] }
export interface DemoConcept { name: string; region: string; band: Band; pre: string[]; page: string | null }
/** app.js K, verbatim. */
export const DEMO_K: { regions: DemoRegion[]; concepts: Record<string, DemoConcept> } = {
  regions: [
    { id: "la", name: "线性代数", at: [-0.62, -0.5], prereqs: [] },
    { id: "st", name: "统计", at: [-0.55, 0.55], prereqs: [] },
    { id: "dr", name: "降维", at: [0.62, 0.05], prereqs: ["la", "st"] },
  ],
  concepts: {
    vec: { name: "向量", region: "la", band: "known", pre: [], page: null },
    mul: { name: "矩阵乘法", region: "la", band: "known", pre: ["vec"], page: null },
    eig: { name: "特征值与特征向量", region: "la", band: "shaky", pre: ["mul"], page: "p2" },
    mean: { name: "均值", region: "st", band: "known", pre: [], page: "p1" },
    var: { name: "方差", region: "st", band: "known", pre: ["mean"], page: "p1" },
    cov: { name: "协方差", region: "st", band: "shaky", pre: ["var"], page: "p3" },
    pca: { name: "主成分分析", region: "dr", band: "none", pre: ["eig", "cov"], page: "p4" },
    proj: { name: "投影", region: "dr", band: "gap", pre: ["vec"], page: "p4" },
  },
};
/** Every concept has one aspect, so its R3.1 band is that cell's band. */
const cellOfConcept = (id: string): string => `${id}.term`;
const MU: Record<Band, [number, number]> = { known: [0.82, 0.12], likely: [0.6, 0.2], shaky: [0.4, 0.22], gap: [0.18, 0.2], none: [0.3, 0.29] };

/** app.js learnCard's three recommendations (an override: the demo's K does not produce them). */
export const DEMO_RECOMMENDATIONS: readonly Recommendation[] = [
  { conceptId: "ml", name: "机器学习", reason: "已掌握「矩阵乘法」和「方差」", pageId: null },
  { conceptId: "svd", name: "奇异值分解", reason: "已掌握「矩阵乘法」", pageId: null },
  { conceptId: "corr", name: "相关系数", reason: "已掌握「方差」", pageId: null },
];

// ---------------------------------------------------------------- pages

/** app.js PAGES. `updated` is what the vault script stamps as 更新于 (local time). */
export const DEMO_PAGES = {
  p1: { title: "pca（1/4）", goal: "能说出均值和方差各自在量什么", updated: "2026-10-08 21:40" },
  p2: { title: "pca（2/4）", goal: "能说出特征向量是什么、怎么从 Av = λv 求出来", updated: "2026-10-10 15:12" },
} as const;
export type DemoPageId = keyof typeof DEMO_PAGES;
/** The 更新于 of a demo page as a local Date (the demo-vault script passes it to the note writer). */
export function demoUpdatedAt(pageId: DemoPageId): Date {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})$/.exec(DEMO_PAGES[pageId].updated)!;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]));
}

/** app.js interactive() as an asset (`assets/demo-ix.html`), placed in s1 like the demo's "IX". */
export const DEMO_IX_ASSET: AssetMeta = {
  asset_id: "demo-ix", page_id: "p2", section_id: "s1", interactive_id: "eigen-turn", title: "转动箭头，找出不被掰弯的方向",
  height: 400, sha256: sha256Hex(DEMO_IX_HTML), kind: "html",
  check: { interactive_id: "eigen-turn", ok: true, console_errors: [], page_errors: [], readings: [], network_requests: 0, overflow_px: 0,
    probes_passed: 1, probes_total: 1, detail: "" },
};

/** app.js FIG_MONA as an image asset (`assets/demo-mona.webp`), placed in s1 after its paragraph. */
export const DEMO_IMG_ASSET: AssetMeta = {
  asset_id: "demo-mona", page_id: "p2", section_id: "s1", interactive_id: "mona-shear", title: "Mona Lisa with eigenvector",
  height: 400, sha256: sha256Hex(DEMO_IMG_B64), kind: "image",
  check: { interactive_id: "mona-shear", ok: true, console_errors: [], page_errors: [], readings: [], network_requests: 0, overflow_px: 0,
    probes_passed: 0, probes_total: 0, detail: "image 640x544 re-encoded server side" },
};
/** deep/contracts.py image_fence for DEMO_IMG_ASSET (app.js FIG_MONA's alt and caption). */
const DEMO_IMG_FENCE = ["```evowiki-image", "asset: demo-mona",
  "alt: 左边是蒙娜丽莎和两支箭头；右边是整幅画被错切推斜以后，红箭头仍然朝上，蓝箭头方向变了",
  "title: Mona Lisa with eigenvector", "author: J. Finkelstein", "license: Public domain", "source: Wikimedia Commons",
  "source_url: https://commons.wikimedia.org/wiki/File:Mona_Lisa_with_eigenvector.png", "width: 640", "height: 544", "```"].join("\n");

/** app.js CHECK. */
export const DEMO_CHECK = { stem: "A = [[2, 0], [0, 3]]，v = [0, 1]。A v 等于多少？", options: ["[0, 3]", "[0, 2]", "[2, 3]", "[3, 0]"], correct: 0 };

/** app.js's math line (`<span class="math">`), with the plugin's class prefix (the note look is T10's). */
const math = (s: string): string => `<span class="ew-math">${s}</span>`;
/** The paragraph sendFeedback() inserts into 「特征值和特征向量」. */
export const DEMO_FEEDBACK_PARAGRAPH = "举个能算的例子：A = [[3, 0], [0, 1]]，v = [1, 0]。A v = [3, 0] = 3 · [1, 0]，所以 v 是特征向量，λ = 3；换成 v = [1, 1]，A v = [3, 1]，不是 v 的倍数，就不是。";

interface SecRec {
  id: string; heading: string; role: SectionRole; cue: string; body: string; folded: boolean; pinned: boolean;
  changed: SectionOut["changed"]; cells: string[]; check: { answered: number | null; correct: boolean | null } | null;
}
interface PageRec { id: DemoPageId; sections: SecRec[]; done: boolean; feedback: FeedbackMarkOut | null }

const sec = (id: string, heading: string, role: SectionRole, body: string, cells: string[], extra: Partial<SecRec> = {}): SecRec =>
  ({ id, heading, role, cue: "", body, folded: false, pinned: false, changed: null, cells, check: null, ...extra });

/** renderNote('p1'): the 你已经懂 均值 callout, then 方差在量什么. */
function freshP1(): PageRec {
  return { id: "p1", done: true, feedback: null, sections: [
    sec("mean", "均值", "explain", "把所有数加起来再除以个数。", [cellOfConcept("mean")], { cue: "均值：一组数的「中心」在哪", folded: true }),
    sec("s1", "方差在量什么", "explain", "两组成绩平均分一样，一组很集中，一组很分散。方差把「有多分散」变成一个数：每个数离均值多远，平方后取平均。",
      [cellOfConcept("var")], { cue: "一组数有多分散：每个数离均值多远，平方后取平均。" }),
  ] };
}

/** freshNote() + renderNote('p2'): the intro, the 你已经懂 矩阵乘法 callout, s1 (with the interactive), s2 + CHECK, s3. */
function freshP2(): PageRec {
  const eig = [cellOfConcept("eig")];
  return { id: "p2", done: false, feedback: null, sections: [
    sec("intro", "开头", "organizer", "上一页你学会了方差和协方差在量什么。这一页换一个角度：一个矩阵把空间拉伸、旋转时，有没有哪些方向是「不被掰弯」的？PCA 要找的主方向，就是这样的方向。", []),
    sec("mul", "矩阵乘法", "explain", "[[a, b], [c, d]] 乘 [x, y] 得到 [ax + by, cx + dy]。", [cellOfConcept("mul")],
      { cue: "矩阵乘法：一行乘一列，对应相乘再相加", folded: true }),
    sec("s1", "先看一个变换会做什么", "explain", [
      "想象你把一张照片横向拉长两倍。大部分方向上的箭头都会被拉歪，但正好躺在水平线上的箭头只是变长，方向一点没变。",
      "这种「被变换以后方向不变、只是变长或变短」的箭头，就是这一页要讲的东西。",
      "下面这张图换成了「错切」：右边把整幅画往一侧推斜。蓝箭头被推歪，方向变了；红箭头还是笔直朝上，只是跟着画挪了位置——它就是这个变换的特征向量。",
      DEMO_IMG_FENCE,
      embedBlock(DEMO_IX_ASSET)].join("\n\n"), eig),
    sec("s2", "特征值和特征向量", "explain", [
      "矩阵 A 乘上向量 v，如果结果正好是 v 的若干倍，就写成：",
      math("A v = λ v"),
      "这时 v 叫 A 的**特征向量**，倍数 λ 叫对应的**特征值**。λ = 2 表示这个方向被拉长到 2 倍；λ = 0.5 表示被压短一半。"].join("\n\n"), eig),
    sec("s2c", "特征值和特征向量", "check", "", eig, { check: { answered: null, correct: null } }),
    sec("s3", "怎么把它们求出来", "explain", [
      "把 Av = λv 移项得到 (A − λI)v = 0。要有不为零的 v，就要求 A − λI 不可逆，也就是：",
      math("det(A − λI) = 0"),
      "对 2×2 矩阵 `[[2, 1], [1, 2]]`，这个式子是 (2 − λ)² − 1 = 0，解出 λ = 3 和 λ = 1。下一页的[协方差矩阵](#p:p3)也是这样求的。"].join("\n\n"), eig),
  ] };
}

// ---------------------------------------------------------------- 今天

/** app.js CARDS, as review items (cue / check / check) with their page · section. */
interface CardFx { cell: string; pageId: DemoPageId; sid: string; heading: string; mode: "cue" | "check"; cue: string; answer?: string;
  stem: string; options: string[]; correct: number }
export const DEMO_CARDS: readonly CardFx[] = [
  { cell: cellOfConcept("var"), pageId: "p1", sid: "s1", heading: "方差在量什么", mode: "cue",
    cue: "哪个量可以描述一组数据的离散程度？",
    answer: "方差把「有多分散」变成一个数：每个数离均值多远，平方后取平均。数越分散，方差越大。", stem: "", options: [], correct: 0 },
  { cell: cellOfConcept("cov"), pageId: "p1", sid: "cov", heading: "协方差", mode: "check", cue: "",
    stem: "两个量一个变大时另一个通常变小，它们的协方差是？", options: ["负的", "正的", "零", "不一定"], correct: 0 },
  { cell: cellOfConcept("eig"), pageId: "p2", sid: "s2c", heading: "特征值和特征向量", mode: "check", cue: "",
    stem: "A v = 2 v 说明什么？", options: ["v 被拉长到 2 倍、方向不变", "v 转了 2 度", "A 的行列式是 2", "v 的长度是 2"], correct: 0 },
];
/** app.js confirmCard(). */
export const DEMO_CONFIRM = { cell: cellOfConcept("eig"), pageId: "p2" as DemoPageId, stem: "A = [[3, 0], [0, 1]]，下面哪个是 A 的特征向量？",
  options: ["[1, 0]", "[1, 1]", "[2, 1]", "[1, 2]"], correct: 0 };

/** The learning path the two pages belong to (「做完就接着学：pca 第 3 页 · 协方差矩阵」). */
const PATH_ID = "path-pca";
const PATH_PAGES: { aim: string; targets: string[]; page: DemoPageId | null }[] = [
  { aim: "均值和方差", targets: ["mean", "var"], page: "p1" },
  { aim: "特征值和特征向量", targets: ["eig"], page: "p2" },
  { aim: "协方差矩阵", targets: ["cov"], page: null },
  { aim: "主成分分析", targets: ["pca", "proj"], page: null },
];

// ---------------------------------------------------------------- 个性化 / 设置

/** A local-noon timestamp (stable in any time zone). */
const localNoon = (month: number, day: number): string => new Date(2026, month - 1, day, 12).toISOString();
const rsiRow = (part: Partial<RsiChangeOut> & Pick<RsiChangeOut, "patch_id" | "status" | "summary" | "why" | "at">): RsiChangeOut => ({
  track: "B", signals: [], before: "", after: "", can_undo: false, reading: "", kind: "讲法", rule_before: "按默认的讲法写。",
  tool_live: false, rule_after: part.summary, user_edited: false, layer: 0, target: part.track === "A" ? "学习状态评估" : "讲解规则", outcome: "pending",
  effect: [], effect_line: "", pages_waiting: 0, pages_failed: 0, ...part });
/** `days` local days before `now`, at H:MM (the demo's 今天 / 昨天 / 10月9日 rows, relative to the fixture's clock). */
const daysAgo = (now: Date, days: number, h = 12, m = 0): string => {
  const t = new Date(now.getFullYear(), now.getMonth(), now.getDate() - days, h, m);
  // a row of today later than now (the demo opened before 9:00) would sort above what is done now: it is put a few
  // minutes before now instead, in the same order (a later hour stays the later row)
  return (t > now ? new Date(now.getTime() - ((24 - h) * 60 - m) * 1000) : t).toISOString();
};
/**
 * app.js ENGINE.log (个性化 · 活动记录), newest first: a code change waiting for review (采用 / 不要), a tool the agent made,
 * the preferences added (live, undoable), EvoWiki's automatic rollback of one (AI 撤回) and the 记忆保持系数 tuning. The
 * Bot's page edit, its pages and the page merge come from the fake's ledger (fixtures-demo-bot.ts).
 */
export function demoRsiLog(now: Date): RsiChangeOut[] {
  const rule = { kind: "", rule_before: "", rule_after: "" };
  return [
    rsiRow({ patch_id: "forgep:demo-1", track: "H", status: "awaiting_user", at: daysAgo(now, 0, 9), summary: "等你审阅的代码改动：交互块渲染前规范化",
      why: "重放 3 节赢 3 节，平 1 节；测试 12 个全过，改了 2 个文件 18 行", ...rule, target: "代码" }),
    rsiRow({ patch_id: "pg-demo-1", track: "G", status: "personal_accepted", at: daysAgo(now, 0, 8, 30), summary: "把「方差」和「协方差」放进主题「统计」",
      why: "它们都是 PCA 要先学的", ...rule, can_undo: true, target: "知识库" }),
    rsiRow({ patch_id: "pb-demo-6", status: "personal_accepted", at: daysAgo(now, 0, 8, 20), summary: "新增偏好「页面里多配几个能动手的交互组件」",
      why: "你说过「这页没有组件，我喜欢组件多一点的界面」", rule_after: "页面里多配几个能动手的交互组件", can_undo: true }),
    rsiRow({ patch_id: "forge:demo-1", track: "H", status: "personal_accepted", at: daysAgo(now, 0, 8, 10), summary: "新增了工具「判断方程式是否配平」：写化学方程式的页面会先自动检查配平，再放进页面",
      why: "你两次指出方程式没配平。试写 3 节，用这个工具的都写得更好；造工具花了 $0.03", ...rule, target: "工具", tool_live: true }),
    rsiRow({ patch_id: "pb-demo-2", status: "rolled_back", at: daysAgo(now, 1), summary: "AI 撤回偏好「每页开头先列出要用到的旧知识」",
      why: "开头太长，看得费劲", rule_after: "每页开头先列出要用到的旧知识", outcome: "harmful",
      effect: [outcomePoint("pb-demo-2", daysAgo(now, 2), "pending"), outcomePoint("pb-demo-2", daysAgo(now, 1), "harmful")],
      effect_line: "之后看不懂的比例 12% → 25%，变差" }),
    // Track A: the 记忆保持系数 tuning (θ_infer forgetting_scale), the backend's plain sentence
    rsiRow({ patch_id: "pa-demo-3", track: "A", status: "personal_accepted", at: daysAgo(now, 2, 12, 3), summary: "按你的复习记录调整了记忆保持系数",
      why: "你隔 3 天复习大多还记得", signals: ["隔 3 天复习的卡大多答对", "12 道题"], before: "确认题预测误差（log loss）0.52",
      after: "0.41", ...rule, can_undo: true, outcome: "helpful",
      effect: [outcomePoint("pa-demo-3", daysAgo(now, 1), "helpful")], effect_line: "之后 12 道确认题：预测误差 0.52 → 0.41" }),
    rsiRow({ patch_id: "pb-demo-3", status: "personal_accepted", at: daysAgo(now, 2, 12, 2), summary: "新增偏好「提出改进建议时，先找出卡住的步骤」",
      why: "这样改出来的讲法，过去更常管用", rule_after: "提出改进建议时，先找出卡住的步骤", can_undo: true }),
    rsiRow({ patch_id: "pb-demo-4", status: "personal_accepted", at: daysAgo(now, 2, 12, 1), summary: "新增偏好「公式出现前先说明要解决的问题」",
      why: "你两次说「不知道为什么要这样算」", signals: ["不知道为什么要这样算"], reading: "公式出现得太突然，不知道为什么要这样算",
      rule_after: "公式出现前先说明要解决的问题", can_undo: true, outcome: "helpful",
      effect: [outcomePoint("pb-demo-4", daysAgo(now, 1), "helpful")], effect_line: "之后看不懂的比例 30% → 12%" }),
  ];
}

const specItem = (item_id: string, text: string, scope: SpecItem["scope"], kind: RuleKind, origin: string | null = null): SpecItem =>
  ({ item_id, text, scope, domain: null, ...provenanceOf(kind, origin), rev: 1, protected: false, helpful: 0, harmful: 0 });
/**
 * app.js PREFS: 我的学习偏好 (u1, u2 the user's from the chat; a1, a2 EvoWiki's), one base rule (not listed in 个性化) and
 * one preference waiting for confirmation (Memory's backfill).
 */
const demoSpecItems = (): SpecItem[] => [
  { ...specItem("u1", "先用具体例子解释新概念", "any", "user"), origin: "user_chat" },
  { ...specItem("u2", "页面里多配几个能动手的交互组件", "any", "user", "pb-demo-6"), origin: "user_chat" },
  specItem("a1", "公式出现前先说明要解决的问题", "any", "rsi", "pb-demo-4"),
  specItem("a2", "提出改进建议时，先找出卡住的步骤", "any", "rsi", "pb-demo-3"),
  specItem("d03", "新词第一次出现时先用一句人话解释", "any", "default"),
  specItem("q1", "讲解数学概念时优先使用可计算的小例子", "any", "pending"),
];
/** The "?" lines (teach.spec_out `evidence`) of app.js PREFS. */
const DEMO_EVIDENCE: Record<string, string> = {
  u1: "你说过「例子不够」", u2: "你说过「这页没有组件，我喜欢组件多一点的界面」", a1: "你两次说「不知道为什么要这样算」",
  a2: "这样改出来的讲法，过去更常管用", q1: "你在 3 次对话里都要了能算出结果的例子",
};
/** The vault note behind SET.exemplars 「库里的笔记：PCA 读书笔记.md」 (the demo-vault script writes the same text). */
export const DEMO_READING_NOTE_PATH = "PCA 读书笔记.md";
export const DEMO_READING_NOTE = [
  "# PCA 读书笔记", "",
  "读《统计学习方法》第 16 章时记的。", "",
  "- 先看数据在哪个方向上散得最开，那个方向就是第一主成分。",
  "- 协方差矩阵的特征向量给出方向，特征值给出这个方向上的方差。",
  "- 降维就是只留前几个方向，把点投影上去。", "",
  "不懂的地方：为什么协方差矩阵的特征向量一定互相垂直？", "",
].join("\n");
export const DEMO_DAILY_NOTE_PATH = "Daily.md";
export const DEMO_DAILY_NOTE = ["# Daily", "", "- 读了 pca 第 2 页：特征值和特征向量。", "- 明天把第 3 页 · 协方差矩阵读完。", ""].join("\n");
const demoExemplars = (): Exemplar[] => [
  { exemplar_id: "ex-demo-1", kind: "vault_note", ref: DEMO_READING_NOTE_PATH, title: "PCA 读书笔记", text: DEMO_READING_NOTE },
];

/** app.js renderSettings 用量: 今天 $0.31 · 本月 $4.12, 页面 / 助手 / 出题. */
export function demoUsage(): UsageOut {
  const g = (cost_usd: number) => ({ cost_usd, input_tokens: 0, output_tokens: 0, calls: 0, replayed_calls: 0, unpriced_calls: 0, cache_read_tokens: 0, cache_write_tokens: 0 });
  return {
    today: { since: new Date(2026, 9, 10).toISOString(), total_usd: 0.31, by_group: { page: g(0.25), assistant: g(0.04), diagnostic: g(0.02) } },
    month: { since: new Date(2026, 9, 1).toISOString(), total_usd: 4.12, by_group: { page: g(3.4), assistant: g(0.52), diagnostic: g(0.2) } },
    daily_limit_usd: null, monthly_limit_usd: null,
  };
}

/** The usage section on fixed numbers (settings changes are kept in memory). */
export class DemoUsageClient extends UsageClient {
  private cur: UsageSettingsOut = { budget_confirmed: true, daily_limit_usd: null, monthly_limit_usd: null };
  constructor() {
    super(() => ({ baseUrl: "", userId: "" }), { request: () => Promise.reject(new Error("EvoWiki demo fixture: no HTTP")) });
  }
  override usage(): Promise<UsageOut> {
    const u = demoUsage();
    return Promise.resolve({ ...u, daily_limit_usd: this.cur.daily_limit_usd, monthly_limit_usd: this.cur.monthly_limit_usd });
  }
  override settings(): Promise<UsageSettingsOut> { return Promise.resolve(structuredClone(this.cur)); }
  override patch(body: UsageSettingsIn): Promise<UsageSettingsOut> {
    this.cur = { budget_confirmed: this.cur.budget_confirmed || body.confirm_budget === true,
      daily_limit_usd: body.daily_limit_usd !== undefined ? body.daily_limit_usd : this.cur.daily_limit_usd,
      monthly_limit_usd: body.monthly_limit_usd !== undefined ? body.monthly_limit_usd : this.cur.monthly_limit_usd };
    return this.settings();
  }
}

// ---------------------------------------------------------------- the wiki fake

const http = (status: number, detail: string): ApiError => new ApiError(status, detail);
const noChange = (part: Partial<ChangeOut> = {}): ChangeOut =>
  ({ rewritten: [], inserted: [], moved: [], folded: [], unfolded: [], llm_calls: 0, other_pages_stale: [], unresolved_conflicts: [],
    conflicts: [], budget_exceeded: false, ...part });

interface AnswerRec { ref: string; pageId: DemoPageId; sid: string; cell: string; stem: string; options: string[]; correct: number;
  choice: number; heading: string; followed: boolean }
interface ConfirmRec { id: string; kind: "slip" | "learned"; due: Date; outcome: "confirmed" | "refuted" | null }

export interface DemoWikiOptions { delayMs?: number; now?: () => Date }

/**
 * A WikiApi on the demo's data. Everything the demo shows is answered here from its own state; the rest (new goals,
 * 问, 展开, guidelines, the component library, rule import, the 个性化 tab) goes to a plain FakeWikiApi, which knows nothing of this data.
 */
/** Local demo math: narrow assistant blocks and the same calculation in the inserted page. */
const DEMO_MATRIX_SETUP = String.raw`先看矩阵 $A$ 和列向量 $v$：

$$
A=\begin{bmatrix}2&0\\0&3\end{bmatrix},\qquad
v=\begin{bmatrix}0\\1\end{bmatrix}
$$`;
const DEMO_MATRIX_ROWS = String.raw`一行乘一列，对应相乘再相加：

$$
\begin{aligned}
\text{第一行：}\quad&2\times0+0\times1=0\\
\text{第二行：}\quad&0\times0+3\times1=3
\end{aligned}
$$

所以结果仍朝向正上方，方向不变，长度变为原来的 **3 倍**。$v$ 是这个矩阵的特征向量，对应特征值 $\lambda=3$。`;
const DEMO_MATRIX_RESULT = String.raw`$$
Av=\begin{bmatrix}0\\3\end{bmatrix}=3v
$$`;
const DEMO_MATRIX_SECTION = `${DEMO_MATRIX_SETUP}\n\n${String.raw`$$
Av=\begin{bmatrix}2&0\\0&3\end{bmatrix}
\begin{bmatrix}0\\1\end{bmatrix}
=\begin{bmatrix}0\\3\end{bmatrix}=3v
$$`}\n\n${DEMO_MATRIX_ROWS}`;

export class DemoWikiApi implements WikiApi {
  readonly calls: { method: keyof WikiApi; args: unknown[] }[] = [];
  private readonly delayMs: number;
  private readonly now: () => Date;
  private readonly rest: FakeWikiApi;
  private readonly bands = new Map<string, Band>(Object.entries(DEMO_K.concepts).map(([id, c]) => [cellOfConcept(id), c.band]));
  private readonly declared = new Map<string, Declared>();
  private readonly pages: Record<DemoPageId, PageRec> = { p1: freshP1(), p2: freshP2() };
  private current: DemoPageId | null = null;
  private readonly exam: PageExamOut = demoPageExam();
  private readonly graded = new Set<string>();
  private readonly removedCards = new Set<string>();
  private readonly answers: AnswerRec[] = [];
  private readonly confirms: ConfirmRec[] = [];
  private rsi: RsiChangeOut[];
  private spec = { version: 3, items: demoSpecItems(), exemplars: demoExemplars() };
  /** teach.spec_out `decisions`: what the user did not adopt or removed, newest first (恢复 brings one back). */
  private decisions: RuleDecisionOut[] = [];
  private settings: SettingsIO = { review_per_day: 20, page_limit_usd: null, language: "zh" };
  private seq = 0;
  // graph.html as it is now (the real-Obsidian comparison), every row with its page
  private readonly graphFx = new GraphFixture({ pageIds: true, data: CURRENT_GRAPH });

  constructor(o: DemoWikiOptions = {}) {
    this.delayMs = o.delayMs ?? 0;
    this.now = o.now ?? ((): Date => new Date());
    this.rsi = demoRsiLog(this.now());
    // Bot: the demo's data is the fake's own seed, so react / approve / reject / undo / pause stay stateful as in the fake
    const d = demoBotData(this.now());
    this.rest = new FakeWikiApi({ delayMs: this.delayMs, now: this.now, bot: { suggestions: d.suggestions, actions: d.actions, activity: d.activity, why: d.why, settings: d.settings }, rsi: d.rsi });
  }

  // ---- reads

  openPage(pageId: string): Promise<PageOut> {
    return this.run("openPage", [pageId], () => { const p = this.page(pageId); this.current = p.id; return this.pageOut(p); });
  }
  catalog(): Promise<CatalogOut> { return this.run("catalog", [], () => this.catalogOut()); }
  reviewFirst(): Promise<void> { return Promise.resolve(); }
  progress(): Promise<ProgressOut> {
    return Promise.resolve((() => {
      const step = this.demoStep(null);
      const pages = this.pathOut(PATH_ID).pages;
      return { today: step, goals: [{ goal_id: PATH_ID, kind: "path" as const, goal: "pca", state: step.kind === "goal_done" ? "done" as const : "learning" as const,
        blocks: [], path_ids: [PATH_ID], pages_done: pages.filter((p) => p.state === "read").length, pages_total: pages.length, step }] };
    })());
  }
  /** The demo path's step (wikiapp.progress): its first page not read, `read` counted as read. */
  private demoStep(read: string | null): StepOut {
    const pages = this.pathOut(PATH_ID).pages;
    const i = pages.findIndex((p) => p.state !== "read" && p.page_id !== read);
    const pg = pages[i];
    const base = { goal_id: PATH_ID, goal: "pca", map_id: null, region_id: null, region_name: "", path_id: PATH_ID, draft_id: null, progress: null, failure: null, review_first: null };
    if (pg === undefined) return { ...base, kind: "goal_done", index: null, page_id: null, title: "" };
    return { ...base, kind: pg.state === "written" ? "read" : "start_page", index: pg.index, page_id: pg.page_id, title: pg.aim };
  }
  kGrid(): Promise<KGridOut> { return this.run("kGrid", [], () => ({ concepts: Object.keys(DEMO_K.concepts).map((id) => this.kConcept(id)) })); }
  getPath(pathId: string): Promise<PathOut> { return this.run("getPath", [pathId], () => this.pathOut(pathId)); }
  reviewToday(): Promise<ReviewTodayOut> { return this.run("reviewToday", [], () => this.todayOut()); }
  reviewPage(pageId: string): Promise<ReviewTodayOut> {
    return this.run("reviewPage", [pageId], () => {
      const t = this.todayOut();
      const items = t.items.filter((i) => i.page_id === pageId);
      return { ...t, items, due: items.length, confirms: [] };
    });
  }
  /** ENGINE.log: the ledger rows (act_*: the Bot edit, preference rows, an approved merge) come from the fake's Bot state, merged with the demo's own, newest first. */
  async rsiChanges(): Promise<RsiChangeOut[]> {
    const ledger = await this.rest.rsiChanges();
    return this.run("rsiChanges", [], () => [...ledger, ...this.rsi].sort((a, b) => Date.parse(b.at) - Date.parse(a.at)));
  }
  teachSpec(): Promise<TeachSpecOut> { return this.run("teachSpec", [], () => this.specOut()); }
  /** The demo's 个性化 opens without 待确认的记忆 (full-demo: only #conflicts shows the card). */
  memoryConflicts(): Promise<MemoryConflictOut[]> { return this.run("memoryConflicts", [], () => []); }
  getSettings(): Promise<SettingsIO> { return this.run("getSettings", [], () => this.settings); }
  assetGenerated(text: string, kind: "evoui" | "html"): Promise<{ generated: boolean }> { return this.run("assetGenerated", [text, kind], () => ({ generated: true })); }
  assetMeta(assetId: string): Promise<AssetMeta> {
    return this.run("assetMeta", [assetId], () => {
      if (assetId === DEMO_EXAM_ASSET.asset_id) return DEMO_EXAM_ASSET;
      if (assetId === DEMO_IMG_ASSET.asset_id) return DEMO_IMG_ASSET;
      if (assetId !== DEMO_IX_ASSET.asset_id) throw http(404, "没有这个交互");
      return DEMO_IX_ASSET;
    });
  }
  assetHtml(assetId: string): Promise<string> {
    return this.run("assetHtml", [assetId], () => {
      if (assetId === DEMO_EXAM_ASSET.asset_id) return DEMO_EXAM_JSON;
      if (assetId === DEMO_IMG_ASSET.asset_id) return DEMO_IMG_B64;
      if (assetId !== DEMO_IX_ASSET.asset_id) throw http(404, "没有这个交互");
      return DEMO_IX_HTML;
    });
  }

  // ---- page writes

  fold(pageId: string, sid: string, folded: boolean): Promise<SectionOut> {
    return this.run("fold", [pageId, sid, folded], () => {
      const s = this.section(pageId, sid);
      s.folded = folded; s.changed = folded ? "folded" : "unfolded";
      return this.sectionOut(s);
    });
  }
  pin(pageId: string, sid: string, pinned: boolean): Promise<SectionOut> {
    return this.run("pin", [pageId, sid, pinned], () => { const s = this.section(pageId, sid); s.pinned = pinned; return this.sectionOut(s); });
  }
  feedbackKnow(pageId: string, sid: string): Promise<FeedbackOut> {
    return this.run("feedbackKnow", [pageId, sid], () => {
      const s = this.section(pageId, sid);
      if (s.pinned) throw http(409, "这一段已钉住");
      for (const c of s.cells) this.bands.set(c, "known");
      s.folded = true; s.changed = "folded";
      return { section: this.sectionOut(s), change: noChange({ folded: [sid] }) };
    });
  }
  organize(pageId: string, on: (e: OrganizeEvent) => void, o: StreamOpts = {}): Promise<void> {
    // 按当前状态重新组织: the same outline again (the demo's reorganize changes nothing on the page).
    return this.stream("organize", [pageId], o.signal, async (emit) => {
      const p = this.page(pageId);
      await emit(() => on({ event: "outline", data: { page_id: p.id, source: "organizer", profile: "", assumed: [], dropped_checks: [],
        sections: p.sections.map((s) => ({ ...this.sectionOut(s), cue: "", body: "", changed: null })) } }));
      await emit(() => on({ event: "writer", data: { model: "demo", fallback: false, reason: "" } }));
      for (const s of p.sections) {
        if (s.role === "check") continue;
        await emit(() => on({ event: "section_delta", data: { section_id: s.id, cue: s.cue, body: s.body, heading: s.heading } }));
        await emit(() => on({ event: "section_done", data: { section_id: s.id, cached: true } }));
      }
      await emit(() => on({ event: "done", data: { page_id: p.id, llm_calls: 1, cost_usd: 0, model: "demo", fallback: false, prefetched: false } }));
    });
  }
  answerCheck(pageId: string, sid: string, choice: number): Promise<CheckAnswerOut> {
    return this.run("answerCheck", [pageId, sid, choice], () => {
      const p = this.page(pageId), s = this.section(pageId, sid);
      if (s.check === null) throw http(404, "这一段没有确认题");
      if (!Number.isInteger(choice) || choice < IDK || choice >= DEMO_CHECK.options.length) throw http(422, "选项超出范围");
      const correct = choice === IDK ? null : choice === DEMO_CHECK.correct;
      // checkCard(): a right answer moves 特征值与特征向量 to likely.
      if (correct === true) for (const c of s.cells) this.bands.set(c, "likely");
      s.check = { answered: choice, correct };
      const ref = this.answer(p.id, sid, s.cells[0] ?? "", DEMO_CHECK.stem, DEMO_CHECK.options, DEMO_CHECK.correct, choice, s.heading);
      return { correct, correct_index: DEMO_CHECK.correct, change: noChange(), answer_ref: ref };
    });
  }
  done(pageId: string): Promise<DoneOut> {
    return this.run("done", [pageId], () => { this.page(pageId).done = true; return {}; });
  }
  declare(cell: string, value: Declared | null): Promise<DeclareOut> {
    return this.run("declare", [cell, value], () => {
      if (!this.bands.has(cell)) throw http(404, "没有这个概念");
      if (value === null) { this.declared.delete(cell); this.bands.set(cell, DEMO_K.concepts[splitCell(cell).conceptId]!.band); }
      else { this.declared.set(cell, value); this.bands.set(cell, value === "know" ? "known" : "gap"); }
      return { cell: this.belief(cell), change: noChange() };
    });
  }

  // ---- feedback (sendFeedback / undoFeedback)

  pageFeedback(pageId: string, body: PageFeedbackIn): Promise<PageFeedbackOut> {
    return this.run("pageFeedback", [pageId, body], () => {
      const p = this.page(pageId);
      const text = (body.text ?? "").trim();
      if ((body.chips ?? []).length === 0 && text === "") throw http(422, "至少选一项或写一句");
      const signal_id = `sig-demo-${++this.seq}`;
      const s2 = p.sections.find((s) => s.id === "s2");
      const reading = "例子不够，想先看一个能动手算的例子再看定义。";
      const summary = "在「特征值和特征向量」里加了一个能算的例子";
      const queued = body.apply !== false && text !== "";
      const style = { kind: "讲法", before: "先给定义，再举例子。", after: "先给一个能算出结果的小例子，再给定义和公式" };
      let feedback_id: string | null = null;
      if (s2 !== undefined && !s2.body.includes(DEMO_FEEDBACK_PARAGRAPH)) {
        s2.body = `${s2.body}\n\n${DEMO_FEEDBACK_PARAGRAPH}`;
        s2.changed = "rewritten";
        feedback_id = `fb-demo-${this.seq}`;
        p.feedback = { feedback_id, at: this.now().toISOString(), summary, reading, sections: ["s2"], assets: [], can_undo: true };
      }
      return { signal_id, understood: true, reason: "",
        reading: { reading, missing: [], style, page: { summary, sections: s2 ? [{ section_id: "s2", add: DEMO_FEEDBACK_PARAGRAPH }] : [],
          component: null, component_after: null, component_config: null, component_why: "", block: null, block_why: "" } },
        dropped: [], feedback_id, summary: feedback_id === null ? "" : summary,
        sections: feedback_id === null ? [] : [{ section_id: "s2", heading: "特征值和特征向量", change: "added" }],
        component: null, missing: [], unfolded: [], rsi: queued ? "queued" : text === "" ? "no_text" : "not_requested", llm_calls: 1 };
    });
  }
  undoFeedback(pageId: string, feedbackId: string): Promise<FeedbackUndoOut> {
    return this.run("undoFeedback", [pageId, feedbackId], () => {
      const p = this.page(pageId);
      if (p.feedback === null || p.feedback.feedback_id !== feedbackId) throw http(404, "没有这条反馈");
      const s2 = p.sections.find((s) => s.id === "s2");
      if (s2) { s2.body = s2.body.replace(`\n\n${DEMO_FEEDBACK_PARAGRAPH}`, ""); s2.changed = null; }
      p.feedback = null;
      return { feedback_id: feedbackId, page_id: p.id };
    });
  }
  pageFit(pageId: string): Promise<SignalCreatedOut> {
    return this.run("pageFit", [pageId], () => { this.page(pageId); return { signal_id: `sig-demo-${++this.seq}` }; });
  }
  handEdit(pageId: string, body: HandEditIn): Promise<HandEditOut> {
    return this.run("handEdit", [pageId, body], () => ({ edit_id: null, page: this.pageOut(this.page(pageId)) }));
  }
  undoHandEdit(pageId: string, editId: string): Promise<HandEditOut> {
    return this.run("undoHandEdit", [pageId, editId], () => ({ edit_id: null, page: this.pageOut(this.page(pageId)) }));
  }

  // ---- review

  gradeReview(body: ReviewGradeIn): Promise<ReviewGradeOut> {
    return this.run("gradeReview", [body], () => {
      const card = DEMO_CARDS.find((c) => c.cell === body.cell);
      if (!card) throw http(404, "没有这张卡");
      const from = this.band(card.cell);
      this.graded.add(card.cell);
      if (body.page_round === true) this.pageRoundAnswers++;
      if ("grade" in body) {
        const to: Band = body.grade === "recalled" ? "known" : body.grade === "fuzzy" ? "likely" : "shaky";
        this.bands.set(card.cell, to);
        return { band: { cell: card.cell, from, to }, correct: null, correct_index: null, page_id: card.pageId, change: noChange(), answer_ref: "" };
      }
      const choice = body.choice ?? Number.NaN;
      if (!Number.isInteger(choice) || choice < IDK || choice >= card.options.length) throw http(422, "选项超出范围");
      const correct = choice === IDK ? null : choice === card.correct;
      const to: Band = correct === true ? from : "shaky";
      this.bands.set(card.cell, to);
      const ref = this.answer(card.pageId, card.sid, card.cell, card.stem, card.options, card.correct, choice, card.heading);
      return { band: { cell: card.cell, from, to }, correct, correct_index: card.correct, page_id: card.pageId, change: noChange(), answer_ref: ref };
    });
  }
  followup(pageId: string, sid: string, body: CheckFollowupIn): Promise<CheckFollowupOut> {
    return this.run("followup", [pageId, sid, body], () => {
      const ref = body.answer_ref ?? null;
      const mine = this.answers.filter((a) => (ref !== null ? a.ref === ref : a.pageId === pageId && a.sid === sid));
      const a = mine[mine.length - 1];
      if (!a) throw http(404, "这一段没有找到这次作答");
      if (a.choice === a.correct) throw http(422, "这次作答没有答错");
      if (a.followed) throw http(409, "这道题已经选过「为什么答错」了");
      if (body.choice === "slip" && a.choice === IDK) throw http(422, "选了「不知道」不算粗心");
      a.followed = true;
      const id = `fu-demo-${++this.seq}`;
      const { conceptId } = splitCell(a.cell);
      const name = DEMO_K.concepts[conceptId]?.name ?? conceptId;
      const band: CellChangeOut = { cell: a.cell, from: this.band(a.cell), to: this.band(a.cell) };
      const base = { followup_id: id, choice: body.choice, cell: a.cell, concept_name: name, llm_calls: 0, suggest: null };
      if (body.choice === "unclear") {
        // followups(): 「去复习：<page> · <section>」 and the question handed to the assistant.
        const review: ReviewTargetOut = { page_id: a.pageId, page_title: DEMO_PAGES[a.pageId].title, section_id: a.sid === "s2c" ? "s2" : a.sid, heading: a.heading };
        const text = `题目：${a.stem}\n我选的：${a.choice === IDK ? "不知道" : a.options[a.choice] ?? ""}\n正确答案：${a.options[a.correct] ?? ""}`;
        return { ...base, status: "done", confirm_due: null, band, review, quote: { kind: "wrong_answer", text, heading: a.heading }, signal_id: `sig-demo-${this.seq}` };
      }
      // 粗心选错了 / 我学会了: confirmCard() is due at once (TODAY.confirms = 1).
      const due = this.now();
      this.confirms.push({ id, kind: body.choice, due, outcome: null });
      return { ...base, status: "pending", confirm_due: due.toISOString(), band, review: null, quote: null, signal_id: null };
    });
  }
  confirmAnswer(followupId: string, body: ConfirmAnswerIn): Promise<ConfirmAnswerOut> {
    return this.run("confirmAnswer", [followupId, body], () => {
      const f = this.confirms.find((x) => x.id === followupId);
      if (!f) throw http(404, "没有这道确认题");
      if (f.outcome !== null) throw http(409, "这道确认题已经答过了");
      const choice = body.choice;
      if (!Number.isInteger(choice) || choice < IDK || choice >= DEMO_CONFIRM.options.length) throw http(422, "没有这个选项");
      const correct = choice === IDK ? null : choice === DEMO_CONFIRM.correct;
      f.outcome = correct === true ? "confirmed" : "refuted";
      const from = this.band(DEMO_CONFIRM.cell), to: Band = correct === true ? "likely" : "shaky";
      this.bands.set(DEMO_CONFIRM.cell, to);
      const reteach: ReviewTargetOut | null = f.kind === "learned" && f.outcome === "refuted"
        ? { page_id: "p2", page_title: DEMO_PAGES.p2.title, section_id: "s2", heading: "特征值和特征向量" } : null;
      return { followup_id: f.id, correct, correct_index: DEMO_CONFIRM.correct, outcome: f.outcome, band: { cell: DEMO_CONFIRM.cell, from, to },
        reteach, signal_id: reteach ? `sig-demo-${++this.seq}` : null };
    });
  }

  // ---- path

  pathNext(pathId: string): Promise<PathNextOut> {
    // 「继续：pca 第 3 页 · 协方差矩阵」: the demo has only pages 1 and 2 (「演示里只做了第 1、2 页」).
    return this.run("pathNext", [pathId], () => { this.pathOut(pathId); throw http(503, "演示里只做了第 1、2 页"); });
  }
  pathPageDone(pathId: string, index: number): Promise<PathPageDoneOut> {
    return this.run("pathPageDone", [pathId, index], () => {
      const pg = PATH_PAGES[index - 1];
      if (pathId !== PATH_ID || pg === undefined) throw http(404, "找不到这一页");
      if (pg.page === null) throw http(409, "这一页还没开始");
      this.pages[pg.page].done = true;
      return { path: this.pathOut(pathId), done: {} };
    });
  }

  // ---- RSI changes

  undoPatch(patchId: string): Promise<RsiChangeOut> {
    if (patchId.startsWith("act_")) return this.rest.undoPatch(patchId);
    return this.run("undoPatch", [patchId], () => {
      const c = this.rsi.find((x) => x.patch_id === patchId);
      if (!c) throw http(404, "没有这条改动");
      if (c.status !== "personal_accepted" || !c.can_undo) throw http(409, "这条改动现在不能撤销");
      c.status = "rolled_back"; c.can_undo = false; c.effect_line = USER_UNDO_LINE;
      // the backend clears the marks the undo restored and marks pages rewritten under the rule; none is in the demo
      c.pages_waiting = 0; c.pages_failed = 0;
      this.spec = { ...this.spec, version: this.spec.version + 1, items: this.spec.items.filter((i) => i.origin_ref !== c.patch_id) };
      return c;
    });
  }

  /** The code change waiting for review (forgep:demo-1): 采用 puts it in effect, 不要 drops it; any other id: the fake answers. */
  approvePatch(patchId: string): Promise<RsiChangeOut> { return this.review("approvePatch", patchId, "personal_accepted") ?? this.rest.approvePatch(patchId); }
  discardPatch(patchId: string): Promise<RsiChangeOut> { return this.review("discardPatch", patchId, "rejected") ?? this.rest.discardPatch(patchId); }
  private review(method: keyof WikiApi, patchId: string, to: RsiChangeOut["status"]): Promise<RsiChangeOut> | null {
    const c = this.rsi.find((x) => x.patch_id === patchId && x.status === "awaiting_user");
    if (c === undefined) return null;
    return this.run(method, [patchId], () => { c.status = to; return c; });
  }

  // ---- settings + teaching spec

  putSettings(s: SettingsPut): Promise<SettingsIO> {
    return this.run("putSettings", [s], () => {
      this.settings = { ...this.settings,
        ...(s.review_per_day !== undefined ? { review_per_day: s.review_per_day } : {}), ...(s.language !== undefined ? { language: s.language } : {}) };
      return this.settings;
    });
  }
  addSpecItem(body: SpecItemIn, base?: number): Promise<TeachSpecOut> {
    return this.specWrite("addSpecItem", [body, base], base, () => {
      const text = body.text.trim();
      this.spec.items.push({ ...specItem(`u-demo-${++this.seq}`, text, body.scope ?? "any", "user"), domain: body.domain ?? null });
      this.decisions = this.decisions.filter((d) => d.text !== text);   // reaccepted
    });
  }
  takeOverSpecItem(itemId: string, base?: number): Promise<TeachSpecOut> {
    return this.specWrite("takeOverSpecItem", [itemId, base], base, () => {
      const it = this.spec.items.find((i) => i.item_id === itemId);
      if (!it) throw http(404, "没有这一条");
      if (it.authority === "user") throw http(422, "这条已经是你的规则了");
      it.authority = "user";
    });
  }
  editSpecItem(itemId: string, body: SpecItemPatchIn, base?: number): Promise<TeachSpecOut> {
    return this.specWrite("editSpecItem", [itemId, body, base], base, () => {
      const it = this.spec.items.find((i) => i.item_id === itemId);
      if (!it) throw http(404, "没有这一条");
      if (body.text !== undefined && body.text !== null) { it.text = body.text.trim(); it.authority = "user"; }
      if (body.scope !== undefined && body.scope !== null) it.scope = body.scope;
      if (body.domain !== undefined) it.domain = body.domain;
    });
  }
  deleteSpecItem(itemId: string, base?: number): Promise<TeachSpecOut> {
    return this.specWrite("deleteSpecItem", [itemId, base], base, () => {
      const it = this.spec.items.find((i) => i.item_id === itemId);
      if (!it) throw http(404, "没有这一条");
      this.spec.items = this.spec.items.filter((i) => i.item_id !== itemId);
      this.decisions.unshift({ decision_id: `rd-demo-${++this.seq}`, rule_id: itemId, text: it.text,
        action: it.authority === "pending" ? "rejected" : "revoked", via: "ui" });
    });
  }
  addExemplar(body: ExemplarIn, base?: number): Promise<TeachSpecOut> {
    return this.specWrite("addExemplar", [body, base], base, () => {
      if (this.spec.exemplars.length >= MAX_EXEMPLARS) throw http(422, `范例最多 ${MAX_EXEMPLARS} 个`);
      const id = `ex-demo-${++this.seq}`;
      this.spec.exemplars.push(body.kind === "vault_note"
        ? { exemplar_id: id, kind: "vault_note", ref: body.ref, title: body.title, text: body.text }
        : { exemplar_id: id, kind: "wiki_page", ref: body.ref, title: this.page(body.ref).id === "p1" ? DEMO_PAGES.p1.title : DEMO_PAGES.p2.title, text: "" });
    });
  }
  deleteExemplar(exemplarId: string, base?: number): Promise<TeachSpecOut> {
    return this.specWrite("deleteExemplar", [exemplarId, base], base, () => {
      if (!this.spec.exemplars.some((e) => e.exemplar_id === exemplarId)) throw http(404, "没有这个样板页");
      this.spec.exemplars = this.spec.exemplars.filter((e) => e.exemplar_id !== exemplarId);
    });
  }

  // ---- not part of the demo: the plain fake

  answerDraft(draftId: string, body: AnswersIn): Promise<GoalDraftOut> { return this.rest.answerDraft(draftId, body); }
  expand(pageId: string): Promise<ShelveOut> { return this.rest.expand(pageId); }
  collapse(pageId: string): Promise<ShelveOut> { return this.rest.collapse(pageId); }
  guidelines(): Promise<GuidelinesOut> { return this.rest.guidelines(); }
  /** 个性化 tab: the demo's ENGINE.prompts / ENGINE.params, which the plain fake already serves (fixtures.ts). */
  engine(): Promise<EngineOut> { return this.rest.engine(); }
  prompts(): Promise<PromptStepOut[]> { return this.rest.prompts(); }
  putPrompt(step: string, text: string): Promise<PromptStepOut> { return this.rest.putPrompt(step, text); }
  resetPrompt(step: string): Promise<PromptStepOut> { return this.rest.resetPrompt(step); }
  addGuideline(body: GuidelineIn): Promise<GuidelineOut> { return this.rest.addGuideline(body); }
  deleteGuideline(id: string): Promise<void> { return this.rest.deleteGuideline(id); }
  components(): Promise<ComponentOut[]> { return this.rest.components(); }
  // Bot: the fake, seeded with the demo's data (constructor)
  botSuggestions(placement: "today" | "page", pageId?: string): Promise<SuggestionOut[]> { return this.rest.botSuggestions(placement, pageId); }
  botReact(id: string, reaction: Reaction): Promise<SuggestionOut> { return this.rest.botReact(id, reaction); }
  botReviewReminder(): Promise<ReviewReminderOut> { return this.rest.botReviewReminder(); }
  botReviewReact(reaction: ReviewReaction): Promise<ReviewReminderOut> { return this.rest.botReviewReact(reaction); }
  botActions(status?: string): Promise<ActionOut[]> { return this.rest.botActions(status); }
  botDecide(id: string, op: "approve" | "reject" | "undo"): Promise<ActionOut> { return this.rest.botDecide(id, op); }
  botWhy(q: { triggerId: string } | { suggestionId: string }): Promise<WhyOut> { return this.rest.botWhy(q); }
  botRecent(skipped: boolean): Promise<ActivityOut[]> { return this.rest.botRecent(skipped); }
  writes(pageIds?: string[]): Promise<WriteOut[]> { return this.rest.writes(pageIds); }
  /** Dev fixture: the assistant's ledger row (fixtures-demo-asst.ts) is also the 活动记录 row, one object per id. */
  putBotAction(a: ActionOut, ledger: Partial<RsiChangeOut> = {}): void { this.rest.putBotAction(a, ledger); }
  /**
   * Dev fixture (Task 16 comparison): app.js boot's idle write of 「降维丢了多少信息」 (p6), running at section `sec` of its 4, as
   * the first Bot 最近做了什么 row (started at `at`) and its GET /wiki/writes run.
   */
  seedWritingRun(at: Date, sec: number): void {
    const cost = +(0.009 + 0.016 * sec).toFixed(3);
    this.rest.putActivity({ trigger_id: null, run_id: "wr_demo_p6", kind: "idle_write", title: "写「降维丢了多少信息」", reason: "降维丢了多少信息",
      at: at.toISOString(), gate_outcome: "pass", gate_reason: null, gate_detail: "通过：空闲时写页面开着，没有别的页面在写", result: "writing",
      suggestion_title: null, suggestion_status: null, cost_usd: cost, page_id: null,
      write: { page_id: "p6", title: "降维丢了多少信息", folder: "降维", goal: "学会 PCA", n: 4, asked: null, origin: "idle", action_id: null,
        can_undo: false, undone: false, opened: false, calls: 0, outline_calls: 1, section_calls: sec } });
    this.rest.writesNow = [...this.rest.writesNow.filter((w) => w.page_id !== "p6"),
      { page_id: "p6", run_id: "r1", kind: "idle_write", state: "writing", section: sec, of: 4, started_at: at.toISOString(), cost_usd: cost, pass: null }];
  }
  /** Dev fixture (Task 16 comparison): the runs GET /wiki/writes answers with, for the assistant cards and the wiki alike. */
  setWrites(w: WriteOut[]): void { this.rest.writesNow = w; }
  getPage(pageId: string): Promise<PageOut> { return this.run("getPage", [pageId], () => this.pageOut(this.page(pageId))); }
  files(): Promise<FileOut[]> { return this.rest.files(); }
  runStream(pageId: string, runId: string, on: (e: OrganizeEvent) => void, o?: StreamOpts): Promise<void> { return this.rest.runStream(pageId, runId, on, o); }
  cancelRun(pageId: string): Promise<void> { return this.rest.cancelRun(pageId); }
  botBadge(): Promise<BadgeOut> { return this.rest.botBadge(); }
  botSettings(): Promise<BotSettings> { return this.rest.botSettings(); }
  rsiSwitch(): Promise<RsiSwitch> { return this.rest.rsiSwitch(); }
  putRsiSwitch(on: boolean): Promise<RsiSwitch> { return this.rest.putRsiSwitch(on); }
  putBotSettings(s: BotSettings): Promise<BotSettings> { return this.rest.putBotSettings(s); }
  botPause(until: PauseUntil): Promise<BotSettings> { return this.rest.botPause(until); }
  botGoals(): Promise<BotGoalOut[]> { return this.rest.botGoals(); }
  reviseFromSource(sourceId: string, pageIds: string[]): Promise<SourceReviseOut> { return this.rest.reviseFromSource(sourceId, pageIds); }
  botPark(pathId: string, park: boolean): Promise<BotGoalOut> { return this.rest.botPark(pathId, park); }
  replanPath(pathId: string): Promise<PathEditOut> { return this.run("replanPath", [pathId], () => this.graphFx.pathOf(pathId)); }
  postReading(batchId: string, entries: ReadingEntry[]): Promise<BotReadingOut> { return this.rest.postReading(batchId, entries); }
  blockEvents(): Promise<{ inK: boolean }> { return Promise.resolve({ inK: false }); }
  blockProgress(): Promise<{ revision: number; data: null }> { return Promise.resolve({ revision: 0, data: null }); }
  saveBlockProgress(): Promise<{ stale: boolean }> { return Promise.resolve({ stale: false }); }

  // ---- 知识图谱: graph.html's data (graph-page/fixture.ts)
  graph(): Promise<GraphOut> { return this.run("graph", [], () => this.graphFx.graph()); }
  splitNode(nodeId: string): Promise<SplitOut> { return this.run("splitNode", [nodeId], () => this.graphFx.split(nodeId)); }
  wantNode(nodeId: string, mapId: string): Promise<NodeActionOut> { return this.run("wantNode", [nodeId, mapId], () => this.graphFx.want(nodeId, mapId)); }
  editPath(pathId: string, edit: PathEditIn): Promise<PathEditOut> { return this.run("editPath", [pathId, edit], () => this.graphFx.editPath(pathId, edit)); }
  splitRow(pathId: string, pageKey: string): Promise<PathEditOut> { return this.run("splitRow", [pathId, pageKey], () => this.graphFx.splitRow(pathId, pageKey)); }
  undoSplitRow(pathId: string, pageKey: string, changeId: string): Promise<PathEditOut> {
    return this.run("undoSplitRow", [pathId, pageKey, changeId], () => this.graphFx.undoSplitRow(pathId, pageKey, changeId));
  }

  // ---- 资料 (/wiki/sources): the plain fake's in-memory sources (the demo has no sources of its own yet)
  listSources(): Promise<SourceOut[]> { return this.rest.listSources(); }
  getSource(sourceId: string): Promise<SourceDetail> { return this.rest.getSource(sourceId); }
  uploadSource(input: SourceUploadIn): Promise<SourceOut> { return this.rest.uploadSource(input); }
  followSource(sourceId: string, on: (e: SourceEvent) => void, o?: StreamOpts): Promise<void> { return this.rest.followSource(sourceId, on, o); }
  addSourceUse(sourceId: string, use: SourceUseIn): Promise<SourceUseOut> { return this.rest.addSourceUse(sourceId, use); }
  renameSource(sourceId: string, title: string): Promise<SourceOut> { return this.rest.renameSource(sourceId, title); }
  setSourceOrigin(sourceId: string, origin: string): Promise<SourceOut> { return this.rest.setSourceOrigin(sourceId, origin); }
  editSections(sourceId: string, edits: SectionEditIn[]): Promise<SourceDetail> { return this.rest.editSections(sourceId, edits); }
  retrySource(sourceId: string): Promise<SourceOut> { return this.rest.retrySource(sourceId); }
  deleteSource(sourceId: string): Promise<void> { return this.rest.deleteSource(sourceId); }
  sourceFile(sourceId: string): Promise<ArrayBuffer> { return this.rest.sourceFile(sourceId); }

  // ---- 真题 (app.js EXAM): page p2 has the 真题练习 block; a report marks the item done, a second one is a duplicate
  pageExam(pageId: string): Promise<PageExamOut> {
    return this.run("pageExam", [pageId], () => (pageId === "p2" ? this.exam
      : { asset_id: null, quiz_count: 0, open_items: [], sources: [], frequent: false }));
  }
  examSelf(itemId: string, result: ExamSelfIn["result"]): Promise<ExamSelfOut> {
    return this.run("examSelf", [itemId, result], () => {
      const it = this.exam.open_items.find((x) => x.item_id === itemId);
      if (it === undefined) throw http(404, "没有这道题");
      if (result !== "done" && !it.has_key) throw http(422, "这道题没有答案，只能标做完");
      if (it.done) return { status: "duplicate" as const };
      it.done = true;
      return { status: result === "done" ? "done" as const : "score" as const };
    });
  }

  /** The assistant's 加进去 (DemoAssistantApi.decide): app.js adds 「为什么 A v 是 [0, 3]」 after 「特征值和特征向量」. */
  addAssistantSection(): string[] {
    const p = this.pages.p2;
    if (p.sections.some((s) => s.id === "s2b")) return [];
    const at = p.sections.findIndex((s) => s.id === "s3");
    p.sections.splice(at < 0 ? p.sections.length : at, 0, sec("s2b", "为什么 A v 是 [0, 3]", "explain",
      DEMO_MATRIX_SECTION,
      [cellOfConcept("eig")], { changed: "inserted" }));
    return ["s2b"];
  }

  // ---- helpers

  private run<T>(method: keyof WikiApi, args: unknown[], fn: () => T): Promise<T> {
    this.calls.push({ method, args });
    try { return Promise.resolve(structuredClone(fn())); } catch (e) { return Promise.reject(e instanceof Error ? e : new Error(String(e))); }
  }
  /** SSE: each emit waits delayMs (not before the first) and stops on abort; errors before the first event reject. */
  private async stream(method: keyof WikiApi, args: unknown[], signal: AbortSignal | undefined,
    body: (emit: (deliver: () => void) => Promise<void>) => Promise<void>): Promise<void> {
    this.calls.push({ method, args });
    let first = true;
    await body(async (deliver) => {
      if (!first && this.delayMs > 0) await new Promise((r) => setTimeout(r, this.delayMs));
      first = false;
      if (signal?.aborted) throw new AbortedError();
      deliver();
    });
  }
  private specWrite(method: keyof WikiApi, args: unknown[], base: number | undefined, change: () => void): Promise<TeachSpecOut> {
    return this.run(method, args, () => {
      if (base !== undefined && base !== this.spec.version) throw http(409, "讲解方式已经变了");
      change();
      this.spec.version += 1;
      return this.specOut();
    });
  }
  private page(pageId: string): PageRec {
    if (pageId !== "p1" && pageId !== "p2") throw http(404, "找不到这一页");
    return this.pages[pageId];
  }
  private section(pageId: string, sid: string): SecRec {
    const s = this.page(pageId).sections.find((x) => x.id === sid);
    if (!s) throw http(404, "找不到这一段");
    return s;
  }
  private band(cell: string): Band { return this.bands.get(cell) ?? "none"; }
  private belief(cell: string): CellBeliefOut {
    const { conceptId } = splitCell(cell);
    const b = this.band(cell);
    const [m, u] = MU[b];
    return { cell, concept_id: conceptId, aspect: "term", band: b, m, u, recall: b === "none" ? 0 : m, direct: b === "none" ? 0 : 1,
      inferred_pos: 0, inferred_neg: 0, inferred_only: false, declared: this.declared.get(cell) ?? null };
  }
  private cellOut(cell: string): CellOut {
    const { conceptId } = splitCell(cell);
    return { cell, concept_id: conceptId, concept_name: DEMO_K.concepts[conceptId]?.name ?? conceptId, aspect: "term", band: this.band(cell),
      declared: this.declared.get(cell) ?? null };
  }
  private kConcept(id: string): KConceptOut {
    const c = DEMO_K.concepts[id]!;
    const onPage = this.current !== null && this.pages[this.current].sections.some((s) => s.cells.includes(cellOfConcept(id)));
    return { id, name: c.name, role: null, aspects: ["term"], prereqs: [...c.pre], on_current_page: onPage, cells: [this.belief(cellOfConcept(id))], notes: [] };
  }
  /** The page that teaches a concept, when the demo has it (p3 / p4 are not pages here: 「还没有讲它的页」). */
  private conceptPage(id: string): string | null {
    const p = DEMO_K.concepts[id]?.page ?? null;
    return p === "p1" || p === "p2" ? p : null;
  }
  private sectionOut(s: SecRec): SectionOut {
    const check = s.check === null ? null : { stem: DEMO_CHECK.stem, options: [...DEMO_CHECK.options], answered: s.check.answered,
      correct_index: s.check.answered === null ? null : DEMO_CHECK.correct, correct: s.check.correct, artifact_id: null };
    return { id: s.id, heading: s.heading, role: s.role, cue: s.cue, body: s.body, note_body: null, folded: s.folded, fold_locked: false,
      foldable: s.role === "recap" || s.role === "explain" || s.role === "compare", pinned: s.pinned, changed: s.changed, why: "",
      cells: s.cells.map((c) => this.cellOut(c)), lookups: [], artifact_id: null, fingerprint: "", check };
  }
  private pageOut(p: PageRec): PageOut {
    const meta = DEMO_PAGES[p.id];
    return { page_id: p.id, next_step: this.demoStep(p.id), title: meta.title, folder: "", parent_name: "", trashed: false, goal: meta.goal, organized: true, source: "organizer", profile: "", assumed: [],
      sections: p.sections.map((s) => this.sectionOut(s)), diff: null, unresolved_conflicts: [], conflicts: [], budget_exceeded: false,
      collapsed: false, can_shelve: false, written_for: "", goal_notes: [], shelf_line: "", done: p.done, llm_calls: 0,
      writer: { model: "demo", at: localNoon(10, 10), cost_usd: 0, fallback_reason: null },
      feedback: p.feedback, hand_edit: null, rewriting: false, sources: [] };
  }
  private card(id: DemoPageId): PageCardOut {
    return { page_id: id, title: DEMO_PAGES[id].title, goal: DEMO_PAGES[id].goal, stale: false, done: this.pages[id].done, organized: true };
  }
  private names(): Record<string, string> {
    const out: Record<string, string> = { p1: DEMO_PAGES.p1.title, p2: DEMO_PAGES.p2.title };
    for (const [id, c] of Object.entries(DEMO_K.concepts)) out[id] = c.name;
    return out;
  }
  private catalogOut(): CatalogOut {
    return {
      learning: (["p1", "p2"] as const).filter((id) => !this.pages[id].done).map((id) => this.card(id)),
      // no demo page is a reference card (shelved)
      shelf: { pages: [] },
      settings: this.settings, current_page: this.current,
      names: this.names(), paths: [this.pathCard()], maps: [],
    };
  }
  private pathOut(pathId: string): PathOut {
    if (pathId !== PATH_ID) throw http(404, "找不到这条学习路径");
    const pages = PATH_PAGES.map((pg, i) => ({
      index: i + 1, aim: pg.aim,
      concepts: pg.targets.map((c) => ({ concept_id: c, name: DEMO_K.concepts[c]!.name, target: true })),
      status: pg.page === null ? "not_started" as const : this.pages[pg.page].done ? "done" as const : "learning" as const,
      state: pg.page === null ? "planned" as const : this.pages[pg.page].done ? "read" as const : "written" as const, progress: null,
      page_id: pg.page, trashed: false, skipped: false, key: "", title: "", abstract: "", sections: [], exam: 0,
    }));
    return { path_id: PATH_ID, goal: "pca", source: "llm", pages, next_index: pages.find((pg) => pg.status === "not_started")?.index ?? null,
      prefetch: null, sources: [] };
  }
  private pathCard(): CatalogOut["paths"][number] {
    const out = this.pathOut(PATH_ID);
    return { path_id: PATH_ID, goal: out.goal, pages: out.pages.length, done: out.pages.filter((pg) => pg.status === "done").length, next_index: out.next_index };
  }
  private todayItems(includeGraded = false): ReviewItemOut[] {
    return DEMO_CARDS.filter((c) => includeGraded || !this.graded.has(c.cell)).map((c): ReviewItemOut => {
      const { conceptId } = splitCell(c.cell);
      return { card_id: `rc_${c.pageId}_${c.sid}_${c.mode}`, cell: c.cell, cells: [c.cell], concept_id: conceptId, concept_name: DEMO_K.concepts[conceptId]!.name,
        aspect: "term", band: this.band(c.cell), mode: c.mode, page_id: c.pageId, page_title: DEMO_PAGES[c.pageId].title, section_id: c.sid,
        heading: c.heading, cue: c.cue, answer: c.answer ?? "", check: c.mode === "check" ? { stem: c.stem, options: [...c.options] } : null,
        recall_now: 0.5, days_to_threshold: -0.5, in_goal: true, exam: false, artifact_id: null };
    }).filter((i) => !this.removedCards.has(i.card_id));
  }
  private pageRoundAnswers = 0;
  private todayOut(): ReviewTodayOut {
    const items = this.todayItems();
    const now = this.now().getTime();
    const confirms: ConfirmCardOut[] = this.confirms.filter((f) => f.outcome === null && f.due.getTime() <= now).map((f) => ({
      followup_id: f.id, kind: f.kind, cell: DEMO_CONFIRM.cell, concept_id: "eig", concept_name: DEMO_K.concepts.eig!.name, aspect: "term",
      page_id: DEMO_CONFIRM.pageId, page_title: DEMO_PAGES[DEMO_CONFIRM.pageId].title,
      check: { stem: DEMO_CONFIRM.stem, options: [...DEMO_CONFIRM.options] }, due: f.due.toISOString(), artifact_id: null }));
    // doneCard 的「6 天 · 连续学习」.
    return { items, due: items.length, done_today: this.graded.size, limit: this.settings.review_per_day, confirms, streak_days: 6, page_round_today: this.pageRoundAnswers };
  }
  private answer(pageId: DemoPageId, sid: string, cell: string, stem: string, options: string[], correct: number, choice: number, heading: string): string {
    const ref = `ans-demo-${++this.seq}`;
    this.answers.push({ ref, pageId, sid, cell, stem, options: [...options], correct, choice, heading, followed: false });
    return ref;
  }
  private specOut(): TeachSpecOut {
    return { version: this.spec.version, stored: true, items: this.spec.items, exemplars: this.spec.exemplars,
      params: { recap_max_per_page: 4, analogy_level: 1, fading_level: 1, interactive_bias: 1 },
      evidence: Object.fromEntries(Object.entries(DEMO_EVIDENCE).filter(([k]) => this.spec.items.some((i) => i.item_id === k))),
      decisions: [...this.decisions], max_items: MAX_SPEC_ITEMS, max_exemplars: MAX_EXEMPLARS };
  }
}

// ---------------------------------------------------------------- the assistant fake

/** freshAsst()'s stored session (one question, one answer citing pca（2/4）). */
export const DEMO_SESSION_ID = "ses_demo_p2";
export const DEMO_SESSION: SessionOut = {
  gone_sources: [], session_id: DEMO_SESSION_ID, page_id: "p2", recap: null,
  messages: [
    { message_id: "m-demo-1", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "特征值为什么叫「特征」？", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null },
    { message_id: "m-demo-2", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, asks: [],
      text: "「特征」在这里是「本身固有的」意思。一个矩阵代表一种变换，它的特征向量是这个变换里方向不变的那些方向，特征值是这些方向被拉长的倍数。〔讲义 p.12｜12｜demo：「方向不变、只是变长或变短」〕\n\n这两样东西只取决于矩阵自己，换个坐标系看也不变，所以说它们刻画了这个矩阵的「特征」。〔讲义 p.40｜未核对：「特征值是矩阵固有的量」〕",
      sources: [{ title: "pca（2/4）", page_id: "p2", url: null, segment_id: null }],
      tools: [{ call_id: "d1", name: "search_wiki", effect: "read", summary: "特征值 特征", ok: true, sources: [],
        label: "在 Wiki 里搜了「特征值 特征」 · 没找到", group: "search_wiki", group_label: "在 Wiki 里搜了 {n} 次", items: [], refs: [] },
      { call_id: "d2", name: "read_page", effect: "read", summary: "pca（2/4）", ok: true, sources: [{ title: "pca（2/4）", page_id: "p2", url: null, segment_id: null }],
        label: "读了《pca（2/4）》", group: "read_page", group_label: "读了 {k} 页", items: ["pca（2/4）"], refs: [{ title: "pca（2/4）", page_id: "p2", url: null, segment_id: null }] }], proposals: [], actions: [], selection: null, attachments: [], rating: null },
  ], decisions: [], proposals: [], actions: [],
};
/** assistantView()'s send(): the tools, the two paragraphs, the source and the 加进去 proposal. */
export const DEMO_ANSWER_EVENTS: AssistantEvent[] = [
  { type: "tool_start", call_id: "d1", name: "search_wiki", effect: "read", summary: "", label: "正在你的 Wiki 里查找" },
  { type: "tool_end", call_id: "d1", ok: true, sources: [{ title: "pca（2/4）", page_id: "p2", url: null, segment_id: null }], label: "在 Wiki 里搜了 · 1 页", group: "search_wiki",
    group_label: "在 Wiki 里搜了 {n} 次", items: ["pca（2/4）"], refs: [] },
  { type: "tool_start", call_id: "d2", name: "read_page", effect: "read", summary: "pca（2/4）", label: "正在读《pca（2/4）》" },
  { type: "tool_end", call_id: "d2", ok: true, sources: [{ title: "pca（2/4）", page_id: "p2", url: null, segment_id: null }], label: "读了《pca（2/4）》", group: "read_page",
    group_label: "读了 {k} 页", items: ["pca（2/4）"], refs: [{ title: "pca（2/4）", page_id: "p2", url: null, segment_id: null }] },
  { type: "text_delta", text: DEMO_MATRIX_SETUP, at: null },
  { type: "text_delta", text: `\n\n${DEMO_MATRIX_RESULT}\n\n${DEMO_MATRIX_ROWS}`, at: null },
  { type: "proposal", proposal_id: "pr-demo-1", draft: { kind: "add_section", cue: "", answer: "", page_id: "p2", section_id: null, heading: "为什么 A v 是 [0, 3]",
    text: DEMO_MATRIX_SECTION,
    why: "加成新的一节「为什么 A v 是 [0, 3]」。", cells: [] },
    change: { old_heading: null, old_body: "", new_body: DEMO_MATRIX_SECTION } },
  { type: "done", message_id: "m-demo-3", tool_calls: 2, llm_calls: 2, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false },
];

/** Scripted replies that never run out: once the queued ones are used, every question gets `next()` (the demo's answer). */
class DemoReplies extends Array<FakeStep> {
  next: () => FakeStep = () => ({ events: DEMO_ANSWER_EVENTS });
  override shift(): FakeStep { return super.shift() ?? this.next(); }
}

/** The assistant on freshAsst(): the stored session, then the demo's scripted answer to every question. */
export class DemoAssistantApi extends FakeAssistantApi {
  /** `stepMs`: how long each status line stays up before the next event (asst.js shows each step about 0.7 s); 0 = at once. */
  constructor(private readonly wiki: DemoWikiApi, private readonly stepMs: number = 0) {
    super();
    this.sessionOuts.set(DEMO_SESSION_ID, DEMO_SESSION);
    const replies = new DemoReplies();
    // asst.js replyFor: the question just sent (the fake records it before it takes a reply) picks the flow
    replies.next = () => {
      const last = this.calls.sendMessage[this.calls.sendMessage.length - 1];
      return this.reply(last?.body ?? { text: "" }, last?.sessionId ?? "");
    };
    this.messageReplies = replies;
  }
  private flows = 0;
  /** The scripted reply, played at the demo's pace: a running row or a status line stays up `stepMs` before the next event. */
  override async sendMessage(sessionId: string, body: MessageIn, onEvent: (e: AssistantEvent) => void, signal?: AbortSignal,
    onId?: (id: string) => void): Promise<void> {
    if (this.stepMs <= 0) return super.sendMessage(sessionId, body, onEvent, signal, onId);
    const got: AssistantEvent[] = [];
    let failed: { error: unknown } | null = null;
    try { await super.sendMessage(sessionId, body, (e) => got.push(e), signal, onId); } catch (error) { failed = { error }; }
    for (const e of got) {
      if (signal?.aborted) break;
      onEvent(e);
      // asst.js: a row runs 0.6 s, the next step comes 0.1 s after it is done; a status line stays as long
      const ms = e.type === "status" || e.type === "tool_start" ? this.stepMs : e.type === "tool_end" ? this.stepMs / 6 : 0;
      if (ms > 0) await new Promise((r) => setTimeout(r, ms));
    }
    if (failed !== null) throw failed.error;
  }
  /** 学点新的's conversations (app.js curConv().blocks), by session id. */
  private readonly blocks = new Map<string, DemoBlocksConv>();
  /** The executed blocks rows: the goal they opened (撤销 closes it). */
  private readonly opened = new Map<string, string>();
  /**
   * asst.js replyFor: a 学点新的 conversation talks its blocks over (app.js blocksReply); 开始's first message gets the draft
   * (startBlocks); a message that starts one of the demo's five flows gets its tools, paragraph and card. The ledger row
   * also waits on its page.
   */
  private reply(body: MessageIn, sessionId: string): FakeStep {
    const text = body.text ?? "";
    const conv = this.blocks.get(sessionId);
    if (conv !== undefined) {
      const { events, replaced } = demoBlocksReply(conv, text, ++this.flows, new Date());
      if (replaced !== null) { this.actionRows.set(replaced.action_id, replaced); this.wiki.putBotAction(replaced); }
      if (conv.act.status === "proposed") { this.actionRows.set(conv.act.action_id, conv.act); this.wiki.putBotAction(conv.act); }
      return { events };
    }
    const start = demoBlocksStart(body, TOPICS, ++this.flows, new Date());
    if (start !== null) {
      this.blocks.set(sessionId, start.conv);
      this.actionRows.set(start.conv.act.action_id, start.conv.act); this.wiki.putBotAction(start.conv.act);
      return { events: start.events };
    }
    const f = demoFlowOf(text);
    if (f === null) return { events: DEMO_ANSWER_EVENTS };
    const { events, action } = demoFlowEvents(f, ++this.flows, new Date());
    if (action !== null) { this.actionRows.set(action.action_id, action); this.wiki.putBotAction(action); }
    return { events };
  }
  override approveAction(actionId: string, draft?: ActionDraft): Promise<ActionOut> {
    const a = this.actionRows.get(actionId);
    const conv = [...this.blocks.values()].find((c) => c.act.action_id === actionId);
    if (a !== undefined && a.status === "proposed" && conv !== undefined) {
      // 确认 of a blocks row: the goal opens with the confirmed blocks (goal-fake.ts answers its round 1)
      const blocks = demoConfirmedBlocks(conv, draft);
      const ids = registerBlockGoal(conv.goal, blocks.map((b) => ({ id: b.key, name: b.name, scope: b.covers, level: b.level, from: b.from })));
      const out: ActionOut = { ...structuredClone(a), status: "executed", can_undo: true, decided_at: new Date().toISOString(),
        content: { ...a.content, pages: blocks.map((b) => ({ key: b.key, title: b.name, covers: b.covers, level: b.level })) },
        result: { draft_id: ids.draftId, map_id: ids.mapId, open: true } };
      conv.act = out;
      this.opened.set(actionId, ids.draftId);
      this.actionReplies.unshift(out);
      this.wiki.putBotAction(out);
      return super.approveAction(actionId, draft);
    }
    if (a !== undefined && a.status === "proposed" && a.source === "assistant") {
      const out = demoApprove(a, draft, new Date());
      this.actionReplies.unshift(out);
      // asst.js ruleFlow `pg: 4`: the written pages the rule left out of date (change_source: stale_counts)
      this.wiki.putBotAction(out, out.kind === "rule" ? { pages_waiting: 4 } : {});
    }
    return super.approveAction(actionId, draft);
  }
  override async rejectAction(actionId: string): Promise<ActionOut> {
    const out = await super.rejectAction(actionId);
    // 取消 of a blocks draft: that conversation is over (a new 开始 starts another)
    for (const [sid, c] of this.blocks) if (c.act.action_id === actionId) this.blocks.delete(sid);
    this.wiki.putBotAction(out);
    return out;
  }
  override async undoAction(actionId: string): Promise<ActionOut> {
    const draftId = this.opened.get(actionId);
    if (draftId !== undefined) { dropBlockGoal(draftId); this.opened.delete(actionId); }
    const out = await super.undoAction(actionId);
    const conv = [...this.blocks.values()].find((c) => c.act.action_id === actionId);
    if (conv !== undefined) conv.act = out;
    this.wiki.putBotAction(out);
    return out;
  }
  /** GET /wiki/writes: the wiki fixture's runs (one source for the cards, the tree and 个性化). */
  override writes(pageIds: string[]): Promise<WriteOut[]> { return this.wiki.writes(pageIds); }
  override decide(proposalId: string, decision: Decision): Promise<ProposalDecisionOut> {
    if (decision === "accept" && this.decisionReplies.length === 0) {
      this.decisionReplies.push({ proposal_id: proposalId, status: "accepted", applied: this.wiki.addAssistantSection(), detail: "", action_id: "", can_undo: false });
    }
    return super.decide(proposalId, decision);
  }
}

// ---------------------------------------------------------------- the switch

/** Everything the fixture replaces, built once per plugin load (main.ts). */
export interface DemoFixture {
  wiki: DemoWikiApi; assistant: DemoAssistantApi; usage: DemoUsageClient; recommendations: readonly Recommendation[];
  /** freshAsst() is shown for p2: the stored session id for page p2 under this identity, unless one is stored already. */
  seedSessions(sessions: Record<string, string>, identity: { server: string; user: string }): void;
  /** app.js boot (`SEEN.eng`): changes from before today were already looked at; 个性化 last opened at the newest of them, unless one is stored. */
  seedSeen(keys: string[]): void;
}
export function createDemoFixture(o: DemoWikiOptions = {}): DemoFixture {
  const wiki = new DemoWikiApi(o);
  return {
    wiki, assistant: new DemoAssistantApi(wiki, (o.delayMs ?? 0) > 0 ? 700 : 0), usage: new DemoUsageClient(), recommendations: DEMO_RECOMMENDATIONS,
    seedSessions: (sessions, identity) => {
      const key = sessionKey(identity, { kind: "page", pageId: "p2", title: DEMO_PAGES.p2.title });
      if (!Object.prototype.hasOwnProperty.call(sessions, key)) {
        Object.defineProperty(sessions, key, { value: DEMO_SESSION_ID, writable: true, enumerable: true, configurable: true });
      }
    },
    seedSeen: (keys) => {
      if (keys.some((k) => k.startsWith("engine@"))) return;
      // every change the demo starts with was looked at (the ledger's too): 个性化 counts what waits for the learner
      const now = (o.now ?? (() => new Date()))();
      keys.push(`engine@${Math.max(...[...demoRsiLog(now), ...demoBotData(now).rsi].map((c) => Date.parse(c.at)))}`);
    },
  };
}
