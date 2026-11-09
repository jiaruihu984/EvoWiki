/**
 * BotController: the Bot's loads, reactions, approvals and settings (WikiState.bot). Owned by WikiController, which
 * forwards the Bot actions to it. Rules: reads never throw out of here and a failed re-read keeps what is shown; a
 * write is single-flight per id; a failed write notices its reason (no silent success); nothing is sent offline or
 * unconfigured.
 */
import { ApiError, validationText } from "../../api/errors";
import type { EwNoticeAction } from "../../ui/ew-notice";
import type { WriteOut } from "../schema";
import { isWriting } from "../../assistant/card-state";
import type { WikiApi } from "../api";
import { WIKI_COPY } from "../copy";
import { decideErrorText, errorText } from "../format";
import type { WikiState } from "../state";
import { sharedWritesFor, type WritesSnapshot } from "../writes-watcher";
import { BOT_COPY, BOT_KIND, SETTINGS_COPY } from "./bot-copy";
import { changeKey } from "./change-rows";
import type { BotAction, BotSettings, PauseUntil, Reaction, SettingsPatch, SourceReviseOut, SuggestionOut } from "./bot-schema";
import { orphanRevises, reviseProgress, type BotSlot } from "./bot-state";

export interface BotHost {
  api: WikiApi;
  get(): WikiState;
  patch(p: Partial<WikiState>): void;
  configured(): boolean;
  notice(t: string): void;
  noticeActions(t: string, a: EwNoticeAction[]): void;
  /** Conn-tracked, like WikiController.call. */
  call<T>(f: () => Promise<T>): Promise<T>;
  /** 查看: 个性化 · 活动记录, then the row `[data-chg=key]` scrolled into view with the ants frame. */
  showChange(key: string): void;
  /** A page's materials draft: the page in review mode at its first marked section. */
  openRevise(pageId: string): void;
  /** Opens the page's note (resolves once it is open, or not). */
  openWikiPage(id: string): Promise<void>;
  openPath(id: string): Promise<void> | void;
  /** 下一页 of the path shown in `state.path` (WikiController.pathNext). */
  pathNext(): Promise<void>;
  startReview(): void;
  /** Sends `text` as a question; with `pageId`, in that page's conversation. */
  askAssistant(text: string, pageId?: string): void;
  /** 个性化 is the tab on screen (the live writing status is read only then). */
  engineShown(): boolean;
  /** Paints the running writes' status and cost into the 最近做了什么 rows in place (no redraw). */
  paintWrites(writes: ReadonlyMap<string, WriteOut>): void;
  /** The writes watcher's last read of the live writes (GET /wiki/writes, no ids), so 个性化 does not poll a second time. */
  sharedWrites?(): WritesSnapshot | null;
  /** Re-read 个性化 · 活动记录 (a decided action may have rolled a change row back). */
  reloadRsi(): Promise<void> | void;
}

/** While a visible write row is running, /wiki/writes is read this often (ms); after a failed read, this late. */
export const WRITES_POLL_MS = 1100;
const REVISE_POLL_MS = 1500;
const WRITES_RETRY_MS = 5000;

export type WhyQuery = { triggerId: string } | { suggestionId: string };
/** The local review card's key (a suggestion id otherwise). */
export const REVIEW_KEY = "review";

/**
 * bot_failing cards are never shown (B3 does not send them; cheap fail-safe). B3 keeps a started (accepted) card listed until its
 * day ends; the demo removes it on 开始, so it is dropped here, except the review_due one (linkedReview needs it for `done`).
 */
const shown = (s: SuggestionOut): boolean => s.kind !== "bot_failing" && (s.status !== "accepted" || s.kind === "review_due");

/** `b` over `a`, field by field (the kinds and levels entries too). */
function mergePatch(a: SettingsPatch, b: SettingsPatch): SettingsPatch {
  return { ...a, ...b, ...(a.kinds || b.kinds ? { kinds: { ...a.kinds, ...b.kinds } } : {}), ...(a.levels || b.levels ? { levels: { ...a.levels, ...b.levels } } : {}) };
}
/** This computer's IANA time zone; null when the runtime cannot say. */
function systemZone(): string | null {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || null; } catch { return null; }
}

function applyPatch(cur: BotSettings, patch: SettingsPatch): BotSettings {
  return { ...cur, ...patch, kinds: { ...cur.kinds, ...patch.kinds }, levels: { ...cur.levels, ...patch.levels } };
}

export class BotController {
  private readonly seq = { today: 0, engine: 0, actions: 0, recent: 0, chips: new Map<string, number>() };
  private readonly flight = new Set<string>();

  constructor(private readonly host: BotHost) {}

  private get slot(): BotSlot { return this.host.get().bot; }
  private put(p: Partial<BotSlot>): void { this.host.patch({ bot: { ...this.host.get().bot, ...p } }); }

  /** Why nothing can be written now, or null. */
  private blocked(): string | null {
    if (!this.host.configured()) return WIKI_COPY.needsSettings;
    if (this.host.get().conn === "offline") return WIKI_COPY.offline;
    return null;
  }
  private canRead(): boolean { return this.host.configured() && this.host.get().conn !== "offline"; }
  /** A refresh was skipped while offline (nothing read since). */
  private missed = false;
  /** The today list or the ledger failed, or a refresh was skipped offline: 重试 / coming back online reads them. */
  needsRead(): boolean { return this.missed || this.slot.today.kind === "error" || this.slot.actions.kind === "error"; }
  /** The last refresh was skipped (not set up / offline then), not failed. */
  skipped(): boolean { return this.missed; }

  /** Run `f` unless one with the same key is in flight. */
  private async once(key: string, f: () => Promise<void>): Promise<void> {
    if (this.flight.has(key)) return;
    this.flight.add(key);
    try { await f(); } finally { this.flight.delete(key); }
  }

  // ---------------- loads ----------------

  /** The today list and the action ledger (parallel); a failure keeps what is shown (an error only when nothing is). */
  async refresh(): Promise<void> {
    if (!this.canRead()) { if (this.host.configured()) this.missed = true; return; }
    this.missed = false;
    const seq = ++this.seq.today;
    const today = async (): Promise<void> => {
      try {
        const [all, reminder] = await this.host.call(() => Promise.all([this.host.api.botSuggestions("today"), this.host.api.botReviewReminder()]));
        const list = all.filter(shown);
        if (seq !== this.seq.today) return;
        const ids = new Set(list.map((s) => s.suggestion_id));
        this.put({ today: { kind: "ready", value: list }, hidden: this.slot.hidden.filter((h) => ids.has(h)), reviewHidden: reminder.hidden });
      } catch (e) {
        if (seq === this.seq.today && this.slot.today.kind !== "ready") this.put({ today: { kind: "error", message: errorText(e) } });
      }
    };
    // The 助手 chips are gated by the settings (kinds.chat, page_cap): read them here too while not loaded.
    await Promise.all([today(), this.loadActions(), ...(this.slot.settings.kind === "ready" ? [] : [this.readSettings()])]);
  }

  /** The action ledger, read again (quietly: a failure keeps a list already shown). */
  async loadActions(): Promise<void> {
    const seq = ++this.seq.actions;
    try {
      const list = await this.host.call(() => this.host.api.botActions());
      if (seq === this.seq.actions) {
        this.put({ actions: { kind: "ready", value: list } });
        if (orphanRevises(list, this.slot.revise).length > 0) this.scheduleRevise();
      }
    } catch (e) {
      if (seq === this.seq.actions && this.slot.actions.kind !== "ready") this.put({ actions: { kind: "error", message: errorText(e) } });
    }
  }

  /** 个性化: the action ledger, the recent activity (the silent rows are not read) and the settings, each on its own. */
  async loadEngine(): Promise<void> {
    if (!this.canRead()) return;
    const seq = ++this.seq.engine;
    const live = (): boolean => seq === this.seq.engine;
    await Promise.all([this.loadActions(), this.loadRecent(live), this.readSettings(live)]);
  }

  /** 最近做了什么 (the silent rows are not read); a failure keeps ready rows. */
  private async loadRecent(live: () => boolean = () => true): Promise<void> {
    const seq = ++this.seq.recent;
    const current = (): boolean => live() && seq === this.seq.recent;
    try {
      const rows = (await this.host.call(() => this.host.api.botRecent(false))).filter((r) => r.kind !== "run_failed");
      if (current()) { this.put({ recent: { kind: "ready", value: rows } }); this.schedulePoll(); }
    } catch (e) {
      if (current() && this.slot.recent.kind !== "ready") this.put({ recent: { kind: "error", message: errorText(e) } });
    }
  }

  // ---------------- live page writes (个性化 · 活动记录) ----------------

  private poll: ReturnType<typeof setTimeout> | null = null;
  private disposed = false;

  /** The pages whose write the rows show: every write row that was running when it was read or is now (a page with no status read yet is asked once). */
  private writePages(): string[] {
    const r = this.slot.recent;
    if (r.kind !== "ready") return [];
    return r.value.flatMap((a) => (a.write != null && (a.result === "writing" || isWriting(this.slot.writes[a.write.page_id])) ? [a.write.page_id] : []));
  }

  /**
   * /wiki/writes is read every WRITES_POLL_MS while a visible 最近做了什么 row is writing (queued / planning / writing) and 个性化 is
   * on screen; with nothing writing the timer stops. Cost and the 「生成中 n/m」 line are painted in place; the panel is redrawn
   * only when a write starts, ends or changes state (then the rows are read again: cost, 撤销 and the page's final state come from them).
   */
  schedulePoll(): void {
    if (this.poll !== null || this.disposed || this.writePages().length === 0) return;
    this.poll = setTimeout(() => { this.poll = null; void this.readWrites(); }, WRITES_POLL_MS);
  }

  private async readWrites(): Promise<void> {
    const ids = this.writePages();
    if (ids.length === 0 || this.disposed) return;
    if (!this.canRead() || !this.host.engineShown() || (typeof document !== "undefined" && document.visibilityState === "hidden")) {
      this.schedulePoll();   // looked at again; nothing is asked while 个性化 is not on screen
      return;
    }
    let out: WriteOut[];
    // The watcher read the live writes a moment ago and every page asked for is still live: use that read. A page that is not
    // live any more (done, failed, undone) is asked for, so its final state comes from the server.
    const shared = sharedWritesFor(this.host.sharedWrites?.() ?? null, ids);
    if (shared !== null) out = shared;
    else try {
      out = await this.host.call(() => this.host.api.writes(ids));
    } catch (e) {
      console.warn("EvoWiki: could not read the writing status", e);
      this.poll = setTimeout(() => { this.poll = null; void this.readWrites(); }, WRITES_RETRY_MS);
      return;
    }
    if (this.disposed) return;
    const before = this.slot.writes;
    const next: Record<string, WriteOut> = { ...before };
    let structural = false;
    for (const id of ids) {
      const w = out.find((x) => x.page_id === id);
      const was = before[id];
      if (w === undefined) delete next[id]; else next[id] = w;
      if (was?.state !== w?.state) structural = true;
    }
    if (structural) {
      this.put({ writes: next });
      // A write that ended (or began): the rows are read again for the final cost, the ledger row and 撤销.
      await Promise.all([this.loadRecent(), this.loadActions()]);
    } else {
      // In place, no notification: a redraw every 1.1 s would reset scroll and open controls. Only a state change (above) redraws.
      this.slot.writes = next;
      this.host.paintWrites(new Map(Object.entries(next)));
    }
    this.schedulePoll();
  }

  /** The owner is closing: the poll ends. */
  dispose(): void {
    this.disposed = true;
    if (this.poll !== null) { clearTimeout(this.poll); this.poll = null; }
    if (this.revisePoll !== null) { clearTimeout(this.revisePoll); this.revisePoll = null; }
  }

  // ---------------- 资料更新: 更新这 N 页 ----------------

  private revisePoll: ReturnType<typeof setTimeout> | null = null;

  private startRevise(s: SuggestionOut, a: Extract<BotAction, { type: "revise_from_source" }>): Promise<void> {
    return this.once(`react:${s.suggestion_id}`, async () => {
      let out: SourceReviseOut;
      try { out = await this.host.call(() => this.host.api.reviseFromSource(a.source_id, a.page_ids)); } catch (e) { this.host.notice(errorText(e)); return; }
      const r = await this.react(s.suggestion_id, "accepted");
      if (r.kind === "conflict") await this.refresh();
      if (out.started.length === 0) { this.host.notice(BOT_COPY.reviseNone); this.hide(s.suggestion_id); return; }
      this.put({ revise: { ...this.slot.revise, [s.suggestion_id]: { suggestion: s, actionIds: out.started.map((x) => x.action_id), total: out.started.length, loaded: false } } });
      await this.loadActions();
      this.markLoaded(s.suggestion_id);
      this.scheduleRevise();
    });
  }
  private markLoaded(id: string): void {
    const b = this.slot.revise[id];
    if (b !== undefined && !b.loaded) this.put({ revise: { ...this.slot.revise, [id]: { ...b, loaded: true } } });
  }
  /** The ledger is read again every REVISE_POLL_MS while a batch still has a draft being written; a finished batch is dropped. */
  private scheduleRevise(): void {
    const acts = this.slot.actions.kind === "ready" ? this.slot.actions.value : [];
    const live = Object.entries(this.slot.revise).filter(([, b]) => reviseProgress(b, acts).open);
    const gone = Object.keys(this.slot.revise).filter((k) => !live.some(([l]) => l === k));
    if (gone.length > 0) this.put({ revise: Object.fromEntries(live) });
    const writing = orphanRevises(acts, this.slot.revise).some((a) => a.revise?.state === "writing");
    if (this.revisePoll !== null || this.disposed || (live.length === 0 && !writing)) return;
    this.revisePoll = setTimeout(() => {
      this.revisePoll = null;
      void this.loadActions().then(() => this.scheduleRevise());
    }, REVISE_POLL_MS);
  }
  /** 去确认: the page of the first draft waiting for 确认替换, in review mode. */
  goConfirm(key: string): void {
    const b = this.slot.revise[key];
    const acts = this.slot.actions.kind === "ready" ? this.slot.actions.value : [];
    const first = b === undefined ? key : reviseProgress(b, acts).firstWait;     // no batch: the key is the draft's own row
    const act = first === undefined ? undefined : acts.find((a) => a.action_id === first);
    const pid = act?.revise?.page_id || act?.page_id || "";
    if (pid !== "") this.host.openRevise(pid);
  }

  /** The settings read; a failure keeps ready settings (an error only when none were shown). */
  private async readSettings(live: () => boolean = () => true): Promise<void> {
    try {
      const value = await this.host.call(() => this.host.api.botSettings());
      if (live()) this.put({ settings: { kind: "ready", value } });
      if (live()) await this.followSystemZone(value);
    } catch (e) {
      if (live() && this.slot.settings.kind !== "ready") this.put({ settings: { kind: "error", message: errorText(e) } });
    }
  }

  /** The Bot's day follows this computer's time zone (no setting): a different stored zone is replaced, quietly. */
  private async followSystemZone(value: BotSettings): Promise<void> {
    const zone = systemZone();
    if (zone === null || zone === value.timezone || this.blocked() !== null) return;
    try {
      const next = await this.host.call(() => this.host.api.putBotSettings({ ...value, timezone: zone }));
      this.put({ settings: { kind: "ready", value: next } });
    } catch (e) { console.warn("EvoWiki: could not set the Bot's time zone", e); }
  }

  /** The page's suggestion chips (kind chat); a failed read keeps the chips shown. */
  async loadChips(pageId: string): Promise<void> {
    if (!this.canRead()) return;
    const seq = (this.seq.chips.get(pageId) ?? 0) + 1;
    this.seq.chips.set(pageId, seq);
    try {
      const list = (await this.host.call(() => this.host.api.botSuggestions("page", pageId))).filter((s) => s.kind === "chat" && s.status !== "accepted");   // B3 keeps an accepted chip listed until its day ends
      if (this.seq.chips.get(pageId) === seq) this.put({ chips: { ...this.slot.chips, [pageId]: list } });
    } catch { /* chips are optional: keep what is shown */ }
  }

  // ---------------- cards ----------------

  /** The data-chg key of the preference row (the shown change when it is listed, else the ledger key). */
  private preferenceKey(pid: string): string {
    const s = this.host.get();
    const c = s.rsi.kind === "ready" ? s.rsi.value.find((x) => x.patch_id === pid) : undefined;
    const acts = s.bot.actions.kind === "ready" ? s.bot.actions.value : [];
    return c === undefined ? `act:${pid}` : changeKey(c, acts.find((a) => a.action_id === pid) ?? null);
  }

  /** 查看 on a change row (key `act:<id>` / `rsi:<id>`). */
  showChange(key: string): void { this.host.showChange(key); }

  /** The review_due suggestion the local review card is linked to, if one is listed. */
  linkedReview(): SuggestionOut | null {
    const t = this.slot.today;
    return t.kind === "ready" ? t.value.find((s) => s.kind === "review_due") ?? null : null;
  }
  private card(key: string): SuggestionOut | null {
    const t = this.slot.today;
    return t.kind === "ready" ? t.value.find((s) => s.suggestion_id === key) ?? null : null;
  }

  private hide(key: string): void {
    if (!this.slot.hidden.includes(key)) this.put({ hidden: [...this.slot.hidden, key] });
  }
  private unhide(key: string): void {
    if (this.slot.hidden.includes(key)) this.put({ hidden: this.slot.hidden.filter((h) => h !== key) });
  }

  /**
   * React. `conflict` (409): the card was already reacted, not an error (no notice). `failed`: noticed with the reason.
   */
  private async react(id: string, r: Reaction): Promise<{ kind: "ok"; row: SuggestionOut } | { kind: "conflict" } | { kind: "failed" }> {
    try {
      return { kind: "ok", row: await this.host.call(() => this.host.api.botReact(id, r)) };
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) return { kind: "conflict" };
      this.host.notice(errorText(e));
      return { kind: "failed" };
    }
  }

  /** 开始 on a task card: "review" = the local review card, else a suggestion id. */
  async startKey(key: string): Promise<void> {
    if (key === REVIEW_KEY) { this.host.startReview(); return; }
    const s = this.card(key);
    if (s) await this.start(s);
  }

  /** A review round started, however it was opened: the cooldown resets on the server (and the linked suggestion is accepted; `done` follows at the end of the round). */
  async reviewStarted(): Promise<void> {
    if (!this.canRead()) return;
    const linked = this.linkedReview();
    if (linked) this.put({ reviewLinked: linked.suggestion_id });
    await this.once("review-reminder", async () => {
      try {
        const r = await this.host.call(() => this.host.api.botReviewReact("accepted"));
        this.put({ reviewHidden: r.hidden });
      } catch (e) {
        this.host.notice(errorText(e));
        this.put({ reviewLinked: null });
      }
    });
  }

  /** 减少提醒: one more step of the cooldown on the server, then the notice with 撤回 (the row is also in 活动记录). */
  private async reviewLess(): Promise<void> {
    await this.once("review-reminder", async () => {
      this.put({ reviewHidden: true });
      let pid: string | null;
      try {
        pid = (await this.host.call(() => this.host.api.botReviewReact("not_interested"))).preference_action_id ?? null;
      } catch (e) {
        this.put({ reviewHidden: false });
        this.host.notice(errorText(e));
        return;
      }
      await Promise.all([this.refresh(), Promise.resolve(this.host.reloadRsi())]);
      const text = BOT_COPY.prefNotice(true, "");
      if (pid === null) { this.host.notice(text); return; }
      const id = pid;
      this.host.noticeActions(text, [{ label: BOT_COPY.undo, run: () => { void this.undoPreference(id, null); } }]);
    });
  }

  /** Whether the review card is cooling down, read again (a ledger row was undone); a failed read keeps what is shown. */
  async readReviewReminder(): Promise<void> {
    if (!this.canRead()) return;
    try {
      const r = await this.host.call(() => this.host.api.botReviewReminder());
      this.put({ reviewHidden: r.hidden });
    } catch { /* the next 今天 read says it */ }
  }

  /** 开始: hide the card, react accepted (its error is noticed, the action still runs), then run the card's action. */
  async start(s: SuggestionOut): Promise<void> {
    const id = s.suggestion_id;
    if (s.action.type === "revise_from_source") { await this.startRevise(s, s.action); return; }
    await this.once(`react:${id}`, async () => {
      this.hide(id);
      const reacted = this.react(id, "accepted");
      await this.run(s);
      const r = await reacted;
      if (r.kind === "failed") this.unhide(id);
      else if (r.kind === "conflict") await this.refresh();
    });
  }

  private async run(s: SuggestionOut): Promise<void> {
    const a = s.action;
    switch (a.type) {
      case "revise_from_source": return;   // handled by start(): the card stays and shows its drafts
      case "start_practice": this.host.startReview(); return;
      case "open_page": {
        await this.host.openWikiPage(a.page_id);
        if (a.ask === undefined || a.ask === "") return;
        const pg = this.host.get().page;
        // The question goes into this page's conversation, so only once its note is open; else it is shown, not lost.
        if (pg.kind === "open" && pg.page.page_id === a.page_id) this.host.askAssistant(a.ask, a.page_id);
        else this.host.notice(BOT_COPY.askNotSent(a.ask));
        return;
      }
      case "open_goal": {
        // The card's own path (not the one 下一步 would pick): its next page is written only when that path is shown.
        await this.host.openPath(a.path_id);
        const ps = this.host.get().path;
        if (ps.pathId !== a.path_id || ps.view.kind !== "ready") {
          this.host.notice(ps.pathId === a.path_id && ps.view.kind === "error" ? ps.view.message : BOT_COPY.goalNotOpened);
          return;
        }
        if (ps.view.value.next_index === null) { this.host.notice(BOT_COPY.goalNoNext); return; }
        await this.host.pathNext();
        return;
      }
      case "send_to_assistant": this.host.askAssistant(a.text); return;
      case "open_settings": return;
      case "park_goal": {
        // B5: the Bot stops reminding this goal (no 继续 card, no weekly review); the goal and its pages stay.
        try { await this.host.call(() => this.host.api.botPark(a.path_id, true)); } catch (e) { this.host.notice(errorText(e)); return; }
        this.host.notice(BOT_COPY.parked);
        return;
      }
      case "replan_goal": {
        // B5: 重新规划 (one LLM call on the server; the user's own path edits are kept), then the path is shown.
        try { await this.host.call(() => this.host.api.replanPath(a.path_id)); } catch (e) { this.host.notice(errorText(e)); return; }
        this.host.notice(BOT_COPY.replanned);
        await this.host.openPath(a.path_id);
        return;
      }
    }
  }

  /** 不感兴趣 on a task card; "review" = 减少提醒 on the local review card. */
  async notInterestedKey(key: string): Promise<void> {
    if (key === REVIEW_KEY) { await this.reviewLess(); return; }
    const s = this.card(key);
    if (s) await this.notInterested(s);
  }

  /** 不感兴趣: hide, react, then the preference notice with 查看 / 撤回 (without them when the backend wrote no preference row). */
  async notInterested(s: SuggestionOut): Promise<void> {
    const id = s.suggestion_id;
    await this.once(`react:${id}`, async () => {
      this.hide(id);
      const res = await this.react(id, "not_interested");
      if (res.kind === "failed") { this.unhide(id); return; }
      if (res.kind === "conflict") { await this.refresh(); return; }
      const pid = res.row.preference_action_id ?? null;
      const text = BOT_COPY.prefNotice(s.kind === "review_due", BOT_KIND[s.kind] ?? "");
      if (pid === null) { this.host.notice(text); return; }
      // app.js prefChanged: the preference row lands in 个性化 (ledger + change list) before the notice, so 查看 finds it.
      await Promise.all([this.loadActions(), Promise.resolve(this.host.reloadRsi())]);
      this.host.noticeActions(text, [
        { label: BOT_COPY.undo, run: () => { void this.undoPreference(pid, id); } },
      ]);
    });
  }

  /** 撤回 on the preference notice: undo the row, bring the card back, re-read the ledger, the cards and the change list. */
  private async undoPreference(actionId: string, cardId: string | null): Promise<void> {
    await this.once(`act:${actionId}`, async () => {
      try {
        await this.host.call(() => this.host.api.botDecide(actionId, "undo"));
      } catch (e) { this.host.notice(errorText(e)); return; }
      if (cardId !== null) this.unhide(cardId);
      await Promise.all([this.refresh(), Promise.resolve(this.host.reloadRsi())]);
    });
  }

  /** A page chip: remove it, react accepted, send its text to the assistant. */
  async chip(s: SuggestionOut): Promise<void> {
    const id = s.suggestion_id;
    await this.once(`react:${id}`, async () => {
      const pid = s.page_id;
      const list = pid === null ? undefined : this.slot.chips[pid];
      if (pid !== null && list) this.put({ chips: { ...this.slot.chips, [pid]: list.filter((c) => c.suggestion_id !== id) } });
      const reacted = this.react(id, "accepted");
      this.host.askAssistant(s.action.type === "send_to_assistant" ? s.action.text : s.title);
      const r = await reacted;
      if (pid === null) return;
      if (r.kind === "failed" && list) {
        // Put the chip back where it was (the reason was noticed).
        const now = this.slot.chips[pid] ?? [];
        if (!now.some((c) => c.suggestion_id === id)) {
          const at = list.findIndex((c) => c.suggestion_id === id);
          this.put({ chips: { ...this.slot.chips, [pid]: [...now.slice(0, at), s, ...now.slice(at)] } });
        }
      } else if (r.kind === "conflict") await this.loadChips(pid);
    });
  }

  /** A chip by suggestion id, from any page. */
  async chipKey(id: string): Promise<void> {
    for (const list of Object.values(this.slot.chips)) {
      const s = list.find((c) => c.suggestion_id === id);
      if (s) { await this.chip(s); return; }
    }
  }

  // ---------------- ledger ----------------

  /** 批准 / 不用 / 撤销 a ledger action; then the ledger and the change list are re-read. */
  async decide(id: string, op: "approve" | "reject" | "undo"): Promise<void> {
    await this.once(`act:${id}`, async () => {
      const why = this.blocked();
      if (why !== null) { this.host.notice(why); return; }
      try {
        const a = await this.host.call(() => this.host.api.botDecide(id, op));
        this.host.notice(op === "approve" ? BOT_COPY.approved(a.detail) : op === "reject" ? BOT_COPY.rejected
          : a.kind === "bot_write" ? BOT_COPY.writeUndone : BOT_COPY.undone);
      } catch (e) {
        this.host.notice(decideErrorText(e));
      }
      // Success or not, the ledger is re-read (a 409 means it moved meanwhile). A page write changes 最近做了什么 too: an approved
      // 「要写 N 页」 starts writing, an undone write is struck through.
      await Promise.all([this.loadActions(), Promise.resolve(this.host.reloadRsi()), this.loadRecent(), this.readReviewReminder()]);
    });
  }

  /** A ledger row was decided outside 个性化 (an assistant card's 确认 / 取消 / 撤销): the ledger and the change list are re-read. */
  async ledgerChanged(): Promise<void> {
    if (!this.canRead()) return;
    await Promise.all([this.loadActions(), Promise.resolve(this.host.reloadRsi()), this.loadRecent(), this.readReviewReminder()]);
  }

  /** The 为什么 chain of a page-write row: open or close it (its pieces are on the row itself; nothing is read). */
  toggleWriteWhy(key: string): void {
    this.put({ whyOpen: this.slot.whyOpen.includes(key) ? this.slot.whyOpen.filter((k) => k !== key) : [...this.slot.whyOpen, key] });
  }

  /** The 为什么 chain of a card / row: open or close it; opening reads it unless it is already read. */
  async toggleWhy(key: string, q: WhyQuery): Promise<void> {
    if (this.slot.whyOpen.includes(key)) { this.put({ whyOpen: this.slot.whyOpen.filter((k) => k !== key) }); return; }
    this.put({ whyOpen: [...this.slot.whyOpen, key] });
    const cur = this.slot.why[key];
    if (cur !== undefined && (cur.kind === "ready" || cur.kind === "loading")) return;
    this.put({ why: { ...this.slot.why, [key]: { kind: "loading" } } });
    try {
      const value = await this.host.call(() => this.host.api.botWhy(q));
      this.put({ why: { ...this.slot.why, [key]: { kind: "ready", value } } });
    } catch (e) {
      this.put({ why: { ...this.slot.why, [key]: { kind: "error", message: errorText(e) } } });
    }
  }

  // ---------------- settings ----------------

  /**
   * Settings writes are one queue: a save or a pause asked for while one is out waits behind it (none is dropped). Saves
   * that wait together are merged field by field and sent as one PUT on the latest settings, so the last write wins per
   * field and a change never reverts another.
   */
  private readonly queue: ({ kind: "save"; patch: SettingsPatch } | { kind: "pause"; until: PauseUntil })[] = [];
  private draining: Promise<void> | null = null;
  private enqueue(op: (typeof this.queue)[number]): Promise<void> {
    this.queue.push(op);
    this.draining ??= this.drain().finally(() => { this.draining = null; });
    return this.draining;
  }
  private async drain(): Promise<void> {
    for (let op = this.queue.shift(); op !== undefined; op = this.queue.shift()) {
      if (op.kind === "pause") { await this.doPause(op.until); continue; }
      let patch = op.patch;
      while (this.queue[0]?.kind === "save") patch = mergePatch(patch, (this.queue.shift() as { patch: SettingsPatch }).patch);
      await this.doSave(patch);
    }
  }

  saveSettings(patch: SettingsPatch): Promise<void> { return this.enqueue({ kind: "save", patch }); }
  pause(until: PauseUntil): Promise<void> { return this.enqueue({ kind: "pause", until }); }

  private async doSave(patch: SettingsPatch): Promise<void> {
    const why = this.blocked();
    if (why !== null) { this.host.notice(why); this.put({ settings: { ...this.slot.settings } }); return; }
    const cur = this.slot.settings;
    if (cur.kind !== "ready") { this.host.notice(SETTINGS_COPY.failed); return; }
    try {
      const value = await this.host.call(() => this.host.api.putBotSettings(applyPatch(cur.value, patch)));
      this.put({ settings: { kind: "ready", value } });
      this.host.notice(BOT_COPY.saved);
    } catch (e) {
      // A refused PUT (422: a level B3 will not let change, a bad number) shows the server detail, and the panel is
      // redrawn from the unchanged settings so the control the user touched goes back.
      this.host.notice(e instanceof ApiError && e.status === 422 && e.detail !== "" ? validationText(e.detail) : errorText(e));
      this.put({ settings: { ...this.slot.settings } });
    }
  }

  private async doPause(until: PauseUntil): Promise<void> {
    const why = this.blocked();
    if (why !== null) { this.host.notice(why); return; }
    try {
      const value = await this.host.call(() => this.host.api.botPause(until));
      this.put({ settings: { kind: "ready", value } });
      this.host.notice(until === "tomorrow" ? BOT_COPY.pausedTomorrow(value.daily_hour, value.daily_minute, endsToday(value.paused_until, value.timezone))
        : until === "forever" ? BOT_COPY.pausedAlways : BOT_COPY.resumed);
    } catch (e) {
      this.host.notice(errorText(e));
    }
  }

  // ---------------- review round ----------------

  /** The review round just finished: the completion card, and `done` on the listed review_due suggestion. */
  reviewFinished(n: number, ok: number): void {
    const id = this.slot.reviewLinked;
    this.put({ celebrate: { n, ok, at: Date.now() }, reviewLinked: null });
    if (id === null) return;
    // accepted -> done is legal; a 409 means it was done already (no notice).
    void this.react(id, "done").then((r) => { if (r.kind === "conflict") return this.refresh(); return undefined; });
  }

  /**
   * The 学习任务全部完成 check has been drawn (app.js sets BOT.allDoneSeen right after drawing it). Written in place and
   * without a notification: a redraw now would swap the animating box for a still one.
   */
  markAllDoneSeen(): void {
    this.slot.allDoneSeen = true;
  }

  closeCelebrate(): void {
    if (this.slot.celebrate !== null) this.put({ celebrate: null });
  }
}


/** Whether a pause that ends at `until` (ISO) ends on today's date in the Bot's time zone. */
function endsToday(until: string | null, timeZone: string): boolean {
  if (until === null) return false;
  const day = (d: Date): string => {
    try { return d.toLocaleDateString("en-CA", { timeZone }); } catch { return d.toLocaleDateString("en-CA"); }
  };
  return day(new Date(until)) === day(new Date());
}
