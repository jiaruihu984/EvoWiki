/**
 * Evoui asset blocks and the reading-time reports: result events and progress (kept until the server records them,
 * resent on reconnect), the 真题 block's reads and self-reports, B5 reading batches. No LLM, no busy state.
 */
import { ApiError, validationText } from "../../api/errors";
import { WIKI_COPY } from "../copy";
import type { ReadingEntry } from "../bot/bot-schema";
import { type BlockEvent, type BlockProgressData, eventKey } from "../evoui/block-event";
import type { ExamSelfIn, ExamSelfOut, PageExamOut } from "../schema";
import { MAX_BLOCK_EVENTS } from "./helpers";
import type { WikiCore } from "./core";
import type { Reads } from "./reads";

export class Blocks {
  constructor(private readonly core: WikiCore, private readonly reads: Reads) {
    try {
      for (const { assetId, event } of core.deps.blockEventStore?.load() ?? []) {
        const pending = this.pendingEvents.get(assetId) ?? new Map<string, BlockEvent>();
        pending.set(eventKey(event), event);
        this.pendingEvents.set(assetId, pending);
      }
    } catch (error) {
      this.eventStorageReady = false;
      this.eventStorageFailed(error);
    }
    try {
      for (const { assetId, progress } of core.deps.blockProgressStore?.load() ?? []) {
        if ((this.pendingProgress.get(assetId)?.revision ?? -1) >= progress.revision) continue;
        this.pendingProgress.set(assetId, progress);
        this.progressCache.set(assetId, progress);
      }
    } catch (error) {
      this.progressStorageReady = false;
      this.progressStorageFailed(error);
    }
    queueMicrotask(() => {
      if (!core.disposed) { void this.flushBlockEvents(); void this.flushBlockProgress(); }
    });
  }

  /**
   * Evoui result events not yet recorded by the server, by asset id, then by the server's event key (oldest first). In
   * durable account-scoped storage: a reload resends the original event identities.
   */
  private eventStorageReady = true;
  private readonly pendingEvents = new Map<string, Map<string, BlockEvent>>();
  /** Assets with a results post in flight; a flush meanwhile is run again when it ends. */
  private readonly sendingEvents = new Set<string>();
  private readonly eventsAgain = new Set<string>();
  /** The latest durable UI snapshot per asset, retained until its matching version is acknowledged. */
  private progressStorageReady = true;
  private readonly unusableProgress = new WeakSet<BlockProgressData>();
  private readonly pendingProgress = new Map<string, BlockProgressData>();
  private readonly sendingProgress = new Set<string>();
  private readonly progressAgain = new Set<string>();
  /** Progress read (or saved) this session by asset id; a failed read is not cached. */
  private readonly progressCache = new Map<string, BlockProgressData | null>();
  /** Assets whose stale save was already told this session. */
  private readonly staleTold = new Set<string>();

  /** Report pending delivery without discarding the durable result handoff. */
  warnUnsent(): void {
    const unsent = [...this.pendingEvents.values()].reduce((n, m) => n + m.size, 0);
    if (unsent > 0) {
      const kept = this.core.deps.blockEventStore === null ? "only in memory" : "kept on this device for retry";
      console.warn(`EvoWiki wiki: ${String(unsent)} block result event(s) awaiting acknowledgment or local cleanup (${kept})`);
    }
    if (this.pendingProgress.size > 0) {
      const kept = this.core.deps.blockProgressStore === null ? "only in memory" : "kept on this device for retry";
      console.warn(`EvoWiki wiki: progress of ${String(this.pendingProgress.size)} block(s) awaiting acknowledgment or local cleanup (${kept})`);
    }
  }

  /** B5 reading time: whether a batch may be sent now (configured, online, not the demo). */
  canReportReading(): boolean { return !this.core.deps.demo && this.core.deps.configured() && this.core.deps.conn.state !== "offline"; }
  /** B5 reading time: one batch (ReadingReporter); rejects on failure so the batch is kept and resent. */
  async reportReading(batchId: string, entries: ReadingEntry[]): Promise<void> {
    await this.core.call(() => this.core.deps.api.postReading(batchId, entries));
  }

  /**
   * A result event of an evoui asset block (evoui.js onEvent: item / summary / route / request): sent to the server as
   * evidence (it grades items; a graded item with a known cell goes into K). Fail closed: events wait in pendingEvents
   * until the server records them and are sent again when the connection comes back (the offline banner's 重试 too) or
   * with the next event; the server skips an event it already has, so a resend never counts twice. A post the server
   * refuses (4xx other than 409 / 429: unknown asset, an event the block cannot have) is dropped with a notice. No LLM,
   * no busy state.
   */
  private eventStorageFailed(error: unknown): void {
    console.error("EvoWiki wiki: interactive results could not be kept on this device", error);
    this.core.deps.notice?.(WIKI_COPY.blockEventStorageFailed);
  }

  private persistEvents(pending: ReadonlyMap<string, ReadonlyMap<string, BlockEvent>>): boolean {
    if (!this.eventStorageReady) return false;
    try {
      this.core.deps.blockEventStore?.save([...pending].flatMap(([assetId, events]) =>
        [...events.values()].map((event) => ({ assetId, event }))));
      return true;
    } catch (error) { this.eventStorageFailed(error); return false; }
  }

  blockEvent(assetId: string, ev: BlockEvent): boolean {
    if (this.core.disposed || !this.eventStorageReady) return false;
    const event = JSON.parse(JSON.stringify(ev)) as BlockEvent;
    const key = eventKey(event);
    const mine = this.pendingEvents.get(assetId) ?? new Map<string, BlockEvent>();
    if (mine.has(key)) { void this.flushBlockEvents(); return true; }
    const next = new Map(this.pendingEvents);
    next.set(assetId, new Map(mine).set(key, event));
    if (!this.persistEvents(next)) return false;
    mine.set(key, event);
    this.pendingEvents.set(assetId, mine);
    void this.flushBlockEvents();
    return true;
  }

  private acknowledgeEvents(assetId: string, mine: Map<string, BlockEvent>, sent: readonly [string, BlockEvent][]): boolean {
    if (this.core.disposed) return false;
    const remaining = new Map(mine);
    for (const [key] of sent) remaining.delete(key);
    const next = new Map(this.pendingEvents);
    if (remaining.size === 0) next.delete(assetId); else next.set(assetId, remaining);
    if (!this.persistEvents(next)) return false;
    for (const [key] of sent) mine.delete(key);
    return true;
  }

  /** 真题练习 (exam/exam-block.ts): GET /wiki/pages/{id}/exam; null in the demo or before setup (no block). Throws on failure. */
  async pageExam(pageId: string): Promise<PageExamOut | null> {
    if (this.core.disposed || this.core.deps.demo || !this.core.deps.configured()) return null;
    return this.core.call(() => this.core.deps.api.pageExam(pageId));
  }

  /** 我答对了 / 没答对 / 我做完了 on a 真题 (POST /wiki/exam/items/{id}/self); throws when it was not recorded (the block says so). */
  async examSelf(itemId: string, result: ExamSelfIn["result"]): Promise<ExamSelfOut> {
    if (this.core.disposed || this.core.deps.demo || !this.core.deps.configured()) throw new Error("exam self-report not available here");
    return this.core.call(() => this.core.deps.api.examSelf(itemId, result));
  }

  /**
   * The progress an evoui asset block should resume from: the one saved this session, else the server's (GET, cached);
   * null = render fresh (none stored, or the server is unreachable: the offline banner already says so, nothing is shown here).
   */
  async blockProgress(assetId: string): Promise<BlockProgressData | null> {
    if (this.core.disposed || this.core.deps.demo) return null;
    const pending = this.pendingProgress.get(assetId);
    if (pending !== undefined && !this.unusableProgress.has(pending)) return pending;
    if (this.progressCache.has(assetId)) return this.progressCache.get(assetId) ?? null;
    try {
      const got = await this.core.deps.api.blockProgress(assetId);
      const again = this.pendingProgress.get(assetId);     // a save made while the read was out is newer
      if (again !== undefined && !this.unusableProgress.has(again)) return again;
      if (!this.progressCache.has(assetId)) this.progressCache.set(assetId, got.data);
      return this.progressCache.get(assetId) ?? null;
    } catch (e) {
      console.warn("EvoWiki wiki: block progress not read; the block starts fresh", e);
      return null;
    }
  }

  /**
   * evoui.js `onProgress` of an asset block: the newest progress per asset is kept until the server records it (it keeps
   * a save only when its revision is newer), resent on reconnect / 重试; a refusal (4xx other than 409 / 429) is dropped
   * with one notice. A save the server calls stale (it has newer progress, from another window or a read that failed) is
   * dropped from the cache so a reopened block resumes from the server's, with one notice per asset.
   */
  private progressStorageFailed(error: unknown): void {
    console.error("EvoWiki wiki: interactive progress could not be kept on this device", error);
    this.core.deps.notice?.(WIKI_COPY.blockProgressStorageFailed);
  }

  private persistProgress(pending: ReadonlyMap<string, BlockProgressData>): boolean {
    if (!this.progressStorageReady) return false;
    try {
      this.core.deps.blockProgressStore?.save([...pending].map(([assetId, progress]) => ({ assetId, progress })));
      return true;
    } catch (error) { this.progressStorageFailed(error); return false; }
  }

  saveBlockProgress(assetId: string, progress: BlockProgressData): boolean {
    if (this.core.disposed || !this.progressStorageReady || !Number.isInteger(progress.revision) || progress.revision < 0) return false;
    const pending = this.pendingProgress.get(assetId);
    const cached = this.progressCache.get(assetId);
    const old = cached != null && (pending === undefined || cached.revision > pending.revision) ? cached : pending;
    if (old != null && old.revision >= progress.revision) {
      if (old.revision > progress.revision || this.unusableProgress.has(old)) return false;
      if (this.pendingProgress.has(assetId)) void this.sendProgress(assetId);
      return true;
    }
    const copy = JSON.parse(JSON.stringify(progress)) as BlockProgressData;
    const next = new Map(this.pendingProgress).set(assetId, copy);
    if (!this.persistProgress(next)) return false;
    this.pendingProgress.set(assetId, copy);
    this.progressCache.set(assetId, copy);
    void this.sendProgress(assetId);
    return true;
  }

  private acknowledgeProgress(assetId: string, sent: BlockProgressData): boolean {
    if (this.core.disposed || this.pendingProgress.get(assetId) !== sent) return false;
    const next = new Map(this.pendingProgress);
    next.delete(assetId);
    if (!this.persistProgress(next)) return false;
    this.pendingProgress.delete(assetId);
    return true;
  }

  async reconcileBlockVisibility(): Promise<void> {
    this.progressCache.clear();
    await Promise.all([this.flushBlockEvents(), this.flushBlockProgress()]);
  }

  flushBlockProgress(): Promise<void> {
    return Promise.all([...this.pendingProgress.keys()].map((id) => this.sendProgress(id))).then(() => undefined);
  }

  private async sendProgress(assetId: string): Promise<void> {
    if (this.sendingProgress.has(assetId)) { this.progressAgain.add(assetId); return; }
    const sent = this.pendingProgress.get(assetId);
    if (sent === undefined || this.core.disposed) return;
    this.sendingProgress.add(assetId);
    let done = false;
    try {
      const { stale } = await this.core.deps.api.saveBlockProgress(assetId, { revision: sent.revision, data: sent });
      if (this.core.disposed) return;
      done = true;
      if (stale && this.pendingProgress.get(assetId) === sent) {
        this.unusableProgress.add(sent);
        this.progressCache.delete(assetId);
        if (!this.staleTold.has(assetId)) { this.staleTold.add(assetId); this.core.deps.notice?.(WIKI_COPY.blockProgressStale); }
      }
    } catch (e) {
      if (e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 409 && e.status !== 429) {
        if (this.core.disposed) return;
        done = true;
        this.unusableProgress.add(sent);
        if (this.progressCache.get(assetId) === sent) this.progressCache.delete(assetId);
        console.error("EvoWiki wiki: block progress refused", e);
        this.core.deps.notice?.(WIKI_COPY.blockProgressRejected + validationText(e.detail));
      } else {
        console.warn("EvoWiki wiki: block progress not sent yet; kept for the next retry", e);
      }
    } finally {
      this.sendingProgress.delete(assetId);
      if (done) this.acknowledgeProgress(assetId, sent);
    }
    const newer = this.pendingProgress.get(assetId);
    if (this.progressAgain.delete(assetId) && newer !== undefined) await this.sendProgress(assetId);
  }

  /** Sends every asset's pending events (at most 200 per post, oldest first); resolves when those posts end. */
  flushBlockEvents(): Promise<void> {
    return Promise.all([...this.pendingEvents.keys()].map((id) => this.sendEvents(id))).then(() => undefined);
  }

  private async sendEvents(assetId: string): Promise<void> {
    if (this.sendingEvents.has(assetId)) { this.eventsAgain.add(assetId); return; }
    const mine = this.pendingEvents.get(assetId);
    if (mine === undefined || mine.size === 0 || this.core.disposed) return;
    const sent = [...mine.entries()].slice(0, MAX_BLOCK_EVENTS);
    this.sendingEvents.add(assetId);
    let again = false;
    try {
      const { inK } = await this.core.deps.api.blockEvents(assetId, sent.map(([, ev]) => ev));
      if (!this.acknowledgeEvents(assetId, mine, sent)) return;
      again = mine.size > 0;
      if (inK && this.core.state.k.kind === "ready") void this.reads.loadK(() => undefined);   // the maps re-read with K
    } catch (e) {
      if (e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 409 && e.status !== 429) {
        if (!this.acknowledgeEvents(assetId, mine, sent)) return;
        console.error("EvoWiki wiki: block result events refused", e);
        this.core.deps.notice?.(WIKI_COPY.blockResultRejected + validationText(e.detail));
        again = mine.size > 0;
      } else {
        console.warn("EvoWiki wiki: block result events not sent yet; kept for the next retry", e);
      }
    } finally {
      this.sendingEvents.delete(assetId);
      if (mine.size === 0) this.pendingEvents.delete(assetId);
    }
    if (this.eventsAgain.delete(assetId) || again) await this.sendEvents(assetId);
  }
}
