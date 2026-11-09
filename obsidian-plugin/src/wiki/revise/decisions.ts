/**
 * An in-page draft (the assistant's practice draft, a materials draft) is ONE decision shown in two places: the page's
 * draft bar and the chat's card (full-demo app.js draftDecide / draftBtns). Both read and write the decision here, so a
 * click in either resolves both at once: while it is out both places wait; when it went through both leave their
 * buttons (`settled` carries the ledger row, so the other place need not wait for its next ledger read); when the request
 * did not go through both show only a quiet 请重试 that sends the same decision again.
 */
import { ApiError } from "../../api/errors";
import type { ActionOut } from "../bot/bot-schema";

/** 确认替换 / 取消, or 重试 (redraft: the draft written again for the page as it is now). */
export type DraftOp = "approve" | "reject" | "redraft";
export interface DraftDecision {
  /** A decision is being sent (from either place). */
  busy: DraftOp | null;
  /** The last decision did not go through (a transport failure): 请重试 sends this again. */
  failed: DraftOp | null;
  /** It went through: the ledger row the server answered. */
  settled: ActionOut | null;
}

/**
 * A decision that did not reach the server or failed there (offline, a network error, a 5xx): the same click may work
 * now, so the place shows only 请重试. A 4xx is the server refusing it (gone, decided elsewhere, stale): its reason shows.
 */
export function retryable(e: unknown): boolean {
  return !(e instanceof ApiError) || e.status >= 500 || e.status === 0;
}

const IDLE: DraftDecision = Object.freeze({ busy: null, failed: null, settled: null });

export class DraftDecisions {
  private readonly m = new Map<string, DraftDecision>();
  private readonly subs = new Set<(actionId: string) => void>();

  get(actionId: string): DraftDecision { return this.m.get(actionId) ?? IDLE; }

  /** Claims the decision for `op`; false while one is already out or it already went through (the click does nothing). */
  start(actionId: string, op: DraftOp): boolean {
    const d = this.get(actionId);
    if (d.busy !== null || d.settled !== null) return false;
    this.put(actionId, { busy: op, failed: null, settled: null });
    return true;
  }
  /** Went through: `row` is the server's answer. */
  settle(actionId: string, row: ActionOut): void { this.put(actionId, { busy: null, failed: null, settled: row }); }
  /** Did not go through (offline, the server unreachable or erring): both places offer 请重试. */
  fail(actionId: string, op: DraftOp): void { this.put(actionId, { busy: null, failed: op, settled: null }); }
  /** Refused with a reason the place that sent it shows: back to the plain buttons. */
  clear(actionId: string): void {
    if (!this.m.has(actionId)) return;
    this.m.delete(actionId);
    this.emit(actionId);
  }

  subscribe(fn: (actionId: string) => void): () => void {
    this.subs.add(fn);
    return () => { this.subs.delete(fn); };
  }

  private put(actionId: string, d: DraftDecision): void {
    this.m.set(actionId, d);
    this.emit(actionId);
  }
  private emit(actionId: string): void {
    for (const fn of [...this.subs]) {
      try { fn(actionId); } catch (e) { console.warn("EvoWiki: a draft decision listener failed", e); }
    }
  }
}
