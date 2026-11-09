/**
 * The 今天 tab body (app.js `todayView`): `.ew-side-body` (full height, its own scroller) holding, in order,
 * 待办事项 (render-next.ts), 想学点新的？ (render-learn.ts) and 知识图谱 (user review: no 需要你看的 card; RSI changes
 * are in 参数, render-engine.ts). 知识图谱: `.ew-blk` > `.ew-row` (space-between) > h2, then the small cloud's persistent `.ew-mini` (and its failure
 * line). The demo line is the first line of the body; offline is the sidebar header's one banner (wiki-renderer.ts).
 */
import { el } from "../ui/ew";
import type { RenderCtx, WikiActions } from "./actions";
import { WIKI_COPY } from "./copy";
import { renderLearn } from "./render-learn";
import { renderNext } from "./render-next";
import type { WikiState } from "./state";
import { TODAY_COPY } from "./today-copy";

/** app.js todayView / engineView: `.ew-side-body` with height 100% and border-box sizing. */
export function sideBody(cls: string): HTMLDivElement {
  const b = el("div", cls);
  b.style.height = "100%";
  b.style.boxSizing = "border-box";
  return b;
}

/** The demo line (one `.ew-muted` line), or nothing. Offline is the sidebar's own banner (offlineBanner), not a line here. */
export function statusLines(s: WikiState): HTMLDivElement[] {
  if (!s.demo) return [];
  const d = el("div", "muted", () => (WIKI_COPY.demoBanner));
  d.dataset.role = "demo";
  return [d];
}

function mapBlock(ctx: RenderCtx): HTMLDivElement {
  const mm = el("div", "blk");
  mm.dataset.section = "map";
  const hd = el("div", "row");
  hd.style.justifyContent = "space-between";
  const h2 = el("h2", "", () => (TODAY_COPY.mapTitle));
  h2.setAttribute("style", "font-size:15px;margin:0");
  hd.append(h2);
  mm.append(hd);
  if (ctx.cloud) mm.append(ctx.cloud.root);
  return mm;
}

export function renderToday(s: WikiState, a: WikiActions, ctx: RenderCtx): HTMLDivElement {
  const b = sideBody("side-body");
  b.dataset.tab = "today";
  // User review: no 需要你看的 card here; RSI changes live only in 参数 (render-engine.ts).
  b.append(...statusLines(s), renderNext(s, a, ctx), renderLearn(s, a), mapBlock(ctx));
  // app.js todayView: `b.append(mm); b.append(goalsBlock())` - the goal rows are a sibling after the map block.
  if (ctx.cloud?.goals) b.append(ctx.cloud.goals.root, ctx.cloud.goals.line);
  return b;
}
