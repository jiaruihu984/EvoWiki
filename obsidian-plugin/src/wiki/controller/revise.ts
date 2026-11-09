/**
 * Materials drafts (a page revised from 资料, shown as track changes in the page): following their `source_revise`
 * runs, 确认替换 / 取消 / 撤销 through the ledger, the note brought to the page afterwards, and 在页面里看.
 */
import { WIKI_COPY } from "../copy";
import { assetIdsIn, evouiAssetIdsIn, imageAssetIdsIn } from "../embed/assets";
import { decideErrorText } from "../format";
import { REVISE_COPY } from "../revise/copy";
import { retryable } from "../revise/decisions";
import { inPageDraft, REVISE_KIND, pendingRevise } from "../revise/rows";
import type { EditedSync } from "./helpers";
import type { WikiCore } from "./core";
import type { Notes } from "./notes";

/** The Bot's ledger and the page opener (the facade wires them). */
export interface ReviseHooks {
  ledgerChanged(): Promise<void>;
  openWikiPage(pageId: string): Promise<void>;
  jumpToSection(sid: string): void;
}

export class Revise {
  constructor(private readonly core: WikiCore, private readonly notes: Notes, private readonly h: ReviseHooks) {}

  /** Pages whose `source_revise` run (a materials draft being written) was live in the last writes snapshot. */
  private revising = new Set<string>();
  /** Drafts still `writing` in the ledger whose run the snapshot no longer shows: the ledger was read again for them once. */
  private readonly reviseRead = new Set<string>();
  /**
   * A materials draft is written by a `source_revise` run (the writes watcher sees it; it writes nothing to the note). When
   * one ends, or a row the ledger still says is being written has no live run, the ledger is read again: the row then holds
   * its draft (the page goes into review mode) or why it failed.
   */
  followReviseRuns(): void {
    if (this.core.disposed) return;
    const writes = this.core.watcher?.snapshot()?.writes ?? [];
    const now = new Set(writes.filter((w) => w.kind === "source_revise" && (w.state === "queued" || w.state === "planning" || w.state === "writing")).map((w) => w.page_id));
    let read = [...this.revising].some((id) => !now.has(id));
    this.revising = now;
    const a = this.core.state.bot.actions;
    for (const row of a.kind === "ready" ? a.value : []) {
      if (row.kind !== REVISE_KIND || row.status !== "proposed" || row.revise?.state !== "writing") continue;
      if (now.has(row.revise.page_id) || this.reviseRead.has(row.action_id)) continue;
      this.reviseRead.add(row.action_id);
      read = true;
    }
    if (read) void this.h.ledgerChanged();
  }

  /** Draft asset ids already asked for (each once per session). */
  private readonly draftAssets = new Set<string>();
  /**
   * A waiting in-page draft that places a library component (the assistant's practice draft) shows it from its asset,
   * stored on the server when it was drafted: fetch it into the vault now, so the page draws it at once instead of a
   * 「取回」 placeholder. Cheap and idempotent (called on every state change).
   */
  fetchDraftAssets(): void {
    const assets = this.core.deps.assets;
    const a = this.core.state.bot.actions;
    if (this.core.disposed || assets === undefined || a.kind !== "ready") return;
    const pending = { html: [] as string[], evoui: [] as string[], image: [] as string[] };
    for (const row of a.value) {
      if (!inPageDraft(row.kind) || row.status !== "proposed" || row.revise?.state !== "ready") continue;
      for (const s of row.revise.sections) {
        for (const [kind, ids] of [["html", assetIdsIn(s.new_body)], ["evoui", evouiAssetIdsIn(s.new_body)], ["image", imageAssetIdsIn(s.new_body)]] as const) {
          for (const id of ids) if (!this.draftAssets.has(id) && !pending[kind].includes(id)) pending[kind].push(id);
        }
      }
    }
    for (const kind of ["html", "evoui", "image"] as const) {
      const ids = pending[kind];
      if (ids.length === 0) continue;
      for (const id of ids) this.draftAssets.add(id);
      void assets.retrieveAll(ids, kind).catch((e: unknown) => { console.warn("EvoWiki wiki: a draft's asset was not fetched", e); });
    }
  }

  /**
   * The in-page draft row `id` (a materials draft, or the assistant's practice draft) as the ledger was last read (null:
   * another kind, or not read). Its decisions go through `reviseDecide`, so the note follows the page after 确认替换 / 撤销.
   */
  reviseRow(id: string): { pageId: string | null } | null {
    const a = this.core.state.bot.actions;
    const row = a.kind === "ready" ? a.value.find((x) => x.action_id === id) : undefined;
    return row === undefined || !inPageDraft(row.kind) ? null : { pageId: row.revise?.page_id || row.page_id };
  }
  private reviseBusy = new Set<string>();

  /**
   * A materials draft (UI principles 2026-10-21 §三): 确认替换 (approve, no draft body) / 取消 (reject) / 撤销 (undo) through
   * the ledger, then the ledger is read again (the overlay, the bar, the top line and the cards follow it). After approve and
   * undo the note is brought to the page as the server now has it (pageEditedElsewhere: the same sync as any page save);
   * before 确认替换 nothing is ever written to the note. A failure says why in a notice and changes nothing.
   */
  async reviseDecide(actionId: string, op: "approve" | "reject" | "undo"): Promise<void> {
    if (this.core.disposed || this.reviseBusy.has(actionId)) return;
    if (!this.core.deps.configured()) { this.core.deps.notice?.(WIKI_COPY.needsSettings); return; }
    // 确认替换 / 取消 are one decision with the chat's card (revise/decisions.ts): claimed there, its outcome shown in both
    const dop = op === "undo" ? null : op;
    const shared = dop === null ? undefined : this.core.deps.decisions;
    if (shared !== undefined && dop !== null) {
      if (!shared.start(actionId, dop)) return;
      // offline: nothing is sent; both places offer 请重试 (no notice: the quiet link is the whole message)
      if (this.core.state.conn === "offline") { shared.fail(actionId, dop); return; }
    } else if (this.core.state.conn === "offline") { this.core.deps.notice?.(WIKI_COPY.offline); return; }
    const pageId = this.reviseRow(actionId)?.pageId ?? null;
    this.reviseBusy.add(actionId);
    // 参数's 撤销 buttons are drawn off while a decision is out (blockedFor: busy); a write already running keeps its own busy
    const own = this.core.state.busy === null;
    if (own) this.core.set({ busy: { what: "rsi", sid: actionId, abortable: false } });
    let done = false;
    try {
      const out = await this.core.call(() => this.core.deps.api.botDecide(actionId, op));
      done = true;
      if (shared !== undefined) shared.settle(actionId, out);
      if (op === "undo") this.core.deps.notice?.(REVISE_COPY.undone);
    } catch (e) {
      if (shared !== undefined && dop !== null && retryable(e)) shared.fail(actionId, dop);
      else {
        if (shared !== undefined) shared.clear(actionId);
        this.core.deps.notice?.(decideErrorText(e));
      }
    } finally {
      this.reviseBusy.delete(actionId);
      if (own && this.core.state.busy?.what === "rsi" && this.core.state.busy.sid === actionId) this.core.set({ busy: null });
    }
    await this.h.ledgerChanged();
    if (done && op !== "reject" && pageId !== null && !this.core.disposed) await this.reviseNote(pageId);
  }

  /**
   * 重试 on a draft that no longer fits its page (its section gone or renamed; a materials draft whose page changed): the
   * server writes it again for the same request against the page as it is now (a practice: the same row, a new draft; a
   * materials draft: a new row, being written), then the ledger is read again and the page shows the new draft. One
   * decision with the chat's card (revise/decisions.ts): both wait while it is out; not through → 请重试 in both; a
   * refusal (no usable draft came back) says why in a notice.
   */
  async reviseRedraft(actionId: string): Promise<void> {
    const api = this.core.deps.api;
    if (this.core.disposed || this.reviseBusy.has(actionId)) return;
    if (!this.core.deps.configured()) { this.core.deps.notice?.(WIKI_COPY.needsSettings); return; }
    const shared = this.core.deps.decisions;
    if (shared !== undefined && !shared.start(actionId, "redraft")) return;
    if (this.core.state.conn === "offline" || api.botRedraft === undefined) {
      if (shared !== undefined && api.botRedraft !== undefined) shared.fail(actionId, "redraft");
      else this.core.deps.notice?.(WIKI_COPY.offline);
      return;
    }
    this.reviseBusy.add(actionId);
    try {
      await this.core.call(() => api.botRedraft!(actionId));
      shared?.clear(actionId);
    } catch (e) {
      if (shared !== undefined && retryable(e)) shared.fail(actionId, "redraft");
      else { shared?.clear(actionId); this.core.deps.notice?.(decideErrorText(e)); }
    } finally {
      this.reviseBusy.delete(actionId);
    }
    await this.h.ledgerChanged();
  }

  /**
   * After 确认替换 / 撤销 the note must show the page the server now has. Fail closed: when that read / write fails, the
   * page waits in 「待同步」 (the next time it lands it is written) and a notice says so, with 重试.
   */
  private async reviseNote(pageId: string): Promise<void> {
    let out: EditedSync = "failed";
    try { out = await this.notes.syncEdited(pageId); } catch (e) { console.warn("EvoWiki wiki: the revised page's note was not written", e); }
    // deferred: another write started while the page was read; the page is in 待同步 and is written when it next lands
    if (out !== "failed" || this.core.disposed) return;
    this.notes.pendingSync.add(pageId);
    const retry = { label: REVISE_COPY.retry, run: () => { void this.reviseNote(pageId); } };
    if (this.core.deps.noticeActions) this.core.deps.noticeActions(REVISE_COPY.noteFailed, [retry]); else this.core.deps.notice?.(REVISE_COPY.noteFailed);
  }

  /**
   * 在页面里看 (参数) / the chat's 草稿在页面里 card: open the page's note at the first section the draft marks (the note
   * opens where any page open puts it when no section is known).
   */
  async openRevise(pageId: string): Promise<void> {
    if (this.core.disposed) return;
    await this.h.openWikiPage(pageId);
    const p = this.core.state.page;
    if (p.kind !== "open" || p.page.page_id !== pageId || pendingRevise(this.core.state, pageId) === null) return;
    const row = pendingRevise(this.core.state, pageId)!;
    const sid = row.revise.sections.find((x) => x.status !== "added" && p.page.sections.some((y) => y.id === x.section_id))?.section_id;
    if (sid !== undefined) this.h.jumpToSection(sid);
  }
}
