/** View types the plugin registered before and no longer does: the concept-era sidebar and graph (retired 2026-10-21). */
export const REMOVED_VIEW_TYPES: readonly string[] = ["evowiki-sidebar", "evowiki-graph"];

/**
 * Detaches every leaf of a removed view type, so a saved workspace layout does not restore it as an empty
 * 「插件视图缺失」 panel. Run once the layout is ready; returns how many leaves were closed.
 */
export function detachRemovedViews(ws: { getLeavesOfType(type: string): { detach(): void }[] }): number {
  let n = 0;
  for (const type of REMOVED_VIEW_TYPES) {
    for (const leaf of ws.getLeavesOfType(type)) { leaf.detach(); n++; }
  }
  return n;
}
