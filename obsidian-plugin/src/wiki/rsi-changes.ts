/**
 * 个性化 · 活动记录's changes (GET /wiki/rsi/changes): what is shown, and its plain words. Pure logic; render-personal.ts draws
 * them, dots.ts and the controller read through it.
 *
 * RSI changes apply without the user's confirmation once their gate passes (Global Constraint 3): nothing waits for a
 * yes / no any more. Shown: every change that took effect, of every layer and track: 已生效 (personal_accepted) and
 * 已撤销 (rolled_back: by the user, or automatically when it made things worse), plus track B's `rejected` (没有采用:
 * records from before changes applied by themselves). Never shown: `candidate` / `gate_failed` (the system's own
 * attempts that never took effect: listing them would bury the changes that did), A's `rejected` (gate attempts used
 * up) and `awaiting_user` (records from the old confirm flow: nothing can decide them now), except a code change the agent
 * wrote (isCodePatch): it waits for the learner's review (等你审阅: 采用 / 不要) and, once discarded, shows as 已取消.
 * A row shows the backend's plain `summary` and `why` as they are (second line), and, for a ledger rule change (track G), how many
 * pages it marked out of date still wait for their rewrite (or failed); no date or effect line.
 */
import { ApiError, OfflineError } from "../api/errors";
import { WIKI_COPY } from "./copy";
import { ENGINE_COPY } from "./engine-copy";
import { errorText } from "./format";
import type { PatchStatus, RsiChangeOut } from "./schema";

const APPLIED: readonly PatchStatus[] = ["personal_accepted", "rolled_back"];

/**
 * A code change the agent wrote (track H, id `forgep:<id>`): built, it waits for the learner's review (`awaiting_user`:
 * 采用 / 不要); once discarded it is `rejected`. The only rows that are still decided here (not in the Bot's ledger).
 */
export const isCodePatch = (c: Pick<RsiChangeOut, "track" | "patch_id">): boolean => c.track === "H" && c.patch_id.startsWith("forgep:");

/** The plugin's own name for a shown status (also the row's data attribute: never the raw status). */
export type RsiState = keyof typeof WIKI_COPY.rsiStatus;
const STATE: Partial<Record<PatchStatus, RsiState>> = {
  personal_accepted: "live", rejected: "declined", rolled_back: "undone", awaiting_user: "review",
};

/** What a shown change says, in the plugin's words. */
export interface RsiCard {
  change: RsiChangeOut;
  state: RsiState;
  /** The backend's plain sentence, as is. */
  summary: string;
  /** The backend's plain reason, as is; null when empty. */
  why: string | null;
  /** 撤销: only for a change in effect that the backend says can be undone. */
  undoable: boolean;
  /** A ledger rule change's (track G: the assistant's 讲解规则) pages still out of date / whose rewrite stopped retrying; null when none. */
  waiting: string | null;
  failed: string | null;
}

export function isShown(c: RsiChangeOut): boolean {
  if (isCodePatch(c) && (c.status === "awaiting_user" || c.status === "rejected")) return true;
  return APPLIED.includes(c.status) || (c.status === "rejected" && c.track === "B" && c.layer === 0);
}

/** The shown changes, in the backend's order (newest first). */
export function visibleChanges(list: readonly RsiChangeOut[]): RsiChangeOut[] {
  return list.filter(isShown);
}

/** ms since the epoch of a change's `at`; null when it does not parse. */
export function changeTime(c: RsiChangeOut): number | null {
  const ms = Date.parse(c.at);
  return Number.isNaN(ms) ? null : ms;
}

/**
 * The shown changes newer than `since` (ms: the newest `at` 个性化 showed when it was last opened; null = never opened:
 * every shown change is new), newest first. A change whose `at` does not parse is never new (no dot from it).
 */
export function newChanges(list: readonly RsiChangeOut[], since: number | null): RsiChangeOut[] {
  return visibleChanges(list).filter((c) => { const t = changeTime(c); return t !== null && (since === null || t > since); });
}

/** The newest `at` among the shown changes (what 个性化 has shown once it is opened), or null when none parses. */
export function newestTime(list: readonly RsiChangeOut[]): number | null {
  let top: number | null = null;
  for (const c of visibleChanges(list)) { const t = changeTime(c); if (t !== null && (top === null || t > top)) top = t; }
  return top;
}

/** The newest change in effect the user has not seen yet (newer than `since`, as newChanges), or null. */
export function recentChange(list: readonly RsiChangeOut[], since: number | null): RsiChangeOut | null {
  return newChanges(list, since).find((c) => c.status === "personal_accepted") ?? null;
}

const present = (s: string): string | null => (s.trim() === "" ? null : s);

/** A shown change as a card; null for one that is not shown. */
export function rsiCard(c: RsiChangeOut): RsiCard | null {
  const state = isShown(c) ? STATE[c.status] : undefined;
  if (state === undefined) return null;
  return {
    change: c, state, summary: c.summary.trim(), why: present(c.why.trim()), undoable: state === "live" && c.can_undo === true,
    waiting: c.track === "G" && c.pages_waiting > 0 ? WIKI_COPY.rsiPagesWaiting(c.pages_waiting) : null,
    failed: c.track === "G" && c.pages_failed > 0 ? WIKI_COPY.rsiPagesFailed(c.pages_failed) : null,
  };
}

/**
 * The line for a failed 撤销: 409 (busy cycle, not undoable) → one sentence; 404 → gone; 5xx (the real undo route may
 * answer 500 on an uncaught decision error) → a plain retry-later sentence; offline / the usage limit / anything else
 * as errorText. Never the server's detail, except the 409 of an `act_` (ledger) row: its detail is a plain sentence by
 * contract (e.g. 「…的写法后来又改过了，先撤销后来的那次」), shown instead of the busy line; likewise a plain Chinese 409
 * of a `forgep:` / `forge:` / `h_` row (e.g. 「这次改动之后，同一个设置又被改过了，要先撤销后面那次」).
 */
export function rsiErrorText(e: unknown, patchId?: string): string {
  if (e instanceof OfflineError) return WIKI_COPY.offline;
  if (e instanceof ApiError) {
    if (e.status === 409) {
      const d = e.detail.trim();
      // a round in progress (the server's BUSY_DETAIL), whatever the row
      if (e.code === "rsi_busy") return WIKI_COPY.rsiBusy;
      // a code change under review: 采用 refused while code changes are switched off (code patches_off); else the backend's plain sentence
      if (e.code === "patches_off") return WIKI_COPY.rsiPatchesOff;
      // a code patch, a harness setting change (h_) or a forged tool (forge:): the backend's plain Chinese reason
      if (patchId !== undefined && /^(forgep:|forge:|h_)/.test(patchId)) {
        return e.code !== null && d !== "" && !/^[[{]/.test(d) ? d : WIKI_COPY.rsiBusy;
      }
      return patchId?.startsWith("act_") === true && d !== "" && !/^[[{]/.test(d) ? d : WIKI_COPY.rsiBusy;
    }
    if (e.status === 404) return WIKI_COPY.rsiGone;
    if (e.status >= 500) return WIKI_COPY.rsiFailed;
  }
  return errorText(e);
}

/**
 * What an undo restored, by what the change was (app.js `undoneText`): a page edit or a new page is back to before; a teaching
 * rule says its pages update when opened; a path is restored; anything else just says 已撤销.
 */
export function undoneNotice(c: RsiChangeOut | undefined): string {
  if (c === undefined) return ENGINE_COPY.undoneGeneric;
  if (c.track === "G") {
    if (c.kind === "page_body" || c.kind === "new_page") return ENGINE_COPY.undoneRestored;
    if (c.kind === "teaching_params") return ENGINE_COPY.undoneRule;
    if (c.kind === "path" || c.kind === "goals") return ENGINE_COPY.undonePath;
    return ENGINE_COPY.undoneGeneric;
  }
  return c.track === "B" && c.layer !== 2 ? ENGINE_COPY.undoneRule : ENGINE_COPY.undoneGeneric;
}

/** A change taken back by the system itself (it made things worse), not by the learner. */
export const autoUndone = (c: RsiChangeOut): boolean => c.status === "rolled_back" && c.outcome === "harmful";
