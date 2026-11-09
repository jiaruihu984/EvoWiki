/** Test fixture: the prototype's 18 concepts as a K grid, an empty catalog and goal membership. */
import { ASPECTS, type Band, type CatalogOut, type CellBeliefOut, type KConceptOut, type KGridOut } from "../schema";
import type { GoalMembership } from "./model";

type Row = [id: string, name: string, cluster: string, band: Band, known: number, pre: string[]];
export const ROWS: Row[] = [
  ["mean", "均值", "统计", "known", 6, []],
  ["var", "方差", "统计", "likely", 4, ["mean"]],
  ["sd", "标准差", "统计", "likely", 3, ["var"]],
  ["cov", "协方差", "统计", "shaky", 1, ["var"]],
  ["corr", "相关系数", "统计", "none", 0, ["cov", "sd"]],
  ["vec", "向量", "线性代数", "known", 5, []],
  ["mat", "矩阵", "线性代数", "likely", 4, ["vec"]],
  ["mm", "矩阵乘法", "线性代数", "likely", 3, ["mat"]],
  ["eig", "特征值与特征向量", "线性代数", "gap", 0, ["mm"]],
  ["orth", "正交", "线性代数", "shaky", 2, ["vec"]],
  ["pca", "主成分分析", "机器学习", "gap", 0, ["cov", "eig", "orth"]],
  ["dimr", "降维", "机器学习", "none", 0, ["pca"]],
  ["dna", "DNA 结构", "生物", "likely", 3, []],
  ["nuc", "核苷酸", "生物", "shaky", 2, ["dna"]],
  ["base", "碱基", "生物", "likely", 3, ["nuc"]],
  ["hb", "氢键", "生物", "shaky", 1, []],
  ["pair", "基因配对", "生物", "likely", 3, ["base", "hb"]],
  ["rep", "DNA 复制", "生物", "none", 0, ["pair"]],
];

export function cell(conceptId: string, aspect: (typeof ASPECTS)[number], band: Band): CellBeliefOut {
  return {
    cell: `${conceptId}/${aspect}`, concept_id: conceptId, aspect, band, m: 0.5, u: 0.2, recall: 0, direct: 0,
    inferred_pos: 0, inferred_neg: 0, inferred_only: false, declared: null,
  };
}

export function concept(id: string, name: string, bands: Band[], prereqs: string[] = [], onPage = false): KConceptOut {
  const aspects = ASPECTS.slice(0, bands.length);
  return {
    id, name, role: null, aspects: [...aspects], prereqs, on_current_page: onPage,
    cells: aspects.map((a, i) => cell(id, a, bands[i]!)), notes: [],
  };
}

export function protoK(): KGridOut {
  return {
    concepts: ROWS.map(([id, name, , band, known, pre]) => {
      const rest: Band = band === "known" || band === "likely" ? "none" : band;
      const bands = ASPECTS.map((_, i): Band => (i < known ? (band === "known" ? "known" : "likely") : rest));
      return concept(id, name, bands, pre, id === "pca");
    }),
  };
}

export function protoCatalog(): CatalogOut {
  return {
    learning: [], shelf: { pages: [] },
    settings: { review_per_day: 5, page_limit_usd: null, language: "zh" },
    current_page: null, names: {}, paths: [], maps: [],
  };
}

export function protoGoals(): GoalMembership {
  const ids = (bio: boolean) => ROWS.filter((r) => (r[2] === "生物") === bio).map((r) => r[0]);
  return {
    goals: [{ id: "g-pca", name: "PCA" }, { id: "g-gene", name: "基因配对" }],
    membership: { "g-pca": ids(false), "g-gene": ids(true) },
  };
}
