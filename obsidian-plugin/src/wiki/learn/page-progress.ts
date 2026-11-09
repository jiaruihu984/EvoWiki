/**
 * A block's page cards (user request 2026-10-24): each card's title background is its page's REAL writing progress, as the
 * server says it on the path row (wikiapp/progress.py: `state`, and `progress` {section, of} while writing): planned /
 * failed = 0%, writing = section / of, written / read / mastered = 100%. A page the wiki controller streams here (生成第一页:
 * the organize stream's outline, then each section_done) fills from that stream instead (`streamProgress`). Pure;
 * learn-view.ts reads the path again while a row is being written and paints.
 */
import type { PageRow } from "./learn-flow";

/** The fill of a row's card title, 0–100, from its server state. */
export function rowProgress(r: Pick<PageRow, "state" | "progress">): number {
  switch (r.state) {
    case "written": case "read": case "mastered": return 100;
    case "writing": {
      const p = r.progress;
      return p !== null && p.of !== null && p.of > 0 ? Math.round(100 * Math.min(p.section, p.of) / p.of) : 0;
    }
    default: return 0;            // planned, failed, trashed
  }
}

/** A row still moving: its page is being written, or was just made and not started yet (the path is read again). */
export const rowPending = (r: Pick<PageRow, "state" | "pageId">): boolean => r.state === "writing" || (r.pageId !== null && r.state === "planned");

/** While a block's pages are written, the path is read again this often (ms). */
export const PAGE_PROGRESS_POLL_MS = 1100;

/** A page's organize stream in the wiki controller (WikiCore.writeProgress): `done` of `of` sections, `ended` / `ok`. */
export interface StreamProgress { pageId: string; done: number; of: number; ended: boolean; ok: boolean }

/** The fill from the stream: done / of while it runs (0% before its outline), 100% written; a failed or stopped one 0%. */
export function streamProgress(p: StreamProgress): number {
  if (p.ended) return p.ok ? 100 : 0;
  return p.of > 0 ? Math.round(100 * Math.min(p.done, p.of) / p.of) : 0;
}
