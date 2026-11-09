import { tr } from "../i18n/index";
/**
 * A goal confirmed in the chat (free chat, a PDF: 学点新的 is not driving it) goes on where the learner is (user ruling
 * 2026-10-24, "the diagnosis appears where the user is"): under its card the chat shows the diagnosis one question at a
 * time, 跳过 plans the path with the evidence so far (answers {skip: true}); when the opening diagnosis ends a big goal
 * enters the block its map names next (`next_region`, else the first block not known). Entering a block only shows its
 * parts (spec 2026-10-28): the run stops there and the learner picks one in 学点新的 — never the first part by itself. Once
 * a point is opened and its path planned (a diagnosis over it first, here, when it needs one), its first page is written at once — a direct request, whatever the Bot's idle writing says — with the writing status, then
 * a link to the page.
 *
 * The state machine is 学点新的's own (`LearnFlow`: the same routes, rounds, 跳过, 进入, 生成第一页); this only drives it
 * forward and says what it is doing. `auto`: drive forward (a run started by 确认 in this session). A run rebuilt from a
 * stored conversation (`restore`, after a plugin reload) shows where GET /wiki/progress has the goal — the open round of
 * the goal or of a block, the page being written (its writing card), its link, a failed write's 重试 — and never enters or
 * writes by itself: it drives forward again only once the learner answers or skips in it.
 *
 * A page being written shows as the path's page card does (writing-fill.ts): it fills with the server's progress
 * {section, of}, read from GET /wiki/progress while it writes; once its first section is written its title is a link.
 *
 * Fail closed: a failed call stops the run with its line (重试 runs the step again); nothing waits silently.
 */
import { LearnFlow, learnErrorText, nodeOf, partsOf, topRegions, type Entered, type LearnRound, type LearnState } from "../wiki/learn/learn-flow";
import { nextUnit, unknownUses } from "../wiki/learn/reading";
import type { GoalApi } from "../wiki/learn/goal-api";
import type { ProgressOut, StepOut, WriteProgressOut } from "../wiki/schema";
import type { GoalResult } from "./goal-chat";

/** The first page to write: the entered block's path (or its single page), the first row's title, the goal. */
export interface FirstPage { pathId: string | null; pageId: string | null; title: string; goal: string }

export interface GoalRunHost {
  api: GoalApi;
  /**
   * 学点新的's 生成第一页: the wiki controller writes the first page and opens it (POST paths/{id}/next + organize);
   * `onPage`: the page's id once known, while it is still written.
   */
  writeFirstPage(target: FirstPage, onPage?: (pageId: string) => void): Promise<void>;
  /** The page this run's link opens comes from this goal (its note on the page). */
  pageFromGoal?(pageId: string, mapId: string): void;
  /** The page whose write the learner stopped (「停止」), else null: a first page stopped mid-write is not written. */
  stoppedWrite?(): string | null;
  /**
   * 「停止」 on the writing card: the wiki controller stops page `pageId` — its own stream, else the server's run
   * (POST run/cancel), also for a page restored after a reload. True: stopped.
   */
  stopPage?(pageId: string): Promise<boolean>;
  /** 重新写 on a stopped first page: the wiki controller writes it again and opens it. */
  restartWrite?(pageId: string): Promise<void>;
  /** Something the card shows changed. */
  changed(): void;
  /**
   * The diagnosis's questions are asked in the chat (stage 3 T11: the assistant writes each with ask_check): a message
   * that starts draft `draftId`'s (this run's, the card the learner clicked) in the conversation showing this run;
   * resolves when that turn has ended. Absent: nothing starts it. `back`: he came back to the goal: the turn goes out
   * unless a grid still waits for him there (with his taps); the server shows only what he has not answered.
   */
  diagnose?(draftId: string, back?: boolean): Promise<boolean | void>;
}

/** While a page is being written, GET /wiki/progress is read this often (ms) for its writing card. */
export const WRITING_POLL_MS = 2000;

export type GoalRunView =
  | { kind: "working"; text: string }
  | { kind: "round"; round: LearnRound; question: number; of: number; busy: boolean }
  | { kind: "writing"; pageId: string | null; title: string; progress: WriteProgressOut | null }
  | { kind: "written"; pageId: string | null; title: string; reused?: true }
  | { kind: "stopped"; pageId: string; title: string }
  | { kind: "failed"; text: string }
  /** An entered block's parts are shown and none is picked: the learner picks one in 学点新的. */
  | { kind: "choose"; name: string }
  /**
   * 读懂这一篇 (reading-mode spec §5): the next step is a unit of the material, read in 学点新的 — the first one not read
   * (`order`, `title`); `uses`: the points it uses that are not learned yet (the 先补 hint beside it, never a block).
   */
  | { kind: "read"; order: number; title: string; uses: string[] }
  | { kind: "idle" };

export const GOAL_RUN_COPY = {
  get preparing() { return tr("assistant.goal-run.GOAL_RUN_COPY.preparing.1"); },
  get checking() { return tr("assistant.goal-run.GOAL_RUN_COPY.checking.2"); },
  splitting: (name: string) => tr("assistant.goal-run.GOAL_RUN_COPY.splitting.3", { v0: name }),
  splittingParts: (name: string) => tr("assistant.goal-run.GOAL_RUN_COPY.splittingParts.4", { v0: name }),
  choose: (name: string) => tr("assistant.goal-run.GOAL_RUN_COPY.choose.5", { v0: name }),
  readNext: (order: number, title: string) => tr("assistant.goal-run.GOAL_RUN_COPY.readNext.6", { v0: order, v1: title }),
  readFirst: (names: readonly string[]) => tr("assistant.goal-run.GOAL_RUN_COPY.readFirst.7", { v0: names.join("、") }),
  /** The writing card's line: before its first section, then readable while the rest is written. */
  writingLine: (p: WriteProgressOut | null) => (p === null || p.section < 1 ? tr("assistant.goal-run.GOAL_RUN_COPY.writingLine.8")
    : p.of !== null ? tr("assistant.goal-run.GOAL_RUN_COPY.writingLine.9", { v0: Math.min(p.section, p.of), v1: p.of }) : tr("assistant.goal-run.GOAL_RUN_COPY.writingLine.10", { v0: p.section })),
  get written() { return tr("assistant.goal-run.GOAL_RUN_COPY.written.11"); },
  /** A page another goal wrote opens here: nothing was written for this goal. */
  get reused() { return tr("assistant.goal-run.GOAL_RUN_COPY.reused.12"); },
  checkingPage: (name: string) => tr("assistant.goal-run.GOAL_RUN_COPY.checkingPage.13", { v0: name }),
  get title() { return tr("assistant.goal-run.GOAL_RUN_COPY.title.14"); },
  counter: (i: number, n: number) => tr("assistant.goal-run.GOAL_RUN_COPY.counter.15", { v0: i, v1: n }),
  get idk() { return tr("assistant.goal-run.GOAL_RUN_COPY.idk.16"); },
  get skip() { return tr("assistant.goal-run.GOAL_RUN_COPY.skip.17"); },
  get retry() { return tr("assistant.goal-run.GOAL_RUN_COPY.retry.18"); },
  get stop() { return tr("assistant.goal-run.GOAL_RUN_COPY.stop.19"); },
  get stopped() { return tr("assistant.goal-run.GOAL_RUN_COPY.stopped.20"); },
  get restart() { return tr("assistant.goal-run.GOAL_RUN_COPY.restart.21"); },
  get notWritten() { return tr("assistant.goal-run.GOAL_RUN_COPY.notWritten.22"); },
  /** No question was asked (a stated beginner): where he starts, said in the chat (the demo's 「从 X 开始」). */
  startsAt: (name: string) => tr("assistant.goal-run.GOAL_RUN_COPY.startsAt.23", { v0: name }),
};

/** The progress steps a reloaded run shows (the rest: nothing begun, a block not entered yet, the goal done). */
const RESUMED: readonly StepOut["kind"][] = ["diagnosis", "writing", "failed", "read", "start_page"];

export class GoalRun {
  readonly flow: LearnFlow;
  private failure = "";
  private written: { pageId: string | null; title: string; reused?: true } | null = null;
  private writing = "";
  /** The page being written (known once the write made it) and its progress as GET /wiki/progress has it. */
  private writingId: string | null = null;
  private progress: WriteProgressOut | null = null;
  /** The first page's write was stopped (「停止」): the card reads 已停止 · 重新写. */
  private stopped: { pageId: string; title: string } | null = null;
  private step: (() => Promise<void>) | null = null;
  private unsub: () => void;
  private poll: ReturnType<typeof setTimeout> | null = null;
  private disposed = false;

  constructor(private readonly host: GoalRunHost, readonly actionId: string, private readonly result: GoalResult,
    private auto: boolean) {
    this.flow = new LearnFlow(host.api, { fail: (t) => { this.failure = t; this.host.changed(); }, edited: () => { this.host.changed(); },
      writeFirstPage: (t) => host.writeFirstPage(t, () => { void this.flow.readPath(); }) }, "");
    this.unsub = this.flow.subscribe(() => { this.host.changed(); this.advance(); });
  }

  start(): Promise<void> { return this.run(() => this.flow.openGoal({ draftId: this.result.draftId, mapId: this.result.mapId })); }

  /**
   * A reloaded chat (`auto` off): the goal where GET /wiki/progress has it now (its goal: the map, else the draft it came
   * from). Its opening diagnosis open → that round. A block's diagnosis open → that block shown, its round. A page of a block → the block shown, then that page: its writing card while
   * it is written (read again until it is done), its link once written, the failure line whose 重试 writes it. Nothing
   * begun / the goal done → nothing. An unreadable progress fails closed: its line, 重试 reads it again.
   */
  restore(): Promise<void> {
    return this.run(async () => {
      let p: ProgressOut | null = null;
      if (this.host.api.progress) {
        try { p = await this.host.api.progress(); } catch (err) { this.failure = learnErrorText(err, "other"); return; }
      }
      // the goal as its draft / map has it (a goal confirmed before its opening diagnosis closed knows its map only now)
      await this.flow.openGoal({ draftId: this.result.draftId, mapId: this.result.mapId });
      if (this.failure !== "" || this.flow.state.draftId !== null || p === null) return;
      const step = this.stepOf(p);
      if (step === null || step.region_id === null) return;
      if (!RESUMED.includes(step.kind)) return;
      await this.flow.showEntered(step.region_id, { draftId: step.draft_id, pathId: step.path_id, pageId: step.page_id });
      const e = this.flow.state.entered;
      if (this.failure !== "" || this.flow.state.draftId !== null || e === null || step.kind === "diagnosis") return;
      const title = step.title || this.target(e).title;
      if (step.page_id !== null && this.host.api.writes) {
        const status = await this.pageStatus(step.page_id);
        if (status.state === "written") { this.land(step.page_id, title); return; }
        if (status.state === "writing") {
          this.writing = title; this.writingId = step.page_id; this.progress = status.progress;
          this.track(step.page_id, true);
          return;
        }
      }
      if (step.kind === "read" && step.page_id !== null) { this.land(step.page_id, title); return; }
      if (step.kind === "writing" && step.page_id !== null) {
        this.writing = title; this.writingId = step.page_id; this.progress = step.progress;
        this.track(step.page_id, true);
        return;
      }
      // a write that failed, or the block's first page never begun: 重试 writes it (a later page is 下一页's, not the chat's)
      if (step.kind === "failed" || (step.index ?? 1) <= 1) {
        this.failure = GOAL_RUN_COPY.notWritten;
        this.step = this.write({ ...this.target(e), pageId: step.page_id ?? e.pageId, title });
      }
    });
  }

  /** This run's goal in GET /wiki/progress: its map's (null: not there). */
  private stepOf(p: ProgressOut): StepOut | null {
    const mapId = this.mapId;
    return mapId === null ? null : p.goals.find((x) => x.goal_id === mapId)?.step ?? null;
  }

  /**
   * The page being written: GET /wiki/progress read every WRITING_POLL_MS for its card. `restored`: no write of this
   * plugin waits for it, so the page's step leaving 写作中 ends it here (written: the link; failed: the line + 重试).
   */
  private track(pageId: string, restored: boolean): void {
    if (this.poll !== null || (!this.host.api.writes && !this.host.api.progress)) return;
    this.poll = setTimeout(() => {
      this.poll = null;
      void (async () => {
        const status = await this.pageStatus(pageId);
        if (this.disposed || this.writing === "" || this.writingId !== pageId) return;
        if (status.state === "unknown" || status.state === "writing") {
          this.progress = status.progress; this.host.changed();
          this.track(pageId, restored);
          return;
        }
        if (!restored) { this.track(pageId, restored); return; }
        const title = this.writing;
        this.writing = ""; this.writingId = null; this.progress = null;
        if (status.state === "failed") {
          this.failure = GOAL_RUN_COPY.notWritten;
          this.step = this.write({ pathId: null, pageId, title, goal: this.flow.state.goal });
        } else this.land(pageId, title);
        this.host.changed();
      })();
    }, WRITING_POLL_MS);
  }

  /** The page's durable write, rather than whichever page the goal currently suggests. */
  private async pageStatus(pageId: string): Promise<{ state: "writing" | "written" | "failed" | "unknown"; progress: WriteProgressOut | null }> {
    const api = this.host.api;
    try {
      if (api.writes) {
        const write = (await api.writes([pageId])).find((w) => w.page_id === pageId);
        if (!write) return { state: "unknown", progress: null };
        if (write.state === "done") return { state: "written", progress: null };
        if (write.state === "failed" || write.state === "undone" || write.state === "unwritten") return { state: "failed", progress: null };
        return { state: "writing", progress: { section: write.section ?? 0, of: write.of, pass: write.pass } };
      }
      const step = (await api.progress?.())?.goals.map((g) => g.step).find((s) => s.page_id === pageId);
      if (step?.kind === "writing") return { state: "writing", progress: step.progress };
      if (step?.kind === "read") return { state: "written", progress: null };
      if (step?.kind === "failed" || step?.kind === "start_page") return { state: "failed", progress: null };
    } catch { /* A failed status read cannot turn a write into success or failure. */ }
    return { state: "unknown", progress: null };
  }

  private stopTracking(): void {
    if (this.poll !== null) { clearTimeout(this.poll); this.poll = null; }
    this.writingId = null; this.progress = null;
  }

  /** The goal's map (the flow's, once read). */
  get mapId(): string | null { return this.flow.state.map?.map_id ?? this.result.mapId; }

  /**
   * Goes on to a block of the goal — `region`, else the one the map names next (a block's last page read, 学点新的's 进入,
   * 今天's 下一块): the map is read again, the block entered (its diagnosis here when it needs one) and its first page
   * written, as after the opening diagnosis. The learner asked: the run drives forward again.
   */
  enterBlock(region: string | null): Promise<void> {
    return this.run(async () => {
      const mapId = this.mapId;
      if (mapId === null) return;
      // Both are fresh reads; browsing a planned node need not wait for one before requesting the other.
      const mapRead = this.flow.state.map === null ? null : this.host.api.getMap(mapId).then(
        (map) => ({ map }), (error: unknown) => ({ error }),
      );
      if (await this.backToDiagnosis(mapId) || this.failure !== "") return;
      if (this.flow.state.map === null) await this.flow.openGoal({ draftId: null, mapId });
      else if (mapRead !== null) {
        const fresh = await mapRead;
        if ("error" in fresh) { this.failure = learnErrorText(fresh.error, "other"); return; }
        await this.flow.refreshMap(fresh.map);
      }
      if (this.failure !== "") return;
      const target = region ?? nextRegion(this.flow.state);
      if (target === null) return;
      this.written = null; this.writing = ""; this.stopTracking();
      this.auto = true;      // only now: a restored run driving forward on the map's read would enter the next block itself
      await this.flow.enter(target);
    });
  }

  /**
   * The goal has a diagnosis under way (GET /wiki/progress, the one source): the chat goes back to it, nothing is entered.
   * False: none, the goal goes on. An unreadable progress fails closed: its line, 重试 reads it again.
   */
  private async backToDiagnosis(mapId: string): Promise<boolean> {
    if (!this.host.api.progress) return false;
    let step: StepOut | null;
    try { step = this.stepOf(await this.host.api.progress()); } catch (err) { this.failure = learnErrorText(err, "other"); return false; }
    if (step?.kind !== "diagnosis" || step.draft_id === null) return false;
    const draftId = step.draft_id;
    await this.flow.openGoal({ draftId, mapId });
    if (this.failure !== "") return true;
    this.auto = true;
    this.diagnosed = draftId;
    try { await this.host.diagnose?.(draftId, true); } catch (err) { this.failure = learnErrorText(err, "other"); return true; }
    await this.flow.openGoal({ draftId, mapId });
    return true;
  }

  /** 学点新的 came to the front on this goal: a diagnosis under way comes back in the chat. */
  resumeDiagnosis(): Promise<void> {
    return this.run(async () => { const mapId = this.mapId; if (mapId !== null) await this.backToDiagnosis(mapId); });
  }

  /** A diagnosis of this goal went on in another conversation (he said 接着学): its end moves this run on. */
  follow(draftId: string): Promise<void> {
    return this.run(() => this.flow.openGoal({ draftId, mapId: this.mapId }));
  }

  /** A question of the round is shown and nothing is in flight: typing 跳过 in the chat skips it (as the card's control). */
  get waiting(): boolean { const v = this.view(); return v.kind === "round" && !v.busy; }

  dispose(): void {
    this.disposed = true;
    if (this.poll !== null) { clearTimeout(this.poll); this.poll = null; }
    this.unsub(); this.flow.dispose();
  }

  /** One step; its failure (the flow's own line) stops the run and 重试 runs it again. */
  private async run(step: () => Promise<void>): Promise<void> {
    this.failure = "";
    this.step = step;
    await step();
    // a step that started another one (entering a block starts its first page's write) leaves that one to 重试
    if (this.failure === "" && this.step === step) this.step = null;
    this.host.changed();
  }

  retry(): void { const s = this.step; if (s !== null) void this.run(s); }

  /**
   * An answer of its chat diagnosis went in (the turn that went on from it has ended): the draft and the map are read
   * again — the blocks recolour in 学点新的, and a draft the answer closed lets the run go on (the block entered, its
   * first page written). A draft still open waits for him (a turn elsewhere moves nothing). With no draft open only the map is read.
   */
  reread(): void {
    const s = this.flow.state;
    if (s.draftId === null) { void this.flow.refreshMap(); return; }
    const draftId = s.draftId, mapId = this.mapId;
    void this.run(async () => {
      await this.flow.openGoal({ draftId, mapId });
      if (this.flow.state.draftId === null) { this.auto = true; this.advance(); }
    });
  }

  /** An option of the shown question (−1 = 不知道); the last one of the round sends the round. */
  pick(option: number): void {
    const v = this.view();
    if (v.kind !== "round" || v.busy) return;
    const q = v.round.questions[v.question];
    if (!q) return;
    this.auto = true;                       // the learner goes on here: a restored run drives forward again
    this.flow.pick(v.round, q.index, option);
    if (this.flow.canSubmit(v.round)) void this.run(() => this.flow.submit(v.round));
    else this.host.changed();
  }

  skip(): void {
    if (this.flow.state.draftId === null || this.flow.state.busy) return;
    this.auto = true;
    void this.run(() => this.flow.skip());
  }

  /** The draft round the chat was last asked to diagnose (once per round: the learner may stop answering). */
  private diagnosed = "";
  /** The diagnosis went on in the chat (the model said where he starts); false: none was asked (a stated beginner). */
  get askedInChat(): boolean { return this.diagnosed !== ""; }
  /** The block entered, by name (null: none yet). */
  get enteredName(): string | null { return this.flow.state.entered?.name ?? null; }

  /** After every change: a finished opening diagnosis enters the next block; a planned path writes its first page. */
  private advance(): void {
    if (!this.auto || this.failure !== "" || this.flow.state.busy) return;
    const s = this.flow.state;
    const open = s.rounds[s.rounds.length - 1];
    // a round with no question is asked in the chat; once that turn ends, the draft is read again
    if (s.draftId !== null && open?.open && open.questions.length === 0 && this.host.diagnose) {
      // once per draft: the chat goes on with it question by question (each answer ends a turn; reread() follows)
      const key = s.draftId;
      if (this.diagnosed === key) return;
      this.diagnosed = key;
      const draftId = s.draftId, mapId = this.mapId, diagnose = this.host.diagnose.bind(this.host);
      void this.run(async () => {
        // the turn asked nothing and left the diagnosis open (he said no 摸底 earlier): nothing would ever move, so it ends here
        if (await diagnose(draftId) === false) { await this.flow.openGoal({ draftId, mapId }); await this.flow.skip(); return; }
        await this.flow.openGoal({ draftId, mapId });
      });
      return;
    }
    if (s.stage === "path" && s.map !== null && s.entered === null && s.draftId === null) {
      const next = nextRegion(s);
      if (next !== null) {
        void this.run(() => this.flow.enter(next));
      }
      return;
    }
    const e = s.entered;
    if (s.stage === "entered" && e !== null && s.draftId === null && !e.splitting && this.writing === "" && this.written === null && this.stopped === null
      && (e.path !== null || e.pageId !== null)) {
      const t = this.target(e);
      const made = e.rows[0]?.pageId ?? null;
      // a block planned before (its first page made): its link, nothing written again
      if (made !== null) { this.land(made, t.title); this.host.changed(); return; }
      this.writing = t.title;
      void this.run(this.write(t));
    }
  }

  /** The entered block's first page. */
  private target(e: Entered): FirstPage {
    const first = e.rows[0];
    return { pathId: e.path?.path_id ?? null, pageId: e.pageId, title: first ? first.title || first.aim : e.name, goal: e.path?.goal ?? this.flow.state.goal };
  }

  /**
   * Writes the first page (the wiki controller's 下一页 chain), then links it. The controller resolves only for a written
   * page: a failed write is the failure line, and 重试 runs this again — on the page already made, when there is one (a
   * second POST next would make the next page instead).
   */
  private write(target: FirstPage): () => Promise<void> {
    return async () => {
      this.writing = target.title;
      this.host.changed();
      try {
        const made = this.host.api.live && target.pathId !== null ? await this.firstPageId(target.pathId, null) : null;
        const onPage = (pageId: string): void => { this.writingId = pageId; this.host.changed(); this.track(pageId, false); };
        if (made !== null) onPage(made);
        if (this.host.api.live) await this.host.writeFirstPage(made !== null ? { ...target, pathId: null, pageId: made } : target, onPage);
        else if (target.pathId !== null) await this.host.api.pathNext(target.pathId);
      } catch (err) {
        // 停止 ends the write unwritten: that page is stopped (已停止 · 重新写), not failed
        const halted = this.host.stoppedWrite?.() ?? null;
        const id = this.writingId ?? await this.firstPageId(target.pathId, target.pageId);
        this.writing = ""; this.stopTracking();
        if (halted !== null && halted === id) { this.land(halted, target.title); return; }
        // the stream here ended, the write may not have (the server's run goes on): the server says, never a guess
        if (id !== null && err instanceof Error && err.name === "PageNotWritten" && (this.host.api.writes || this.host.api.progress)) {
          const status = await this.pageStatus(id);
          if (status.state === "written") { this.land(id, target.title); return; }
          if (status.state !== "failed") {
            this.writing = target.title; this.writingId = id; this.progress = status.progress; this.track(id, true);
            return;
          }
        }
        this.failure = err instanceof Error && err.name === "PageNotWritten" ? GOAL_RUN_COPY.notWritten : learnErrorText(err, "other");
        return;
      }
      const pageId = this.writingId ?? await this.firstPageId(target.pathId, target.pageId);
      this.writing = ""; this.stopTracking();
      if (pageId === null && this.host.api.live) { this.failure = GOAL_RUN_COPY.notWritten; return; }
      this.land(pageId, target.title);
    };
  }

  /** After the write: the page is written, unless the learner stopped it. */
  private land(pageId: string | null, title: string): void {
    const halted = this.host.stoppedWrite?.() ?? null;
    if (pageId !== null && halted === pageId) { this.stopped = { pageId, title }; return; }
    const e = this.flow.state.entered;
    this.written = { pageId, title, ...(e !== null && e.path === null && e.pageId === pageId ? { reused: true as const } : {}) };
    const m = this.flow.state.map;
    if (pageId !== null && m) this.host.pageFromGoal?.(pageId, m.map_id);
  }

  /** 重新写 on the stopped first page: written again (the note opens with its first section), then the link to it. */
  restart(): void {
    const st = this.stopped;
    if (st === null || this.writing !== "" || !this.host.restartWrite) return;
    const restartWrite = this.host.restartWrite.bind(this.host);
    this.stopped = null;
    this.writing = st.title; this.writingId = st.pageId;
    this.track(st.pageId, false);
    void this.run(async () => {
      try {
        await restartWrite(st.pageId);
      } catch (err) {
        this.writing = ""; this.stopTracking();
        this.failure = learnErrorText(err, "other");
        return;
      }
      this.writing = ""; this.stopTracking();
      this.land(st.pageId, st.title);
    });
  }

  /**
   * 「停止」 on the writing card: the page's write is stopped (its run cancelled on the server), the card reads
   * 已停止 · 重新写. A write this plugin streams ends on its own (its stopped page lands as stopped); a restored one is
   * ended here.
   */
  stop(): void {
    const pageId = this.writingId, title = this.writing;
    if (pageId === null || title === "" || !this.host.stopPage) return;
    const stopPage = this.host.stopPage.bind(this.host);
    void (async () => {
      const ok = await stopPage(pageId).catch(() => false);
      if (!ok || this.disposed || this.writingId !== pageId || this.step !== null) return;   // a write here lands by itself
      this.writing = ""; this.stopTracking();
      this.stopped = { pageId, title };
      this.host.changed();
    })();
  }

  /** The writing card offers 停止: a page being written whose id is known, and a host that can stop it. */
  get stoppable(): boolean { return this.writing !== "" && this.writingId !== null && this.host.stopPage !== undefined; }

  /** The first page is being written now. */
  get writingPage(): boolean { return this.writing !== ""; }

  private async firstPageId(pathId: string | null, pageId: string | null): Promise<string | null> {
    if (pathId === null) return pageId;
    try { return (await this.host.api.getPath(pathId)).pages.find((p) => p.page_id !== null)?.page_id ?? null; } catch { return null; }
  }

  view(): GoalRunView {
    if (this.failure !== "") return { kind: "failed", text: this.failure };
    if (this.written !== null) return { kind: "written", ...this.written };
    if (this.stopped !== null) return { kind: "stopped", ...this.stopped };
    if (this.writing !== "") return { kind: "writing", pageId: this.writingId, title: this.writing, progress: this.progress };
    const s = this.flow.state;
    const round = s.rounds[s.rounds.length - 1];
    if (round && round.open && round.questions.length === 0) return { kind: "idle" };   // asked in the chat
    if (round && round.open) {
      const i = round.questions.findIndex((q) => !(q.index in round.picks));
      if (i >= 0) return { kind: "round", round, question: i, of: round.questions.length, busy: s.busy };
      return { kind: "working", text: GOAL_RUN_COPY.checking };
    }
    const e = s.entered;
    if (e?.splitting) return { kind: "working", text: (e.checkingPage ? GOAL_RUN_COPY.checkingPage : e.intoParts ? GOAL_RUN_COPY.splittingParts : GOAL_RUN_COPY.splitting)(e.name) };
    if (e && s.stage === "entered" && !s.busy && s.draftId === null && e.path === null && e.pageId === null && partsOf(s.map, e.region).length > 0) {
      return { kind: "choose", name: e.name };
    }
    if (s.stage === "thinking-round") return { kind: "working", text: GOAL_RUN_COPY.checking };
    const unit = s.stage === "path" && !s.busy && s.draftId === null && s.entered === null ? nextUnit(s.map?.reading) : null;
    if (unit !== null) return { kind: "read", order: unit.order, title: unit.title, uses: unknownUses(unit).map((p) => p.name) };
    if (s.busy || s.stage === "goal") return this.auto ? { kind: "working", text: GOAL_RUN_COPY.preparing } : { kind: "idle" };
    return { kind: "idle" };
  }
}

/**
 * The node a finished opening diagnosis (or 下一块) enters: the map's next_region — a block, a sub-block or a point at any
 * depth, or an entered node waiting for a pick (entering it again only shows its parts) — else the first top block not known.
 * A 读懂 goal enters none: its next step is a unit of the material (the run's `read` view; its prerequisites are 先补 rows).
 */
export function nextRegion(s: LearnState): string | null {
  if ((s.map?.reading ?? null) !== null) return null;
  const named = s.map?.next_region ?? null;
  const n = named === null ? undefined : nodeOf(s.map, named);
  if (n !== undefined && n.state !== "known") return n.id;
  return topRegions(s.map).find((r) => r.state !== "known")?.id ?? null;
}
