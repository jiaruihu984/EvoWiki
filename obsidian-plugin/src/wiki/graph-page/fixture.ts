/**
 * The approved node-graph demo's data (the product specification) as a /wiki/graph server:
 * FakeWikiApi and DemoWikiApi answer GET /wiki/graph, split / want and the path edits from one GraphFixture.
 *
 * What graph.html has and where it lives here:
 * - `N` (one kind of node; parts = part_of, pre = prerequisites with a phrase): DEMO_NODES, verbatim. The containers the
 *   demo names only as group ids (la / ca / pr under 数学; opt / sl / ev / ul under 机器学习; tools) are real nodes here,
 *   each with part_of to its parent, as the spec says (a region is a node with children). pca has both parents the
 *   demo's two goals give it (无监督学习 and 线性代数): the card's 属于 lists every parent.
 * - `VIEWS.all.l2` / `VIEWS.ml.l1` (the clustering at two zoom levels; the default view is 全部): the level-2 and
 *   level-1 groups, with the demo's names, summary lines and `at` positions. The view switch itself does not exist in
 *   the plugin; only the default view's groups are served.
 * - Node positions: the demo lays a group's members on a ring around the group's `at` (layout(t) in graph.html). The
 *   same ring, divided by the demo's spread, is stored as each node's x, y, so the page (which maps x, y exactly as the
 *   demo maps `at`) draws every node where the demo draws it. Split children sit where the demo grows them.
 * - The goals 我要学机器学习 / 我要学数学: their node subsets (VIEWS.ml.members / VIEWS.math.members) and paths (PATHS).
 * - `split` of 矩阵乘法 (看不懂): its three children with their bands and evidence (nodeOf in graph.html).
 * Band / mix / aggregate / shares are derived the way the spec says (direct cells weighted 2:1 over the aggregate).
 */
import { ApiError } from "../../api/errors";
import type { NodeActionOut } from "./parse";
import type {
  Band, BandShares, GoalOut, GraphNodeOut, GraphOut, GroupOut, PathEditIn, PathEditOut, PathItemOut, RelationOut, SplitOut,
} from "../schema";

/** A stable 0..1 from a string (the demo's seed). */
const seed = (s: string): number => { let h = 0; for (const ch of s) h = (h * 31 + ch.charCodeAt(0)) | 0; return (h >>> 0) / 4294967296; };

type Pre = [string, string];
interface DemoNode {
  name: string; band: Band | null; parts: string[]; pre: Pre[]; ev: string[]; kind?: string;
  /** [child id, name, band] (graph.html N.mul.split). */ split?: [string, string, Band][];
}

/** graph.html `N`, verbatim, plus the containers it names only as group ids (bands null = no direct evidence). */
export const DEMO_NODES: Record<string, DemoNode> = {
  vec: { name: "向量和点积", band: "known", parts: ["la"], pre: [], ev: ["测验答对：两个向量的点积"] },
  mul: { name: "矩阵乘法", band: "shaky", parts: ["la"], pre: [["vec", "一行乘一列就是点积"]], ev: ["测验答错：2×3 乘 3×4 的结果形状"],
    split: [["rc", "一行乘一列", "known"], ["dim", "维度要对上", "gap"], ["comm", "不满足交换律", "none"]] },
  eig: { name: "特征值与特征向量", band: "shaky", parts: ["la"], pre: [["mul", "Av 就是矩阵乘向量"]], ev: ["复习时想起来了，有点费劲"] },
  la: { name: "线性代数", band: "known", parts: ["math"], pre: [], ev: ["精髓题答对：奇异值分解在做什么"] },
  der: { name: "导数", band: "known", parts: ["ca"], pre: [], ev: ["你说：大学学过高数"] },
  grad: { name: "梯度", band: "gap", parts: ["ca"], pre: [["der", "梯度是每个方向上的导数排在一起"]], ev: ["测验答错：梯度指向哪里"] },
  ca: { name: "微积分", band: null, parts: ["math"], pre: [], ev: [] },
  mean: { name: "均值", band: "known", parts: ["pr"], pre: [], ev: ["测验答对"] },
  var: { name: "方差", band: "known", parts: ["pr"], pre: [["mean", "方差量的是离均值有多远"]], ev: ["测验答对"] },
  cond: { name: "条件概率", band: "none", parts: ["pr"], pre: [], ev: [] },
  pr: { name: "概率统计", band: null, parts: ["math"], pre: [], ev: [] },
  py: { name: "Python", band: "known", parts: ["tools"], pre: [], ev: ["你说：会写 Python"] },
  gd: { name: "梯度下降", band: "none", parts: ["opt"], pre: [["grad", "每一步沿梯度的反方向走"]], ev: [] },
  linreg: { name: "线性回归", band: "none", parts: ["sl"], pre: [["mul", "预测值 = Xw，就是矩阵乘法"], ["gd", "用梯度下降找到最好的 w"]], ev: [] },
  loss: { name: "损失函数", band: "none", parts: ["sl"], pre: [["linreg", "量的是预测和真实差多远"]], ev: [] },
  logreg: { name: "逻辑回归", band: "none", parts: ["sl"], pre: [["linreg", "在线性回归外面套一个 S 形函数"], ["cond", "输出的是条件概率"]], ev: [] },
  svm: { name: "SVM", band: "none", parts: ["sl"], kind: "一种分类模型", pre: [["vec", "间隔用点积来算"]], ev: [] },
  tree: { name: "决策树", band: "none", parts: ["sl"], kind: "一种分类模型", pre: [], ev: [] },
  nn: { name: "神经网络", band: "none", parts: ["sl"], pre: [["logreg", "每个神经元就是一个逻辑回归"], ["gd", "靠梯度下降训练"]], ev: [] },
  reg: { name: "正则化", band: "none", parts: ["ev"], pre: [["loss", "在损失上加一项惩罚"]], ev: [] },
  of: { name: "过拟合与泛化", band: "none", parts: ["ev"], pre: [], ev: [] },
  km: { name: "k-means", band: "none", parts: ["ul"], pre: [["mean", "每一类的中心就是均值"]], ev: [] },
  pca: { name: "PCA 降维", band: "none", parts: ["ul", "la"], pre: [["eig", "主方向就是协方差矩阵的特征向量"], ["var", "保留方差最大的方向"]], ev: [] },
  // the containers graph.html names only as group ids
  math: { name: "数学", band: null, parts: [], pre: [], ev: [] },
  opt: { name: "优化", band: null, parts: ["ml"], pre: [], ev: [] },
  sl: { name: "监督学习", band: null, parts: ["ml"], pre: [], ev: [] },
  ev: { name: "模型评估", band: null, parts: ["ml"], pre: [], ev: [] },
  ul: { name: "无监督学习", band: null, parts: ["ml"], pre: [], ev: [] },
  ml: { name: "机器学习", band: null, parts: [], pre: [], ev: [] },
  tools: { name: "工具", band: null, parts: [], pre: [], ev: [] },
};

/** graph.html VIEWS.ml.l1 (= VIEWS.all.l1): [name, summary, at, members]. */
export const DEMO_L1: Record<string, [string, string, [number, number], string[]]> = {
  la: ["线性代数", "矩阵和向量的运算", [-0.95, -0.55], ["vec", "mul", "eig", "pca"]],
  ca: ["微积分", "变化率和最陡的方向", [-1.05, 0.25], ["der", "grad"]],
  pr: ["概率统计", "数据的中心、散开程度和不确定性", [-0.75, 0.85], ["mean", "var", "cond"]],
  opt: ["优化", "一步步把损失降下来", [-0.15, 0.05], ["gd"]],
  sl: ["监督学习", "有标签的数据学预测", [0.45, -0.45], ["linreg", "loss", "logreg", "svm", "tree", "nn"]],
  ev: ["模型评估", "学得好不好、会不会过拟合", [0.95, 0.25], ["reg", "of"]],
  ul: ["无监督学习", "没有标签时找结构", [0.35, 0.75], ["km", "pca"]],
  tools: ["工具", "写代码把模型跑起来", [-0.15, -0.95], ["py"]],
};
/** graph.html VIEWS.all.l2 (the default view 全部). */
export const DEMO_L2: { id: string; name: string; sum: string; kids: string[]; map_id?: string }[] = [
  { id: "A", name: "数学", sum: "代数、微积分和概率统计", kids: ["la", "ca", "pr"], map_id: "map_math" },
  { id: "B", name: "机器学习", sum: "从数据里学出模型，再检验它好不好", kids: ["opt", "sl", "ev", "ul"], map_id: "map_ml" },
  { get id() { return "C"; }, name: "工具", sum: "动手写代码", kids: ["tools"] },
];
/** graph.html: the members of a view are the nodes that are not groups (the containers above are groups there). */
const LEAVES = Object.keys(DEMO_NODES).filter((k) => !(k in DEMO_L1) && k !== "math" && k !== "ml");
/** graph.html VIEWS.ml.members / VIEWS.math.members. */
export const DEMO_GOALS: { map_id: string; goal: string; name: string; path_id: string; nodes: string[] }[] = [
  { map_id: "map_ml", goal: "我要学机器学习", name: "机器学习", path_id: "path_ml", nodes: LEAVES },
  { map_id: "map_math", goal: "我要学数学", name: "数学", path_id: "path_math", nodes: ["vec", "mul", "eig", "der", "grad", "mean", "var", "cond", "pca"] },
];
/** graph.html PATHS: [title, the question the page answers, nodes, 真题 count (PathItemOut.exam; absent = 0)]. */
export const DEMO_PATHS: Record<string, [string, string, string[], number?][]> = {
  path_ml: [
    ["矩阵乘法", "两张表怎么乘出一张新表", ["mul"]],
    ["导数和梯度", "往哪边走下降得最快", ["grad"]],
    ["梯度下降", "怎么一步步走到最低点", ["gd"], 5],
    ["线性回归和损失函数", "怎么画一条最贴合数据的线", ["linreg", "loss"], 3],
    ["逻辑回归", "怎么算出是或不是的可能", ["logreg", "cond"]],
    ["正则化和过拟合", "模型是不是在背答案", ["reg", "of"]],
    ["SVM 和决策树", "两类之间的线该画在哪", ["svm", "tree"]],
    ["神经网络入门", "一层层叠起来为什么更厉害", ["nn"]],
    ["无监督：k-means 和 PCA", "没有答案时怎么把数据分堆", ["km", "pca"]],
  ],
  path_math: [
    ["向量和矩阵乘法", "两个箭头有多同向，两张表怎么相乘", ["vec", "mul"]],
    ["特征值与特征向量", "哪些方向转完还朝原来那边", ["eig"]],
    ["导数和梯度", "这一刻变得有多快，往哪边最陡", ["der", "grad"]],
    ["条件概率", "知道一件事之后，另一件事有多可能", ["cond"]],
  ],
};

/** One dataset the fixture serves: nodes, level-1 groups, goals, paths. */
export interface GraphData {
  nodes: Record<string, DemoNode>; l1: typeof DEMO_L1; goals: typeof DEMO_GOALS; paths: typeof DEMO_PATHS;
  /** How many leading rows of a path are written and read (status done). */
  done?: Record<string, number>;
}
/** graph.html at the node-graph spec (the tests pin this one). */
export const DEMO_GRAPH: GraphData = { nodes: DEMO_NODES, l1: DEMO_L1, goals: DEMO_GOALS, paths: DEMO_PATHS };
const leaves = (nodes: Record<string, DemoNode>, l1: typeof DEMO_L1): string[] =>
  Object.keys(nodes).filter((k) => !(k in l1) && k !== "math" && k !== "ml");
/**
 * graph.html as the assistant-actions spec left it (2026-10-15): eig splits too, cov / pcloss / proj / bayes, the goals
 * 学会 PCA and 看懂贝叶斯公式, one node per row. The dev fixture (real-Obsidian comparison) serves this one.
 */
export const CURRENT_GRAPH: GraphData = (() => {
  const nodes: Record<string, DemoNode> = {
    ...DEMO_NODES,
    eig: { ...DEMO_NODES.eig!, split: [["dir", "方向不变的向量", "shaky"], ["lam", "拉长的倍数", "known"], ["det", "用 det(A − λI) = 0 求", "gap"]] },
    pca: { ...DEMO_NODES.pca!, pre: [["eig", "主方向就是协方差矩阵的特征向量"], ["cov", "主成分是协方差矩阵的特征向量"]] },
    cov: { name: "协方差矩阵", band: "shaky", parts: ["pr"], pre: [["var", "对角线上就是各自的方差"]], ev: ["测验答对一半"] },
    pcloss: { name: "降维丢了多少信息", band: "none", parts: ["ul"], pre: [["pca", "丢掉的是没选上的主成分的方差"]], ev: [] },
    proj: { name: "投影", band: "gap", parts: ["la"], pre: [["vec", "投影长度 = a·b / |b|"]], ev: ["测验答错 3 道：忘了除以 |b|"] },
    bayes: { name: "贝叶斯公式", band: "none", parts: ["pr"], pre: [], ev: [] },
  };
  const l1: typeof DEMO_L1 = {
    ...DEMO_L1,
    la: ["线性代数", "矩阵和向量的运算", [-0.95, -0.55], ["vec", "mul", "eig", "proj", "pca"]],
    pr: ["概率统计", "数据的中心、散开程度和不确定性", [-0.75, 0.85], ["mean", "var", "cov", "cond", "bayes"]],
    ul: ["无监督学习", "没有标签时找结构", [0.35, 0.75], ["km", "pca", "pcloss"]],
  };
  const goals: typeof DEMO_GOALS = [
    { map_id: "map_ml", goal: "我要学机器学习", name: "机器学习", path_id: "path_ml", nodes: leaves(nodes, l1) },
    { map_id: "map_math", goal: "我要学数学", name: "数学", path_id: "path_math", nodes: ["vec", "mul", "eig", "der", "grad", "mean", "var", "cond", "pca"] },
    { map_id: "map_pca", goal: "学会 PCA", name: "PCA", path_id: "path_pca", nodes: ["vec", "mul", "eig", "mean", "var", "cov", "pca", "pcloss", "proj"] },
    { map_id: "map_bayes", goal: "看懂贝叶斯公式", name: "贝叶斯公式", path_id: "path_bayes", nodes: ["bayes"] },
  ];
  const paths: typeof DEMO_PATHS = {
    path_ml: [
      ["矩阵乘法", "两张表怎么乘出一张新表", ["mul"]], ["梯度", "往哪边走下降得最快", ["grad"]],
      ["梯度下降", "怎么一步步走到最低点", ["gd"], 5], ["线性回归", "怎么画一条最贴合数据的线", ["linreg"]],
      ["损失函数", "模型错得有多离谱", ["loss"], 3], ["逻辑回归", "怎么算出是或不是的可能", ["logreg"]],
      ["正则化", "怎么让模型别死记硬背", ["reg"]], ["过拟合与泛化", "模型是不是在背答案", ["of"]],
      ["SVM", "两类之间的线该画在哪", ["svm"]], ["决策树", "怎么靠一串是非题做判断", ["tree"]],
      ["神经网络", "一层层叠起来为什么更厉害", ["nn"]], ["k-means", "没有答案时怎么把数据分堆", ["km"]],
      ["PCA 降维", "怎么用更少的数说清数据", ["pca"]],
    ],
    path_math: [
      ["向量和点积", "两个箭头有多同向", ["vec"]], ["矩阵乘法", "两张表怎么乘出一张新表", ["mul"]],
      ["特征值与特征向量", "哪些方向转完还朝原来那边", ["eig"]], ["导数", "这一刻变得有多快", ["der"]],
      ["梯度", "往哪边走下降得最快", ["grad"]], ["条件概率", "知道一件事之后，另一件事的可能性", ["cond"]],
    ],
    path_pca: [
      ["均值和方差", "一组数有多分散", ["var"]], ["特征值与特征向量", "哪些方向转完还朝原来那边", ["eig"]],
      ["协方差矩阵", "两个量是不是一起变", ["cov"]], ["主成分怎么选", "数据最散开的是哪个方向", ["pca"]],
      ["降维丢了多少信息", "少留几个方向会损失多少", ["pcloss"]], ["投影（补讲）", "一个箭头在另一个方向上有多长", ["proj"]],
    ],
    path_bayes: [["贝叶斯公式", "检测阳性就一定得病了吗", ["bayes"]]],
  };
  // graph.html: "pages 1–2 of each path are written" (app.js 今天: 第 3 / n 页)
  return { nodes, l1, goals, paths, done: { path_ml: 2, path_math: 2, path_pca: 2 } };
})();
/** What the fake writes for the learner's own pages (graph.html: 我想学这个 / 加上). */
export const MINE_SUMMARY = "你加的";
export const MINE_NEW_SUMMARY = "你加的，会在图谱里找到或新建对应的点";
/** graph.html: what 我会了，跳过 adds to the evidence; what a split child's own test said (nodeOf). */
export const SKIP_EVIDENCE = "你说：我会了";
const CHILD_EVIDENCE: Partial<Record<Band, string>> = { known: "拆细后单独测：答对", gap: "拆细后单独测：答错" };

/** The demo's layout constants (graph.html layout(t)): spread = sc·0.34·scale, x stretched by 1.15. */
const SPREAD = 0.34, X_STRETCH = 1.15;
/** A group member's offset from the group centre on the demo's ring, in `at` units (independent of size and zoom). */
export function ringOffset(gid: string, members: string[], i: number, k: string): { ang: number; dx: number; dy: number } {
  const n = members.length, ang = (i / n) * 6.283 + seed(gid) * 6.283;
  const ring = n === 1 ? 0 : (0.05 + 0.012 * n) * (0.8 + 0.3 * seed(k));
  return { ang, dx: (Math.cos(ang) * ring) / (SPREAD * X_STRETCH), dy: (Math.sin(ang) * ring * 0.8) / SPREAD };
}
/** A split child's offset from its parent (graph.html: angle ang + (j − (n−1)/2)·0.9, radius sc·0.07·scale). */
export function childOffset(parentAng: number, j: number, n: number): { dx: number; dy: number } {
  const a2 = parentAng + (j - (n - 1) / 2) * 0.9;
  return { dx: (Math.cos(a2) * 0.07) / (SPREAD * X_STRETCH), dy: (Math.sin(a2) * 0.07) / SPREAD };
}

const SCORE: Record<Exclude<Band, "none">, number> = { known: 1, likely: 0.78, shaky: 0.45, gap: 0.08 };
/** The band word of a mix: no evidence → none; else the band whose score is nearest the weighted score. */
export function bandOfShares(s: BandShares): Band {
  let w = 0, sum = 0;
  for (const b of ["known", "likely", "shaky", "gap"] as const) { const v = s[b] ?? 0; w += v; sum += v * SCORE[b]; }
  if (w <= 0) return "none";
  const avg = sum / w;
  let best: Exclude<Band, "none"> = "gap";
  for (const b of ["known", "likely", "shaky", "gap"] as const) if (Math.abs(SCORE[b] - avg) < Math.abs(SCORE[best] - avg)) best = b;
  return best;
}

interface NodeRec {
  id: string; name: string; direct: Band | null; parents: string[]; evidence: string[]; kind: string; x: number; y: number;
  split: [string, string, Band][] | null; ang: number;
}
interface GoalRec { map_id: string; goal: string; name: string; nodes: string[]; path_id: string; path: PathItemOut[] }

const http = (status: number, detail: string): ApiError => new ApiError(status, detail);

/** One user's graph on the fake server: the demo's data, changed by split / want / path edits. */
export class GraphFixture {
  private readonly nodes = new Map<string, NodeRec>();
  private readonly relations: RelationOut[] = [];
  private readonly l1: GroupOut[] = [];
  private readonly l2: GroupOut[] = [];
  private readonly goals: GoalRec[] = [];
  private seq = 0;
  private readonly addedIntents = new Set<string>();
  private readonly removed = new Map<string, { pathId: string; row: PathItemOut; index: number }>();
  private readonly splits = new Map<string, { pathId: string; row: PathItemOut; keys: string[] }>();

  /**
   * `pageIds`: every row has its page (`pg_<row key>`, write-all) so the rows can show a writing status; `data`: the dataset
   * (DEMO_GRAPH by default; the dev fixture serves CURRENT_GRAPH).
   */
  private readonly d: GraphData;
  constructor(o: { pageIds?: boolean; data?: GraphData } = {}) {
    const d = (this.d = o.data ?? DEMO_GRAPH);
    const home = new Map<string, { gid: string; i: number; members: string[] }>();
    for (const [gid, [, , , members]] of Object.entries(d.l1)) members.forEach((k, i) => home.set(k, { gid, i, members }));
    for (const [id, n] of Object.entries(d.nodes)) {
      const h = home.get(id);
      let x = 0, y = 0, ang = 0;
      if (h) {
        const at = d.l1[h.gid]![2], o = ringOffset(h.gid, h.members, h.i, id);
        x = at[0] + o.dx; y = at[1] + o.dy; ang = o.ang;
      } else if (id in d.l1) { [x, y] = d.l1[id]![2]; }
      this.nodes.set(id, { id, name: n.name, direct: n.band === "none" ? null : n.band, parents: [...n.parts], evidence: [...n.ev],
        kind: n.kind ?? "", x, y, split: n.split ? n.split.map((s) => [...s]) : null, ang });
    }
    // math / ml: the centre of their parts (never drawn; the containers' own positions).
    for (const top of ["math", "ml"]) {
      const kids = [...this.nodes.values()].filter((n) => n.parents.includes(top));
      const t = this.nodes.get(top)!;
      t.x = kids.reduce((s, n) => s + n.x, 0) / kids.length; t.y = kids.reduce((s, n) => s + n.y, 0) / kids.length;
    }
    for (const [id, n] of Object.entries(d.nodes)) for (const [q, phrase] of n.pre) this.relations.push({ kind: "prerequisite", src: q, dst: id, phrase, verified: false, split: false });
    for (const n of this.nodes.values()) for (const p of n.parents) this.relations.push({ kind: "part_of", src: n.id, dst: p, phrase: "", verified: false, split: false });
    for (const g of DEMO_L2) {
      const kids = g.kids.map((k) => d.l1[k]!);
      this.l2.push({ id: g.id, level: 2, name: g.name, summary: g.sum, members: kids.flatMap((k) => k[3]), parent: null, at: null, map_id: g.map_id ?? null, copies: {},
        x: kids.reduce((s, k) => s + k[2][0], 0) / kids.length, y: kids.reduce((s, k) => s + k[2][1], 0) / kids.length });
      for (const k of g.kids) {
        const [name, sum, at, members] = d.l1[k]!;
        this.l1.push({ id: k, level: 1, name, summary: sum, members: [...members], parent: g.id, x: at[0], y: at[1], at: null, map_id: g.map_id ?? null, copies: {} });
      }
    }
    for (const g of d.goals) {
      this.goals.push({ map_id: g.map_id, goal: g.goal, name: g.name, nodes: [...g.nodes], path_id: g.path_id,
        path: d.paths[g.path_id]!.map(([title, question, ids, exam], i) => ({ key: `${g.path_id}_${i + 1}`, title, summary: "", question, node_ids: [...ids],
          status: i < (d.done?.[g.path_id] ?? 0) ? "done" : "not_started", state: i < (d.done?.[g.path_id] ?? 0) ? "read" : "planned", progress: null, skipped: false, mine: false, page_id: o.pageIds === true ? `pg_${g.path_id}_${i + 1}` : null, trashed: false,
          skippable: false, opened: false, sections: [], exam: exam ?? 0 })) });
    }
  }

  graph(): GraphOut {
    const kids = (id: string): string[] => this.relations.filter((r) => r.kind === "part_of" && r.dst === id).map((r) => r.src);
    const descendants = (id: string): string[] => {
      const out = new Set<string>(); const stack = kids(id);
      while (stack.length) { const k = stack.pop()!; if (out.has(k)) continue; out.add(k); stack.push(...kids(k)); }
      return [...out];
    };
    const shares = (bands: Band[]): BandShares => {
      const s: BandShares = {};
      for (const b of bands) s[b] = (s[b] ?? 0) + 1 / bands.length;
      return s;
    };
    const nodes: GraphNodeOut[] = [...this.nodes.values()].map((n) => {
      const direct: BandShares = n.direct ? { [n.direct]: 1 } : {};
      const desc = descendants(n.id);
      const aggregate = desc.length ? shares(desc.map((d) => this.nodes.get(d)!.direct ?? "none")) : {};
      let mix: BandShares;
      if (!n.direct) mix = { ...aggregate };
      else if (!desc.length) mix = { ...direct };
      else {
        mix = {};
        for (const b of new Set([...Object.keys(direct), ...Object.keys(aggregate)]) as Set<Band>) mix[b] = (2 * (direct[b] ?? 0) + (aggregate[b] ?? 0)) / 3;
      }
      return { id: n.id, name: n.name, definition: "", band: bandOfShares(mix), mix, direct, aggregate, parents: [...n.parents],
        evidence: [...n.evidence], kind: n.kind, x: n.x, y: n.y, can_split: n.split !== null };
    });
    const band = new Map(nodes.map((n) => [n.id, n.band]));
    const goals: GoalOut[] = this.goals.map((g) => {
      const c = { known: 0, shaky: 0, gap: 0, none: 0 };
      for (const id of g.nodes) { const b = band.get(id) ?? "none"; c[b === "likely" ? "known" : b] += 1; }
      const total = g.nodes.length || 1;
      return { map_id: g.map_id, goal: g.goal, name: g.name, nodes: [...g.nodes], path_id: g.path_id, path: structuredClone(g.path), paths: [], sources: [], reading: null, next_region: null, next_name: "",
        shares: { known: c.known / total, shaky: c.shaky / total, gap: c.gap / total, none: c.none / total } };
    });
    return { nodes, relations: structuredClone(this.relations), groups: structuredClone([...this.l2, ...this.l1]), goals, domains: [] };
  }

  /** 看不懂: the demo's children grow out of the node (only nodes with split data can split, once). */
  split(nodeId: string): SplitOut {
    const n = this.node(nodeId);
    if (n.split === null) throw http(409, "这个点不能再拆细了");
    const parts = n.split; n.split = null;
    const g1 = this.l1.find((g) => g.members.includes(nodeId)), g2 = g1 ? this.l2.find((g) => g.id === g1.parent) : undefined;
    const children = parts.map(([cid, name, b], j) => {
      const o = childOffset(n.ang, j, parts.length);
      const ev = CHILD_EVIDENCE[b];
      this.nodes.set(cid, { id: cid, name, direct: b === "none" ? null : b, parents: [nodeId], evidence: ev ? [ev] : [], kind: "",
        x: n.x + o.dx, y: n.y + o.dy, split: null, ang: 0 });
      this.relations.push({ kind: "part_of", src: cid, dst: nodeId, phrase: "", verified: false, split: true });
      g1?.members.push(cid); g2?.members.push(cid);
      return cid;
    });
    return { children, graph: this.graph(), kept: "" };
  }

  /** 我想学这个: a page of the learner's own at the end of that goal's path. */
  want(nodeId: string, mapId: string): NodeActionOut {
    const n = this.node(nodeId), g = this.goal(mapId);
    if (g.path.some((p) => p.node_ids.includes(nodeId))) throw http(409, "这个点已经在路径里了");
    // Only the path changes: the goal's node subset stays the map's (graph.html; the server's want_node / graph_out).
    g.path.push(this.mine(n.name, MINE_SUMMARY, [nodeId]));
    return this.pathAnswer(g.path_id);
  }

  /** The want answer: that goal's path as GET /wiki/graph shows it. */
  private pathAnswer(pathId: string): NodeActionOut {
    return { ok: true, path_id: pathId, path: this.graph().goals.find((x) => x.path_id === pathId)!.path };
  }

  editPath(pathId: string, edit: PathEditIn): PathEditOut {
    const g = this.goals.find((x) => x.path_id === pathId);
    if (!g) throw http(404, `没有这条路径：${pathId}`);
    const P = g.path, inRange = (i: unknown): boolean => Number.isInteger(i) && (i as number) >= 0 && (i as number) < P.length;
    if (edit.op === "move") {
      if (!inRange(edit.from) || !inRange(edit.to)) throw http(422, "位置超出范围");
      const [pg] = P.splice(edit.from, 1); P.splice(edit.to, 0, pg!);
    } else if (edit.op === "remove") {
      const index = P.findIndex((r) => r.key === edit.page_key);
      if (index < 0) throw http(422, "路径里没有这一页");
      const [gone] = P.splice(index, 1);
      this.removed.set(gone!.key, { pathId, row: gone!, index });
    } else if (edit.op === "unremove") {
      const r = this.removed.get(edit.page_key);
      if (!r || r.pathId !== pathId) throw http(422, `这一页不是被删掉的：${edit.page_key}`);
      this.removed.delete(edit.page_key);
      P.splice(Math.min(r.index, P.length), 0, r.row);
    } else if (edit.op === "retitle") {
      const r = P.find((x) => x.key === edit.page_key);
      if (!r) throw http(422, `路径里没有这一页：${edit.page_key}`);
      r.title = edit.title;
    } else {
      const title = edit.title.trim();
      if (!title || title.length > 200) throw http(422, "标题 1–200 字");
      const intent = JSON.stringify([pathId, edit.request_id]);
      if (this.addedIntents.has(intent)) return { path: structuredClone(P), undo: null, kept: "" };
      const nodeId = edit.node_id ?? null, toIndex = edit.to_index ?? null;
      if (nodeId !== null) this.node(nodeId);
      const at = toIndex !== null ? Math.min(toIndex, P.length) : P.length;
      P.splice(at, 0, this.mine(title, nodeId !== null ? MINE_SUMMARY : MINE_NEW_SUMMARY, nodeId !== null ? [nodeId] : []));
      this.addedIntents.add(intent);
    }
    return { path: structuredClone(P), undo: null, kept: "" };
  }

  /** 拆成多页: a one-node row splits its node (the graph grows the children) and a row per child takes its place. */
  splitRow(pathId: string, pageKey: string): PathEditOut {
    const g = this.goals.find((x) => x.path_id === pathId);
    if (!g) throw http(404, `没有这条路径：${pathId}`);
    const at = g.path.findIndex((p) => p.key === pageKey);
    if (at < 0) throw http(422, `路径里没有这一页：${pageKey}`);
    const row = g.path[at]!;
    if (row.node_ids.length === 0) throw http(422, "这一页没有对应的点，不能拆成多页");
    const ids = row.node_ids.length > 1 ? row.node_ids : this.split(row.node_ids[0]!).children;
    const rows = ids.map((id): PathItemOut => ({ ...this.mine(this.node(id).name, "", [id]), mine: false }));
    g.path.splice(at, 1, ...rows);
    const token = `split_${++this.seq}`;
    this.splits.set(token, { pathId, row, keys: rows.map((r) => r.key) });
    return { path: structuredClone(g.path), undo: token, kept: "" };
  }

  /** Takes 拆成多页 back: the original row where the first part is (the graph keeps the finer points). */
  undoSplitRow(pathId: string, pageKey: string, changeId: string): PathEditOut {
    const g = this.goals.find((x) => x.path_id === pathId);
    const rec = this.splits.get(changeId);
    if (!g || !rec || rec.pathId !== pathId || rec.row.key !== pageKey) throw http(404, `没有这次拆分：${changeId}`);
    const at = g.path.findIndex((p) => p.key === rec.keys[0]);
    if (at < 0) throw http(409, "路径之后变了，不能撤销");
    g.path = g.path.filter((p) => !rec.keys.includes(p.key));
    g.path.splice(at, 0, structuredClone(rec.row));
    this.splits.delete(changeId);
    return { path: structuredClone(g.path), undo: null, kept: "" };
  }

  /** The path as it is (重新规划 in the fake keeps it: the real replan asks the model). */
  pathOf(pathId: string): PathEditOut {
    const g = this.goals.find((x) => x.path_id === pathId);
    if (!g) throw http(404, `没有这条路径：${pathId}`);
    return { path: structuredClone(g.path), undo: null, kept: "" };
  }
  /** (path id, goal) of every goal with a path. */
  goalPaths(): { path_id: string; goal: string }[] {
    return this.goals.map((g) => ({ path_id: g.path_id, goal: g.goal }));
  }

  private mine(title: string, question: string, ids: string[]): PathItemOut {
    return { key: `mine_${++this.seq}`, title, summary: "", question, node_ids: ids, status: "not_started", state: "planned", progress: null, skipped: false, mine: true, page_id: null, trashed: false, skippable: false, opened: false, sections: [], exam: 0 };
  }
  private node(id: string): NodeRec {
    const n = this.nodes.get(id);
    if (!n) throw http(404, `没有这个点：${id}`);
    return n;
  }
  private goal(mapId: string): GoalRec {
    const g = this.goals.find((x) => x.map_id === mapId);
    if (!g) throw http(404, `没有这个目标：${mapId}`);
    return g;
  }
}
