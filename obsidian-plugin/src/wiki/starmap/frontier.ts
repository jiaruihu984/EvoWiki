import { tr } from "../../i18n/index";
import { byCodePoint, isGood, type StarMap } from "./model";

export interface Recommendation { conceptId: string; name: string; reason: string; pageId: string | null }

/**
 * Q2: the only recommendation source. A concept qualifies when it has at least one prerequisite, all of them known /
 * likely, and it is not known / likely itself. Ranked by direct followers it unlocks, then name.
 */
export function frontier(map: StarMap, opts?: { max?: number }): Recommendation[] {
  const max = opts?.max ?? 3;
  const unlocks = new Map<string, number>();
  for (const e of map.edges) unlocks.set(e.from, (unlocks.get(e.from) ?? 0) + 1);
  const hits = map.nodes.filter((n) => !isGood(n.band) && n.prereqs.length > 0
    && n.prereqs.every((p) => { const x = map.byId.get(p); return x !== undefined && isGood(x.band); }));
  hits.sort((a, b) => (unlocks.get(b.id) ?? 0) - (unlocks.get(a.id) ?? 0) || byCodePoint(a.name, b.name));
  return hits.slice(0, Math.max(0, max)).map((n) => {
    const names = n.prereqs.map((p) => map.byId.get(p)!.name);
    return {
      conceptId: n.id, name: n.name, pageId: n.pageId,
      reason: tr("wiki.starmap.frontier.reason.1", { v0: names.slice(0, 2).map((name) => `「${name}」`).join(tr("wiki.starmap.frontier.reason.2")), v1: names.length > 2 ? tr("wiki.starmap.frontier.reason.3") : "" }),
    };
  });
}
