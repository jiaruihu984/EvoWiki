/**
 * The 学点新的 fake for the screenshot fixture only (plugin data `fixture: "demo"`): big-goal-demo.html's scripted topics
 * (TOPICS: 机器学习 and 黑壳虾 — regions / children / round1 / quiz / deep, and the demo's submitRound / nextRound / finish /
 * enter decisions) behind the real big-goal contract (schema.ts GoalDraftOut / GoalMapOut / PathOut / MapEditOut /
 * PathEditOut). The map carries no positions (the view lays regions out itself, as with the real server); a path page's one
 * concept is the demo's page heading.
 *
 * The way in is the chat-first blocks (the demo's #blocks, user ruling 2026-10-23): the fixture's assistant proposes
 * `fakeBlocks(goal)` (the topic's regions as blocks), the learner talks them over, and 确认 registers the confirmed blocks
 * (`registerBlockGoal`, the demo's useBlocks: a merged block carries its parts' pages and points, an added block one page
 * of its own, the questions follow their block); GET /wiki/goals/{draft_id} then answers round 1 for them.
 *
 * Every region carries all its concept nodes from the map's creation (grow.html: the knowledge points grow in at once,
 * grey); each round's evidence colours them (a region's mix = its points' bands); 进入 marks the region expanded and, after
 * FAKE_SPLIT_MS (the demo's 正在把「X」拆成页…), answers its path. The 学习路径 card's edits (reorder / drop / rename / add,
 * and the inverse ops 撤销 sends back: insert …) and a block's page list (move / remove / unremove / add, 拆成多页 = 「X是什么」「X怎么做」 and its undo) answer
 * at once, as the demo does them.
 *
 * The final map contract: 线性代数 is an existing region of the demo K, so it carries that region's id ("la") and
 * `reused: true`; the goal's new regions keep the fake's short ids.
 * `members` = the region's knowledge-point ids, each also a concept node (parent = the region) that follows its region in
 * `nodes`; a concept is always "expanded", `reused` when the demo K already has it. State: collapsed until entered,
 * expanded once entered (path_id set), known when mastered.
 */
import { ApiError } from "../../api/errors";
import {
  type AnswerResultOut, type Band, type GoalDraftOut, type GoalMapOut, type MapEditOpIn, type MapEditOut,
  type MapNodeOut, type OptionMeans, type PathEditIn, type PathEditOut, type PathItemOut, type PathNextOut, type PathOut, type QuestionOut,
  SELF_REPORT_KIND, type SplitOut,
} from "../schema";
import type { GoalAnswer, GoalApi } from "./goal-api";

type Self = OptionMeans;
/** `level`: the block's level word (基础 / 核心 / 进阶 / 拓展; absent: none, as 机器学习's regions). */
interface Region { id: string; name: string; scope: string; prereqs: string[]; target?: boolean; level?: string }
/** big-goal-demo.html CHILDREN: pages are [title, aim, 真题 count (PathPageOut.exam; absent = 0)]. */
interface Children { pages: [string, string, number?][]; concepts: [string, string, string[]][] }
interface SelfQ { id: string; target: string; stem: string; options: [string, Self][] }
/** The demo's QUIZ without its trailing 「不知道」 (the client adds it); the correct option is always the first. */
interface Quiz { id: string; target: string; cell: string; stem: string; options: string[]; only?: Self }
interface Deep { after: string; region: string; partial: string; skipPages: number; quiz: Quiz }
export interface Topic { bg?: string; regions: Region[]; children: Record<string, Children>; round1: SelfQ[]; quiz: Record<string, Quiz>; deep: Deep | null }

const SELF_OPTS = (a: string, b: string): [string, Self][] => [[a, "know"], [b, "partly"], ["没学过", "dont"], ["说不清", "unsure"]];

/** big-goal-demo.html TOPICS (the two the plugin's screenshots use). */
export const TOPICS: Record<string, Topic> = {
  "机器学习": {
    regions: [
      { id: "la", name: "线性代数", scope: "矩阵、矩阵乘法、向量和点积", prereqs: [] },
      { id: "pr", name: "概率论", scope: "条件概率、贝叶斯公式、期望和方差、正态分布、最大似然", prereqs: [] },
      { id: "ca", name: "微积分", scope: "导数、偏导数和梯度、链式法则", prereqs: [] },
      { id: "op", name: "优化", scope: "梯度下降、学习率", prereqs: ["ca"] },
      { id: "sl", name: "监督学习", scope: "线性回归、损失函数、逻辑回归、正则化、决策树", prereqs: ["la", "pr", "op"], target: true },
      { id: "ev", name: "模型评估", scope: "过拟合、交叉验证、准确率和召回率", prereqs: ["sl"] },
    ],
    children: {
      la: { pages: [["矩阵和矩阵乘法", "能把一批样本写成矩阵，算出预测 Xw"]], concepts: [["mat", "矩阵是什么", []], ["mul", "矩阵乘法", ["mat"]], ["vec", "向量和点积", []]] },
      pr: { pages: [["条件概率和贝叶斯", "能用贝叶斯公式更新判断", 4], ["期望、方差和正态分布", "能说出一批数据的中心和散开程度"], ["最大似然", "知道“训练就是让数据最可能出现”"]],
            concepts: [["cond", "条件概率", []], ["bayes", "贝叶斯公式", ["cond"]], ["exp", "期望", []], ["var", "方差", ["exp"]], ["norm", "正态分布", ["var"]], ["mle", "最大似然", ["norm", "cond"]]] },
      ca: { pages: [["导数、偏导数和梯度", "能说出梯度指向哪里、为什么沿反方向走"]], concepts: [["der", "导数", []], ["grad", "偏导数和梯度", ["der"]], ["chain", "链式法则", ["der"]]] },
      op: { pages: [["梯度下降", "能手算两步梯度下降，知道学习率太大太小会怎样"]], concepts: [["gd", "梯度下降", []], ["lr", "学习率", ["gd"]]] },
      sl: { pages: [["从数据到模型", "知道训练集、特征、标签各是什么"], ["线性回归和损失函数", "能写出均方误差并解释它在量什么"], ["逻辑回归和分类", "能解释为什么分类要用交叉熵"], ["正则化和树模型", "知道模型太复杂时怎么拉回来"]],
            concepts: [["data", "训练集、特征和标签", []], ["linreg", "线性回归", ["data"]], ["loss", "损失函数", ["linreg"]], ["logreg", "逻辑回归", ["loss"]], ["xent", "交叉熵", ["logreg"]], ["reg", "正则化", ["loss"]], ["tree", "决策树", ["data"]], ["ens", "随机森林", ["tree"]]] },
      ev: { pages: [["怎么知道模型好不好", "能判断过拟合，并用交叉验证选模型"]], concepts: [["of", "过拟合", []], ["cv", "交叉验证", ["of"]], ["pr2", "准确率和召回率", []]] },
    },
    round1: [
      { id: "s_la", target: "la", stem: "学过线性代数吗？", options: SELF_OPTS("学过，矩阵乘法还会算", "学过，但忘得差不多了") },
      { id: "s_pr", target: "pr", stem: "学过概率论吗？", options: SELF_OPTS("学过，还记得", "学过一点，记不太清") },
      { id: "s_ca", target: "ca", stem: "求导、梯度这些还熟吗？", options: [["熟", "know"], ["会求导，梯度没印象", "partly"], ["不会", "dont"], ["说不清", "unsure"]] },
      { id: "s_sl", target: "sl", stem: "以前碰过机器学习吗？", options: [["用 sklearn 跑过模型", "partly"], ["只听说过", "dont"], ["完全没有", "dont"]] },
    ],
    quiz: {
      s_la: { id: "q_la", target: "la", cell: "mul", stem: "A 是 2×3 的矩阵，B 是 3×4 的矩阵，AB 是几行几列？", options: ["2×4", "3×3", "2×3", "不能相乘"] },
      s_pr: { id: "q_pr", target: "pr", cell: "cond", stem: "抛两次硬币，已知至少有一次正面，两次都是正面的概率是？", options: ["1/3", "1/2", "1/4"] },
      s_ca: { id: "q_ca", target: "ca", cell: "der", stem: "f(x) = x² 在 x = 3 处的导数是？", options: ["6", "9", "3"] },
      s_sl: { id: "q_sl", target: "ev", cell: "of", only: "partly", stem: "模型在训练集上准确率 99%，在测试集上只有 60%，最可能是？", options: ["过拟合", "欠拟合", "数据太多"] },
    },
    deep: { after: "s_pr", region: "pr", partial: "mle", skipPages: 2,
      quiz: { id: "q_pr2", target: "pr", cell: "var", stem: "X 的方差是 1，2X 的方差是？", options: ["4", "2", "1"] } },
  },
  // 黑壳虾 (user report 2026-10-23, the ChatGPT outline style 1391ae5b): short block names without the topic word (「X与Y」),
  // one level word each, one topic in one block (all water quality in 水质与环境)
  "黑壳虾": {
    bg: "学过高中生物，想知道黑壳虾是什么、怎么养。",
    regions: [
      { id: "id", name: "分类与鉴别", level: "基础", scope: "俗称指哪种虾、和樱花虾水晶虾怎么分、身体结构、分公母", prereqs: [] },
      { id: "wq", name: "水质与环境", level: "基础", scope: "水温、pH 和 GH / KH、氨氮和亚硝酸盐、硝化、溶氧", prereqs: [] },
      { id: "keep", name: "开缸与日常", level: "核心", scope: "缸和密度、过滤和防吸虾、开缸、过水、第一次换水", prereqs: ["wq"], target: true },
      { id: "food", name: "营养与摄食", level: "核心", scope: "生物膜和藻、虾粮、补钙、喂多了为什么坏水", prereqs: [], target: true },
      { id: "br", name: "生长与繁殖", level: "进阶", scope: "蜕皮、抱卵和孵化、小虾怎么养", prereqs: ["keep"], target: true },
      { id: "sick", name: "疾病与风险", level: "进阶", scope: "应激信号、蜕皮失败、铜中毒、突然死虾怎么查", prereqs: ["keep"], target: true },
    ],
    children: {
      id: { pages: [["黑壳虾是什么虾", "能认出黑壳虾，说出它和樱花虾、水晶虾的区别"], ["身体结构和分公母", "能看出哪只是母虾"]], concepts: [["what", "黑壳虾是什么", []], ["kind", "品种和鉴别", ["what"]], ["body", "身体结构", []], ["sex", "分辨公母", ["body"]]] },
      wq: { pages: [["黑壳虾要什么样的水", "能说出水温、pH、GH / KH 的合适范围"], ["氨氮、亚硝酸盐和硝化", "知道新缸为什么容易死虾、怎么等硝化建立"], ["怎么测、怎么调", "会用试剂测水，能决定换多少水"]], concepts: [["temp", "水温", []], ["ph", "pH 和 GH / KH", []], ["nh3", "氨氮和亚硝酸盐", []], ["cycle", "硝化", ["nh3"]], ["test", "测水和调水", ["ph", "nh3"]]] },
      keep: { pages: [["缸、过滤和造景", "能为黑壳虾选缸、配过滤和躲藏处"], ["开缸和过水", "能开好一个缸，把新虾安全放进去"], ["换水和日常观察", "会每周换水，能看出虾的状态不对"]], concepts: [["setup", "开缸", []], ["acc", "过水", ["setup"]], ["change", "换水", ["setup"]], ["watch", "日常观察", []]] },
      food: { pages: [["虾吃什么", "知道缸里的生物膜和藻就是它的主食"], ["喂多少", "知道喂多了为什么会坏水"]], concepts: [["film", "生物膜和藻", []], ["feed", "喂食", []], ["ca", "补钙", []]] },
      br: { pages: [["蜕皮", "知道虾为什么要蜕壳、蜕壳失败是怎么回事"], ["抱卵和孵化", "能看出母虾抱卵，知道多久孵化"], ["小虾怎么养", "能让小虾活过第一个月"]], concepts: [["molt", "蜕皮", []], ["egg", "抱卵和孵化", ["molt"]], ["baby", "小虾的养护", ["egg"]]] },
      sick: { pages: [["虾不对劲的信号", "能看出虾在应激"], ["突然死虾怎么办", "能按顺序查出死虾的原因"]], concepts: [["stress", "应激信号", []], ["death", "死虾的原因", ["stress"]], ["treat", "处理和隔离", ["death"]]] },
    },
    round1: [
      { id: "s_id", target: "id", stem: "以前了解过黑壳虾吗？", options: SELF_OPTS("了解，能认出来", "听说过") },
      { id: "s_wq", target: "wq", stem: "养鱼时测过水吗？", options: [["测过，知道 pH 和氨氮", "know"], ["换过水，没测过", "partly"], ["没有", "dont"], ["说不清", "unsure"]] },
      { id: "s_keep", target: "keep", stem: "自己养过鱼或虾吗？", options: [["养过一段时间", "partly"], ["没养过", "dont"], ["说不清", "unsure"]] },
    ],
    quiz: {
      s_id: { id: "q_id", target: "id", cell: "what", stem: "「黑壳虾」这个名字是？", options: ["水族店的俗称，可能是几种虾", "一个物种的学名", "樱花虾的幼体"] },
      s_wq: { id: "q_wq", target: "wq", cell: "nh3", stem: "新开的缸第二天就放虾，最可能出什么问题？", options: ["氨氮升高，虾中毒", "水太干净", "pH 太高"] },
    },
    deep: null,
  },
};
/** The default topic (the demo's own: any goal it does not know is 机器学习). */
const DEFAULT_TOPIC = "机器学习";
/** big-goal-demo.html pickTopic: the first topic named in the text, else 机器学习. */
export const pickTopic = (text: string): string => Object.keys(TOPICS).find((k) => text.includes(k)) ?? DEFAULT_TOPIC;

/** 机器学习's tables (older tests and the screenshot fixture name them). */
export const REGIONS: Region[] = TOPICS[DEFAULT_TOPIC]!.regions;
export const CHILDREN: Record<string, Children> = TOPICS[DEFAULT_TOPIC]!.children;
/**
 * The ids the map's concepts carry. The demo K (fixtures-demo.ts) already holds 矩阵乘法 (mul), 向量 (vec) and 方差 (var):
 * the fixed backend reuses an existing concept's id instead of making a second node, so the fake does too (the cloud
 * merges map nodes by id only). Every other concept is `<region>.<concept>`; the regions' ids (la …) are the demo K's region ids.
 */
export const REUSED_IDS: Record<string, string> = { "la.mul": "mul", "la.vec": "vec", "pr.var": "var" };
export const conceptId = (region: string, cid: string): string => REUSED_IDS[`${region}.${cid}`] ?? `${region}.${cid}`;
/** The goal regions that are existing regions of the demo K (fixtures-demo.ts DEMO_K). */
export const REUSED_REGIONS: ReadonlySet<string> = new Set(["la"]);
const REUSED_CONCEPTS: ReadonlySet<string> = new Set(Object.values(REUSED_IDS));

const SELF: Record<Self, Band> = { know: "likely", partly: "shaky", forgot: "shaky", dont: "gap", unsure: "none" };

/** round_title = the demo's headers; round_why is sent (the contract has it) but the view never shows it. */
const ROUND1 = { title: "了解你的基础", why: "先了解一下你的学习经历" };
const ROUND2 = { title: "聚焦关键知识", why: "进一步了解你的知识掌握情况" };
const ROUND3 = { title: "再确认一点", why: "再确认一个关键知识点" };

export const FAKE_ROUND_MS = 900;
/** big-goal-demo.html enter(): 正在把「X」拆成页… for 900 ms. */
export const FAKE_SPLIT_MS = 900;

// ---- the chat-first blocks (app.js startBlocks / blocksAct, big-goal-demo.html useBlocks) ----

/** A block of the draft (the demo's S.draft item): `from` = the topic regions it carries (none for one the learner added). */
export interface FakeBlock { id: string; name: string; scope: string; level?: string; from: string[]; target?: boolean }

/** The assistant's first draft for a goal: the goal topic's regions, one block each. */
export function fakeBlocks(goal: string): FakeBlock[] {
  return TOPICS[pickTopic(goal)]!.regions.map((r) => ({ id: r.id, name: r.name, scope: r.scope, level: r.level ?? "", from: [r.id], target: r.target === true }));
}

/** big-goal-demo.html useBlocks: the confirmed blocks as the goal's regions (a merged block carries its parts). */
export function topicFromBlocks(goal: string, blocks: readonly FakeBlock[]): Topic {
  const T0 = TOPICS[pickTopic(goal)]!, to: Record<string, string> = {};
  blocks.forEach((b) => b.from.forEach((f) => (to[f] = b.id)));
  const regions: Region[] = blocks.map((b) => {
    const base = T0.regions.filter((r) => b.from.includes(r.id));
    return { id: b.id, name: b.name, scope: b.scope, level: b.level ?? "", target: base.some((r) => r.target === true),
      prereqs: [...new Set(base.flatMap((r) => r.prereqs).map((p) => to[p]).filter((p): p is string => p !== undefined && p !== b.id))] };
  });
  const children: Record<string, Children> = Object.fromEntries(blocks.map((b) => {
    const parts = b.from.map((f) => T0.children[f]).filter((p): p is Children => p !== undefined);
    return [b.id, parts.length ? { pages: parts.flatMap((p) => p.pages), concepts: parts.flatMap((p) => p.concepts) }
      : { pages: [[b.name, `能说清「${b.name}」是怎么回事`]], concepts: [["c", b.name, []]] }];
  }));
  const remap = <Q extends { target: string }>(q: Q): Q | null => (to[q.target] !== undefined ? { ...q, target: to[q.target]! } : null);
  const round1 = T0.round1.map(remap).filter((q): q is SelfQ => q !== null).filter((q, i, a) => a.findIndex((x) => x.target === q.target) === i);
  const quiz = Object.fromEntries(Object.entries(T0.quiz).map(([k, q]) => [k, remap(q)] as const).filter((e): e is [string, Quiz] => e[1] !== null));
  const deep = T0.deep !== null && to[T0.deep.region] === T0.deep.region ? T0.deep : null;
  return { ...(T0.bg !== undefined ? { bg: T0.bg } : {}), regions, children, round1, quiz, deep };
}

/** The goals confirmed in the fixture's chat (approve of the assistant's goal_new row), by draft id. */
const BLOCK_GOALS = new Map<string, { goal: string; topic: Topic; mapId: string }>();
let blockSeq = 0;
/** The fixture's 确认 of a blocks row: the goal is opened (round 1 waits); its draft id answers GET /wiki/goals/{id}. */
export function registerBlockGoal(goal: string, blocks: readonly FakeBlock[]): { draftId: string; mapId: string } {
  const n = ++blockSeq;
  const ids = { draftId: `fake-draft-b${n}`, mapId: `fake-map-b${n}` };
  BLOCK_GOALS.set(ids.draftId, { goal, topic: topicFromBlocks(goal, blocks), mapId: ids.mapId });
  return ids;
}
/** The fixture's 撤销 of an executed blocks row: the goal is closed (its draft answers 404 from now on). */
export function dropBlockGoal(draftId: string): void { BLOCK_GOALS.delete(draftId); }
/** Whether this draft / map id is the fake's (FixtureGoalApi routes it there). */
export const isFakeId = (id: string): boolean => id.startsWith("fake-");

const BANDS: Band[] = ["known", "likely", "shaky", "gap", "none"];
const zero = (): Record<Band, number> => ({ known: 0, likely: 0, shaky: 0, gap: 0, none: 0 });
const SCORE: Record<Exclude<Band, "none">, number> = { known: 1, likely: 0.78, shaky: 0.45, gap: 0.08 };

/** counts → shares (mix) and the average score of the evidenced share (mastery; null without evidence). */
function summarize(bands: Band[]): { mix: Partial<Record<Band, number>>; counts: Partial<Record<Band, number>>; mastery: number | null } {
  const counts = zero();
  for (const b of bands) counts[b]++;
  const mix = zero();
  for (const b of BANDS) mix[b] = bands.length ? counts[b] / bands.length : 0;
  const ev = bands.filter((b): b is Exclude<Band, "none"> => b !== "none");
  return { mix, counts, mastery: ev.length ? ev.reduce((s, b) => s + SCORE[b], 0) / ev.length : null };
}

interface Round { kind: "self" | "quiz"; ids: string[] }
/** A page row of a planned block (the fake's path): `split` = a part of 拆成多页 (not split again). */
interface FakeRow { key: string; title: string; aim: string; exam: number; split: boolean; removed: boolean; parts: string[] | null }

const clone = <T>(x: T): T => structuredClone(x);

export class FakeGoalApi implements GoalApi {
  readonly live = false;
  private goal = "";
  private T: Topic = clone(TOPICS[DEFAULT_TOPIC]!);
  private band: Record<string, Band> = {};
  private self: Record<string, Self> = {};
  private answers: Record<string, boolean> = {};
  private rounds: Round[] = [];
  private finished = false;
  private expanded = new Set<string>();
  private planned = new Set<string>();
  private paths = new Map<string, FakeRow[]>();
  private ids = { draft: "", map: "" };
  private addSeq = 0;
  private dropped = new Map<string, Region>();

  constructor(private readonly o: { roundMs?: number; splitMs?: number } = {}) {}

  private wait(ms: number): Promise<void> {
    return ms > 0 ? new Promise((r) => window.setTimeout(r, ms)) : Promise.resolve();
  }

  private reset(goal: string, topic: Topic, ids: { draft: string; map: string }): void {
    this.goal = goal; this.T = clone(topic); this.ids = ids;
    this.band = Object.fromEntries(this.T.regions.map((r) => [r.id, "none"]));
    this.self = {}; this.answers = {}; this.finished = false;
    this.expanded = new Set(); this.planned = new Set(); this.paths = new Map(); this.dropped = new Map();
    this.rounds = [{ kind: "self", ids: this.T.round1.map((q) => q.id) }];
  }

  /** Round 1 (or, with no self-report left after a merge / drop, the finished map at once: the demo's finish(true)). */
  private opening(): GoalDraftOut {
    if (this.T.round1.length === 0) { this.finish(); return this.finishedOut([]); }
    return this.draft(ROUND1, this.selfQuestions(), []);
  }

  /** GET /wiki/goals/{draft_id}: a goal confirmed in the chat (round 1), or this flow's own draft as it stands. */
  getDraft(draftId: string): Promise<GoalDraftOut> {
    const b = BLOCK_GOALS.get(draftId);
    if (b !== undefined && this.ids.draft !== draftId) {
      this.reset(b.goal, b.topic, { draft: draftId, map: b.mapId });
      return Promise.resolve(this.opening());
    }
    if (draftId !== this.ids.draft || (b === undefined && this.ids.draft === "")) return Promise.reject(new ApiError(404, "no such draft"));
    return Promise.resolve(this.finished ? this.finishedOut([]) : this.opening());
  }

  private selfQuestions(): QuestionOut[] {
    return this.T.round1.map((q, index) => {
      const r = this.T.regions.find((x) => x.id === q.target)!;
      return { index, kind: SELF_REPORT_KIND, target: q.target, cell: "", concept_id: q.target, concept_name: r.name, aspect: null,
        stem: q.stem, options: q.options.map((o) => o[0]), option_means: q.options.map((o) => o[1]), intent: SELF_REPORT_KIND };
    });
  }
  private quizQuestions(quizzes: Quiz[]): QuestionOut[] {
    return quizzes.map((q, index) => {
      const name = this.T.children[q.target]?.concepts.find(([cid]) => cid === q.cell)?.[1] ?? q.cell;
      return { index, kind: "quiz", target: q.target, cell: `${q.cell}:procedure`, concept_id: conceptId(q.target, q.cell), concept_name: name,
        aspect: "procedure", stem: q.stem, options: [...q.options], option_means: null, intent: "mastery" };
    });
  }

  private base(): Pick<GoalDraftOut, "title" | "concepts" | "notes" | "map"> {
    return { title: this.goal, concepts: [], notes: { from_background: 0, reused_concepts: 0, dropped_questions: 0 }, map: this.map() };
  }
  private draft(h: { title: string; why: string }, questions: QuestionOut[], results: AnswerResultOut[]): GoalDraftOut {
    return { ...this.base(), draft_id: this.ids.draft, page_id: null, path_id: null, map_id: null, round: this.rounds.length,
      round_title: h.title, round_why: h.why, questions, results, decisions: [] };
  }
  private finishedOut(results: AnswerResultOut[]): GoalDraftOut {
    return { ...this.base(), draft_id: null, page_id: null, path_id: null, map_id: this.ids.map, round: 0, round_title: "", round_why: "",
      questions: [], results, decisions: [] };
  }

  private allQuizzes(): Quiz[] { return [...Object.values(this.T.quiz), ...(this.T.deep ? [this.T.deep.quiz] : [])]; }

  async answer(draftId: string, body: GoalAnswer): Promise<GoalDraftOut> {
    if (draftId !== this.ids.draft || this.finished) throw new ApiError(404, "no such draft");
    if ("skip" in body) { this.finish(); return this.finishedOut([]); }
    const round = this.rounds[this.rounds.length - 1]!;
    if (body.answers.length !== round.ids.length) throw new ApiError(422, "answers do not match the questions");
    const results: AnswerResultOut[] = [];
    // big-goal-demo.html submitRound
    round.ids.forEach((id, index) => {
      const pick = body.answers[index]!;
      if (round.kind === "self") {
        const q = this.T.round1.find((x) => x.id === id)!;
        const m: Self = pick === -1 ? "unsure" : q.options[pick]![1];
        this.self[q.id] = m; this.band[q.target] = SELF[m];
        results.push({ index, kind: SELF_REPORT_KIND, choice: pick, answer: null, correct: null, means: m, intent: SELF_REPORT_KIND, node_id: q.target });
        return;
      }
      const q = this.allQuizzes().find((x) => x.id === id)!;
      const ok = pick === 0, idk = pick === -1;
      this.answers[q.id] = ok;
      results.push({ index, kind: "quiz", choice: pick, answer: 0, correct: ok, means: null, intent: "mastery", node_id: q.target });
      const said = Object.entries(this.T.quiz).find(([, x]) => x.id === q.id)?.[0];
      if (this.T.deep && q.id === this.T.deep.quiz.id) this.band[q.target] = ok ? "known" : "shaky";
      else this.band[q.target] = ok ? (said !== undefined && this.self[said] === "know" ? "known" : "likely") : idk ? "gap" : "shaky";
    });
    await this.wait(this.o.roundMs ?? FAKE_ROUND_MS);
    const next = this.nextRound();
    if (next === null) { this.finish(); return this.finishedOut(results); }
    this.rounds.push({ kind: "quiz", ids: next.quizzes.map((q) => q.id) });
    return this.draft(next.head, this.quizQuestions(next.quizzes), results);
  }

  /** big-goal-demo.html nextRound. */
  private nextRound(): { head: typeof ROUND2; quizzes: Quiz[] } | null {
    const n = this.rounds.length, T = this.T;
    if (n === 1) {
      const qs = T.round1.filter((q) => { const m = this.self[q.id], z = T.quiz[q.id]; return z && m && m !== "dont" && (!z.only || z.only === m); }).map((q) => T.quiz[q.id]!);
      if (!qs.length) return null;
      return { head: ROUND2, quizzes: qs };
    }
    const d = T.deep;
    if (n === 2 && d && this.self[d.after] === "know" && T.quiz[d.after] && this.answers[T.quiz[d.after]!.id]) return { head: ROUND3, quizzes: [d.quiz] };
    return null;
  }

  private finish(): void { this.finished = true; }

  /**
   * A knowledge point's band from the evidence so far (grey until a round says something): a quiz on it → known / shaky;
   * else the region's band; the deep region known → every point known but its partial one (the demo's 概率论 case).
   */
  private childBand(region: string, cid: string): Band {
    const d = this.T.deep;
    if (d && region === d.region && this.band[region] === "known") return cid === d.partial ? "none" : "known";
    const quiz = this.allQuizzes().find((q) => q.cell === cid && q.target === region);
    if (quiz && quiz.id in this.answers) return this.answers[quiz.id] ? "known" : "shaky";
    return this.band[region] ?? "none";
  }

  /** The demo's isKnown: a known region is skipped, except the deep one (its last page is still taught). */
  private isKnown(r: Region): boolean { return this.finished && this.band[r.id] === "known" && r.id !== this.T.deep?.region; }

  private map(): GoalMapOut {
    const nodes: MapNodeOut[] = [];
    for (const r of this.T.regions) {
      const kids = this.T.children[r.id]?.concepts ?? [];
      const state = this.isKnown(r) ? "known" : this.expanded.has(r.id) ? "expanded" : "collapsed";
      // A region's colour is the weighted mix of its points (the demo's regionBands). The demo predates splitting on demand
      // (spec 2026-10-28): entering one of its blocks plans its pages at once, as opening a point does (enter "learn").
      const s = summarize(kids.map(([cid]) => this.childBand(r.id, cid)));
      nodes.push({ id: r.id, kind: "region", name: r.name, parent: null, scope: r.scope, level: r.level ?? "", prereqs: [...r.prereqs], target: r.target === true,
        state, path_id: this.planned.has(r.id) ? `fake-path-${r.id}` : null, page_id: null,
        reused: REUSED_REGIONS.has(r.id), members: kids.map(([cid]) => conceptId(r.id, cid)), atomic: null, enter: "learn", ...s });
      // Every region carries all its knowledge points from the start (grow.html), grey until the rounds give evidence.
      for (const [cid, name, pre] of kids) {
        const id = conceptId(r.id, cid);
        nodes.push({ id, kind: "concept", name, parent: r.id, scope: "", level: "", prereqs: pre.map((p) => conceptId(r.id, p)), target: false, state: "expanded",
          path_id: null, page_id: null, reused: REUSED_CONCEPTS.has(id), members: [], atomic: null, enter: "learn", ...summarize([this.childBand(r.id, cid)]) });
      }
    }
    const next = this.finished ? this.T.regions.find((r) => !this.isKnown(r) && !this.planned.has(r.id))?.id ?? null : null;
    return { map_id: this.ids.map, goal: this.goal, title: pickTopic(this.goal), nodes, next_region: next, reading: null };
  }

  getMap(mapId: string): Promise<GoalMapOut> {
    if (mapId !== this.ids.map || !this.finished) return Promise.reject(new ApiError(404, "no such map"));
    return Promise.resolve(this.map());
  }

  async enterRegion(mapId: string, regionId: string): Promise<GoalDraftOut> {
    const r = this.T.regions.find((x) => x.id === regionId);
    if (mapId !== this.ids.map || !this.finished || !r) throw new ApiError(404, "no such region");
    if (this.isKnown(r) || this.planned.has(r.id)) throw new ApiError(409, "region known or planned");
    this.planned.add(r.id);
    await this.wait(this.o.splitMs ?? 0);
    this.expanded.add(r.id);
    const ch = this.T.children[r.id]!;
    // The demo's 概率论 special case: the first two blocks answered right, only 最大似然 is taught.
    const d = this.T.deep, todo = d && r.id === d.region && this.band[r.id] === "known" ? ch.pages.slice(d.skipPages) : ch.pages;
    const pathId = `fake-path-${r.id}`;
    this.paths.set(pathId, todo.map(([title, aim, exam], i) => ({ key: `${pathId}-p${i + 1}`, title, aim, exam: exam ?? 0, split: false, removed: false, parts: null })));
    return { ...this.base(), draft_id: null, page_id: null, path_id: pathId, map_id: null, round: 0,
      round_title: "", round_why: "", questions: [], results: [], decisions: [] };
  }

  /** The demo's blocks are planned into pages, never split into parts: no point of it is split further. */
  splitNode(nodeId: string, mapId: string): Promise<SplitOut> { return Promise.reject(new ApiError(404, `no such node: ${nodeId} on ${mapId}`)); }

  getPath(pathId: string): Promise<PathOut> {
    const rows = this.paths.get(pathId);
    const r = this.T.regions.find((x) => `fake-path-${x.id}` === pathId);
    if (!rows || !r) return Promise.reject(new ApiError(404, "no such path"));
    return Promise.resolve({ path_id: pathId, goal: `${this.goal} · ${r.name}`, source: "llm", next_index: 1, prefetch: null, sources: [],
      pages: rows.filter((x) => !x.removed).map((x, k) => ({ index: k + 1, aim: x.aim, status: "not_started" as const, state: "planned" as const, progress: null, page_id: null, trashed: false, skipped: false, key: x.key,
        title: x.title, abstract: "", sections: [], exam: x.exam, concepts: [{ concept_id: x.key, name: x.title, target: true }] })) });
  }

  pathNext(pathId: string): Promise<PathNextOut> {
    return this.getPath(pathId).then((p) => ({ path_id: p.path_id, index: 1, page_id: `fake-page-${pathId}`, prefetched: false }));
  }

  // ---- the 学习路径 card (POST /wiki/maps/{id}/edits) ----

  editMap(mapId: string, ops: MapEditOpIn[]): Promise<MapEditOut> {
    if (mapId !== this.ids.map || !this.finished) return Promise.reject(new ApiError(404, "no such map"));
    const T = clone(this.T);
    const undo: MapEditOpIn[] = [];
    for (const op of ops) {
      if (op.op === "insert") {           // 撤销 of a drop: that block back at its place, later edits kept
        const saved = this.dropped.get(op.node.id);
        if (!saved || T.regions.some((r) => r.id === op.node.id || r.name === op.node.name)) {
          return Promise.reject(new ApiError(409, "后面又改过，不能这样撤销"));
        }
        T.regions.splice(Math.min(op.index, T.regions.length), 0, saved);
        undo.unshift({ op: "drop", id: saved.id });
        continue;
      }
      if (op.op === "add") {
        const id = `u${++this.addSeq}`;
        T.regions.push({ id, name: op.name, scope: (op.covers ?? []).join("、"), prereqs: [] });
        T.children[id] = { pages: [[op.name, `能说清「${op.name}」是怎么回事`]], concepts: [["c", op.name, []]] };
        this.band[id] = "none";
        undo.unshift({ op: "drop", id });
        continue;
      }
      if (op.op === "reorder") {
        const all = T.regions.map((r) => r.id);
        if (op.ids.length !== all.length || !all.every((id) => op.ids.includes(id))) return Promise.reject(new ApiError(422, "要列出每一块"));
        T.regions = op.ids.map((id) => T.regions.find((r) => r.id === id)!);
        undo.unshift({ op: "reorder", ids: all });
        continue;
      }
      const at = T.regions.findIndex((r) => r.id === op.id);
      if (at < 0) return Promise.reject(new ApiError(422, "没有这一块"));
      const was = T.regions[at]!;
      if (op.op === "drop") {
        T.regions.splice(at, 1);
        this.dropped.set(was.id, was);
        undo.unshift({ op: "insert", index: at, node: { id: was.id, kind: "region", name: was.name, parent: null, scope: was.scope,
          prereqs: was.prereqs, target: was.target ?? false, state: "collapsed", path_id: null, page_id: null,
          reused: false, members: [], level: was.level ?? "" } });
      } else {
        T.regions[at] = { ...was, name: op.name };
        undo.unshift({ op: "rename", id: was.id, name: was.name });
      }
    }
    this.T = T;
    return Promise.resolve({ map: this.map(), undo });
  }

  // ---- a block's page list (the 知识图谱 path routes) ----

  private rowsOut(pathId: string): PathEditOut {
    const rows = this.paths.get(pathId)!;
    const items: PathItemOut[] = rows.filter((x) => !x.removed).map((x) => ({ key: x.key, title: x.title, summary: x.aim, question: "",
      node_ids: x.split ? [] : [x.key], status: "not_started", state: "planned", progress: null, skipped: false, mine: false, page_id: null, trashed: false, skippable: false,
      opened: false, sections: [], exam: x.exam }));
    return { path: items, undo: null, kept: "" };
  }

  editPath(pathId: string, edit: PathEditIn): Promise<PathEditOut> {
    const rows = this.paths.get(pathId);
    if (!rows) return Promise.reject(new ApiError(404, "no such path"));
    const shown = rows.filter((x) => !x.removed);
    if (edit.op === "move") {
      const x = shown[edit.from], y = shown[edit.to];
      if (!x || !y) return Promise.reject(new ApiError(422, "no such page"));
      rows.splice(rows.indexOf(x), 1);
      rows.splice(rows.indexOf(y) + (edit.to > edit.from ? 1 : 0), 0, x);
    } else if (edit.op === "remove") {
      const x = shown[edit.index];
      if (!x) return Promise.reject(new ApiError(422, "no such page"));
      x.removed = true;
    } else if (edit.op === "unremove") {
      const x = rows.find((r) => r.key === edit.page_key);
      if (!x) return Promise.reject(new ApiError(422, "no such page"));
      x.removed = false;
    } else if (edit.op === "retitle") {
      const x = rows.find((r) => r.key === edit.page_key);
      if (!x) return Promise.reject(new ApiError(422, "no such page"));
      x.title = edit.title;
    } else {
      rows.push({ key: `u-${pathId}-${rows.length + 1}`, title: edit.title, aim: "", exam: 0, split: true, removed: false, parts: null });
    }
    return Promise.resolve(this.rowsOut(pathId));
  }

  /** The demo's 拆成多页: 「X是什么」「X怎么做」 take the row's place; the parts are not split again. */
  splitRow(pathId: string, pageKey: string): Promise<PathEditOut> {
    const rows = this.paths.get(pathId);
    const x = rows?.find((r) => r.key === pageKey && !r.removed);
    if (!rows || !x) return Promise.reject(new ApiError(404, "no such page"));
    if (x.split) return Promise.reject(new ApiError(422, "这一页没有对应的点，不能拆成多页"));
    const parts: FakeRow[] = [`${x.title}是什么`, `${x.title}怎么做`].map((t, k) => ({ key: `${pageKey}.s${k + 1}`, title: t, aim: "", exam: 0, split: true, removed: false, parts: null }));
    x.removed = true; x.parts = parts.map((p) => p.key);
    rows.splice(rows.indexOf(x) + 1, 0, ...parts);
    return Promise.resolve({ ...this.rowsOut(pathId), undo: `split-${pageKey}` });
  }

  undoSplitRow(pathId: string, pageKey: string, changeId: string): Promise<PathEditOut> {
    const rows = this.paths.get(pathId);
    const x = rows?.find((r) => r.key === pageKey);
    if (!rows || !x || x.parts === null || changeId !== `split-${pageKey}`) return Promise.reject(new ApiError(409, "这次拆分已经不能撤销了"));
    for (const k of x.parts) { const i = rows.findIndex((r) => r.key === k); if (i >= 0) rows.splice(i, 1); }
    x.removed = false; x.parts = null;
    return Promise.resolve(this.rowsOut(pathId));
  }
}

/**
 * 学点新的 with plugin data `fixture: "demo"` (user review): the scripted fake answers ONLY the goals confirmed in the
 * fixture's own chat (their draft ids are the fake's); any other goal is a real goal and goes to the live big-goal routes.
 * The choice is made by getDraft and every later call of the flow goes to the same API. Before a goal is chosen `live` is
 * false (the view then prefills the demo goal when no topic was given, as the screenshot needs).
 */
export class FixtureGoalApi implements GoalApi {
  private chosen: GoalApi | null = null;
  constructor(readonly fake: GoalApi, readonly http: GoalApi) {}
  get live(): boolean { return this.chosen?.live ?? false; }
  private on(): GoalApi { return this.chosen ?? this.http; }
  getDraft(draftId: string): Promise<GoalDraftOut> {
    this.chosen = isFakeId(draftId) ? this.fake : this.http;
    return this.chosen.getDraft(draftId);
  }
  answer(draftId: string, body: GoalAnswer): Promise<GoalDraftOut> { return this.on().answer(draftId, body); }
  getMap(mapId: string): Promise<GoalMapOut> { return this.on().getMap(mapId); }
  enterRegion(mapId: string, regionId: string): Promise<GoalDraftOut> { return this.on().enterRegion(mapId, regionId); }
  splitNode(nodeId: string, mapId: string): Promise<SplitOut> { return this.on().splitNode(nodeId, mapId); }
  getPath(pathId: string): Promise<PathOut> { return this.on().getPath(pathId); }
  pathNext(pathId: string): Promise<PathNextOut> { return this.on().pathNext(pathId); }
  editMap(mapId: string, ops: MapEditOpIn[]): Promise<MapEditOut> { return this.on().editMap(mapId, ops); }
  editPath(pathId: string, edit: PathEditIn): Promise<PathEditOut> { return this.on().editPath(pathId, edit); }
  splitRow(pathId: string, pageKey: string): Promise<PathEditOut> { return this.on().splitRow(pathId, pageKey); }
  undoSplitRow(pathId: string, pageKey: string, changeId: string): Promise<PathEditOut> { return this.on().undoSplitRow(pathId, pageKey, changeId); }
}
