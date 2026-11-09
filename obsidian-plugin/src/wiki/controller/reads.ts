/**
 * The GETs (reads, no LLM) and the P5 refresh after a write: page, catalog, K, path, review list, changes list
 * and the path plans. `onFail` null = a plain load (an error replaces the area); otherwise a refresh after a write
 * (core.refreshFail adds its line under the write's result).
 */
import { WIKI_COPY } from "../copy";
import { errorText } from "../format";
import type { PageOut } from "../schema";
import { OfflineError } from "../../api/errors";
import { type Target, type OnFail, type Ctx, isAbort, bad } from "./helpers";

/** A refresh after a write that failed (not offline: the reconnect re-reads those) is tried once more after this long. */
export const REFRESH_RETRY_MS = 4000;
import type { WikiCore } from "./core";
import type { Notes } from "./notes";
import { setUiLanguage } from "../../i18n";

export class Reads {
  constructor(private readonly core: WikiCore, private readonly notes: Notes) {}

  /** Review list GETs: only the latest one's answer lands (an older one is dropped). */
  reviewSeq = 0;
  catalogSeq = 0;
  private progressSeq = 0;
  private readonly planKeys = new Map<string, string>();
  /**
   * Bumped by every path write (下一页 / 学完) when it starts: a GET path sent before it may be older than it and is
   * dropped (unrelated writes do not drop it).
   */
  pathEpoch = 0;

  /** GET the plan of every path that has a next page (the newest 5; reads, no LLM); a failed one is skipped (no page title then). */
  async loadPathPlans(): Promise<void> {
    const c = this.core.state.catalog;
    if (this.core.disposed || c.kind !== "ready" || !Array.isArray(c.value.paths) || !this.core.deps.configured()) return;
    // A plan is read again only when its card changed (pages done, next page, size); a cached plan is reused otherwise.
    const key = (p: { done: number; next_index: number | null; pages: number }): string => `${p.done}/${p.next_index}/${p.pages}`;
    // the catalog lists paths oldest first: the newest goals are the ones being learned
    const todo = c.value.paths.filter((p) => p.next_index !== null).slice(-5)
      .filter((p) => this.planKeys.get(p.path_id) !== key(p) || this.core.state.pathPlans[p.path_id] === undefined);
    const plans = await Promise.all(todo.map((p) => this.core.call(() => this.core.deps.api.getPath(p.path_id))
      .then((plan) => { this.planKeys.set(p.path_id, key(p)); return plan; }, () => null)));
    if (this.core.disposed) return;
    const got = Object.fromEntries(plans.flatMap((p) => (p === null ? [] : [[p.path_id, p] as const])));
    if (Object.keys(got).length > 0) this.core.set({ pathPlans: { ...this.core.state.pathPlans, ...got } });
    // A page in the trash has no file: the row is only a record (Task 14 offers 重新写); the file goes to Obsidian's trash.
    for (const plan of Object.values(got)) {
      for (const row of plan.pages) if (row.trashed === true && row.page_id !== null) this.notes.trashFileOf(row.page_id);
    }
  }

  /**
   * P5 after a write that may change K: GET page (still the same page; ctx null = none open), GET catalog, GET K if
   * loaded (a failed GET adds its line under the write's result at `target`); then sync the page when the write
   * changed it (A3-2) or the GET says so (A3-3) / it waits in 「待同步」 (A3-4).
   */
  async refresh(ctx: Ctx | null, changed: boolean, target: Target, retry = true): Promise<void> {
    if (this.core.disposed) return;
    this.core.watcher?.kick();   // a path edit, 看不懂 or a page action may have queued a write
    const s = this.core.state.page;
    const same = ctx !== null && ctx.token === this.core.pageToken && s.kind === "open" && s.page.page_id === ctx.pageId;
    // The first refresh shows its failure under the write's result; the background retry is quiet and, when every GET
    // answers, takes that line away again (the shown state is current now).
    let failed: unknown = null;
    const shown = retry ? this.core.refreshFail(target) : null;
    const fail = (e: unknown): void => { failed ??= e; shown?.(e); };
    const page = ctx !== null && same ? await this.getAndLand(ctx.pageId, ctx.token, false, fail) : null;
    await this.loadCatalog(fail);
    if (this.core.state.k.kind !== "idle") await this.loadK(fail);
    if (retry) this.core.set({ busy: null });
    if (page !== null) await this.notes.syncLanded(page, changed || page.diff !== null || page.llm_calls > 0);
    if (!retry) { if (failed === null) this.core.clearRefreshSub(target); return; }
    if (failed !== null && !(failed instanceof OfflineError)) {
      void this.core.sleep(REFRESH_RETRY_MS).then(() => this.core.disposed ? undefined : this.refresh(ctx, changed, target, false));
    }
  }

  /**
   * GET the page and land it. `fresh` (opened by the user) builds a new view and a failure becomes page error;
   * otherwise (refresh) the open view keeps its notes and a failure goes to `onFail` (a refresh after a successful
   * write: the write's result line keeps its text) or, without one, to its status line.
   */
  async getAndLand(pageId: string, token: number, fresh: boolean, onFail: OnFail = null): Promise<PageOut | null> {
    let page: PageOut;
    try {
      // only the user's open (`fresh`) may start a waiting RSI page rewrite; refreshes after a write do not
      page = await this.core.call(() => this.core.deps.api.openPage(pageId, fresh ? { rewrite: true } : {}));
    } catch (e) {
      if (this.core.disposed || token !== this.core.pageToken || isAbort(e)) return null;
      const s = this.core.state.page;
      // A refresh of a page that never got its first view (stopped before the outline) must not stay loading.
      if (fresh || (s.kind === "loading" && s.pageId === pageId)) this.core.set({ page: { kind: "error", pageId, message: errorText(e) } });
      else if (onFail) onFail(e);
      else this.core.patchView(pageId, () => ({ status: bad(errorText(e)) }));
      return null;
    }
    if (this.core.disposed || token !== this.core.pageToken) return null;
    this.notes.land(page, fresh);
    return page;
  }

  /** `onFail` null = a plain load (an error replaces the area); otherwise a refresh after a write (see refreshFail). */
  async loadCatalog(onFail: OnFail): Promise<void> {
    const seq = ++this.catalogSeq;
    const catalog = this.core.call(() => this.core.deps.api.catalog());
    const progress = this.loadProgress();             // alongside, after the catalog's request
    try {
      const c = await catalog;
      if (this.core.disposed || seq !== this.catalogSeq) return;
      if (!this.core.state.demo) setUiLanguage(c.settings.language);
      this.core.names.addNames(c.names);
      this.core.names.addCatalog(c);
      this.core.set({ catalog: { kind: "ready", value: c } });
    } catch (e) {
      if (this.core.disposed || seq !== this.catalogSeq) return;
      if (onFail && this.core.state.catalog.kind === "ready") onFail(e);
      else this.core.set({ catalog: { kind: "error", message: errorText(e) } });
    }
    await progress;
  }

  /**
   * GET /wiki/progress (the server's one answer to where learning goes on: 今天, a page's end, the goal rows). Read with
   * the catalog, which every write that changes progress reloads. A failure keeps the last answer (or says it failed).
   */
  async loadProgress(): Promise<void> {
    const seq = ++this.progressSeq;
    try {
      const p = await this.core.call(() => this.core.deps.api.progress());
      if (!this.core.disposed && seq === this.progressSeq) this.core.set({ progress: { kind: "ready", value: p } });
    } catch (e) {
      if (!this.core.disposed && seq === this.progressSeq && this.core.state.progress.kind !== "ready") this.core.set({ progress: { kind: "error", message: errorText(e) } });
    }
  }

  /**
   * GET a learning path; it lands only while `pathId` is still the chosen path. `onFail` as in loadCatalog (null = a
   * plain load: an error replaces the path view). When a path write (下一页 / 学完: pathEpoch) started after the GET was
   * sent and a path is already shown, the answer may be older than that write (e.g. 学完's response): it is dropped.
   * Other writes do not drop it. A view still loading takes it anyway (better than loading forever; nothing is re-sent).
   */
  async loadPath(pathId: string, onFail: OnFail): Promise<void> {
    const here = (): boolean => !this.core.disposed && this.core.state.path.pathId === pathId;
    if (!this.core.deps.configured()) {
      if (here()) this.core.setPath({ view: { kind: "error", get message() { return WIKI_COPY.needsSettings; } } });
      return;
    }
    if (!onFail && here()) this.core.setPath({ view: { kind: "loading" } });
    const epoch = this.pathEpoch;
    const stale = (): boolean => this.pathEpoch !== epoch && this.core.state.path.view.kind === "ready";
    try {
      const p = await this.core.call(() => this.core.deps.api.getPath(pathId));
      if (!here() || stale()) return;
      this.core.names.addPath(p);
      this.core.set({ pathPlans: { ...this.core.state.pathPlans, [pathId]: p } });
      this.core.setPath({ view: { kind: "ready", value: p } });
    } catch (e) {
      if (!here() || stale()) return;
      if (onFail && this.core.state.path.view.kind === "ready") onFail(e);
      else this.core.setPath({ view: { kind: "error", message: errorText(e) } });
    }
  }

  /**
   * GET today's review list (a read). `onFail` as in loadCatalog (null = a plain load: loading first, an error replaces
   * the list). Only the latest call's answer lands. Unconfigured: needsSettings, nothing sent.
   */
  async loadReview(onFail: OnFail): Promise<void> {
    const seq = ++this.reviewSeq;
    if (!this.core.deps.configured()) { this.core.setReview({ today: { kind: "error", get message() { return WIKI_COPY.needsSettings; } } }); return; }
    if (!onFail) this.core.setReview({ today: { kind: "loading" } });
    try {
      const t = await this.core.call(() => this.core.deps.api.reviewToday());
      if (this.core.disposed || seq !== this.reviewSeq) return;
      this.core.setReview({ today: { kind: "ready", value: t } });
    } catch (e) {
      if (this.core.disposed || seq !== this.reviewSeq) return;
      if (onFail && this.core.state.review.today.kind === "ready") onFail(e);
      else this.core.setReview({ today: { kind: "error", message: errorText(e) } });
    }
  }

  async loadK(onFail: OnFail): Promise<void> {
    if (!this.core.deps.configured()) { this.core.set({ k: { kind: "error", get message() { return WIKI_COPY.needsSettings; } } }); return; }
    if (!onFail) this.core.set({ k: { kind: "loading" } });
    try {
      const k = await this.core.call(() => this.core.deps.api.kGrid());
      this.core.names.addK(k);
      this.core.set({ k: { kind: "ready", value: k } });
    } catch (e) {
      if (onFail && this.core.state.k.kind === "ready") onFail(e);
      else this.core.set({ k: { kind: "error", message: errorText(e) } });
    }
  }

  /** `onFail` null = a plain load (loading first; an error replaces the list); otherwise a refresh of a shown list. */
  async loadRsi(onFail: OnFail): Promise<void> {
    if (!this.core.deps.configured()) { this.core.set({ rsi: { kind: "error", get message() { return WIKI_COPY.needsSettings; } } }); return; }
    if (!onFail) this.core.set({ rsi: { kind: "loading" } });
    try {
      const list = await this.core.call(() => this.core.deps.api.rsiChanges());
      // A line about an earlier failed re-read goes once the list is current again.
      this.core.set(this.core.state.rsiNote?.patchId === null ? { rsi: { kind: "ready", value: list }, rsiNote: null }
        : { rsi: { kind: "ready", value: list } });
    } catch (e) {
      if (onFail && this.core.state.rsi.kind === "ready") onFail(e);
      else this.core.set({ rsi: { kind: "error", message: errorText(e) } });
    }
  }
}
