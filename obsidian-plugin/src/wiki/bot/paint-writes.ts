import { tr } from "../../i18n/index";
/** Live page-write status in 参数 · 最近做了什么, painted in place (no redraw) as app.js `paintWrites` does. */
import { isWriting, writeLine } from "../../assistant/card-state";
import type { WriteOut } from "../schema";

export const costText = (c: number): string => (c > 0 ? "$" + c.toFixed(3) : "$0");

/** The status line of a running write: the `<i>` breathing dot (only while it pulses) and the text (P:697-711 `wStatus`). */
export function fillWline(e: Element, w: WriteOut | undefined): void {
  const line = writeLine(w) ?? { text: tr("wiki.bot.paint-writes.line.text.1"), pulse: true, key: "" };
  e.replaceChildren();
  if (line.pulse) e.append(document.createElement("i"));
  e.append(line.text);
}

/** P:710-711 `paintWrites`: every running write's status line and cost under `root`, in place (no redraw). */
export function paintRunWrites(root: ParentNode, writes: ReadonlyMap<string, WriteOut>): void {
  for (const e of root.querySelectorAll<HTMLElement>(".ew-wline[data-wrun]")) {
    const w = writes.get(e.dataset.wrun ?? "");
    const key = writeLine(w)?.key ?? "";
    if (e.dataset.state === key) continue;
    e.dataset.state = key;
    fillWline(e, w);
  }
  for (const e of root.querySelectorAll<HTMLElement>(".ew-cost[data-wcost]")) {
    const w = writes.get(e.dataset.wcost ?? "");
    if (w !== undefined && isWriting(w) && w.cost_usd !== undefined) e.textContent = costText(w.cost_usd);
  }
}
