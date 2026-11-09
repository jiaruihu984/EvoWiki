/** The left panel's decisions (graph.html render / nodeCard / pathCard), pure. */
import { SHARE_BANDS, type GoalOut, type GoalSourceOut, type PathEditIn, type PathItemOut, type ShareBand } from "../schema";
import type { Scene, SceneNode } from "./scene";
import { GRAPH_COPY } from "./copy";

/** A goal's share bar: one segment per band with any share, its flex the share (graph.html: counts; same lengths). */
export function shareSegments(shares: GoalOut["shares"]): { band: ShareBand; flex: number }[] {
  return SHARE_BANDS.filter((b) => (shares[b] ?? 0) > 0).map((band) => ({ band, flex: shares[band] ?? 0 }));
}

/**
 * The goal the node card's actions go to: the selected goal, else the first one (graph.html: PATH = PATHS[FOCUS || "ml"],
 * its first goal).
 */
export function actionGoal(goals: readonly GoalOut[], focus: string | null): GoalOut | null {
  return goals.find((g) => g.map_id === focus) ?? goals[0] ?? null;
}

export interface CardButtons { want: boolean }
/**
 * graph.html nodeCard (spec 2026-10-21: the card only tells): 我想学这个 for a node that is not a split child, unless a page
 * of the goal's path already covers it (and only when there is a goal to add it to). No 看不懂, no 我会了，跳过.
 */
export function cardButtons(n: SceneNode, goal: GoalOut | null): CardButtons {
  const inPath = goal !== null && goal.path.some((p) => p.node_ids.includes(n.id));
  return { want: !n.child && goal !== null && goal.path_id !== null && !inPath };
}

/** 拆成多页 is offered for a row with a node, until the node's page was opened; a one-node row's node must be splittable. */
export function canSplitRow(row: PathItemOut, nodeCanSplit: boolean): boolean {
  if (row.node_ids.length === 0 || row.opened === true) return false;
  return row.node_ids.length > 1 || nodeCanSplit;
}

/** graph.html nodeCard 属于: every parent's name. */
export const parentNames = (S: Scene, n: SceneNode): string[] => n.parents.map((p) => S.names.get(p) ?? p);

/** graph.html movePage: the path with page `from` moved to `to` (a copy). */
export function movedPath(path: readonly PathItemOut[], from: number, to: number): PathItemOut[] {
  const P = [...path];
  const [pg] = P.splice(from, 1);
  if (pg !== undefined) P.splice(to, 0, pg);
  return P;
}

/** What the page shows at once for an edit (the server's answer replaces it). `add` waits for the server. */
export function optimisticPath(path: readonly PathItemOut[], edit: PathEditIn): PathItemOut[] | null {
  if (edit.op === "move") return movedPath(path, edit.from, edit.to);
  if (edit.op === "remove") return path.filter((_, i) => i !== edit.index);
  if (edit.op === "retitle") return path.map((p) => (p.key === edit.page_key ? { ...p, title: edit.title } : p));
  return null;
}

/**
 * The bottom line's pieces (graph.html COVER): each material's uncovered section titles, or its name once deleted. A goal
 * row (GoalOut) or a path without a map (PathOut, 学点新的) - both carry `sources`.
 */
export function coverParts(goal: { sources?: readonly GoalSourceOut[] }): string[] {
  return (goal.sources ?? []).flatMap((s) => (s.deleted ? [GRAPH_COPY.deletedTitle(s.title)] : s.uncovered));
}

/** The whole bottom line, or "" when every section has a page: 「资料里这几节没讲：前言、附录 B」. */
export function coverLine(goal: { sources?: readonly GoalSourceOut[] }): string {
  const parts = coverParts(goal);
  return parts.length ? GRAPH_COPY.uncoveredLead + parts.join("、") : "";
}

/** A path row's § labels, each once, leaving out a label its title already says (a 读懂 row is 「《论文》§摘要」). */
export function rowSections(title: string, sections: readonly string[] | undefined): string[] {
  return [...new Set(sections ?? [])].filter((x) => !title.includes(x));
}
