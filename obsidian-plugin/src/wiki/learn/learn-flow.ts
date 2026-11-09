import { tr } from "../../i18n";
/**
 * 学点新的: big-goal-demo.html's state (`S`) and its transitions — 开始 → map + round 1, 提交 → next round or the
 * finished map, 跳过 → the finished map, 进入 → the region expanded with its learning path — driven by a GoalApi (the
 * big-goal routes) instead of the scripted data. The view (learn-view.ts) renders `state` on every change.
 *
 * Chat-first blocks (spec 2026-10-23, the demo's #blocks): 开始 sends the goal (and a 背景 line, the materials) to the
 * 助手 (`host.startChat`) in a new conversation and follows that conversation's goal_new rows (`goalRow`):
 * a draft with blocks → stage "blocks" (the cloud shows them); executed → GET /wiki/goals/{draft_id} (+ the map) and the
 * flow goes on as before (学习路线 + 第 1 轮); 取消 (rejected, not replaced) or 撤销 → the goal card again. The 学习路径 card
 * edits the map (POST /wiki/maps/{id}/edits, 撤销 = the inverse ops it answered); entering a block splits only that block (正在把「X」拆成页…)
 * and its page list is edited on the path routes (move / remove / unremove / add, 拆成多页 and its undo).
 *
 * Fail closed: a failed call puts the flow back where it was (the round open again, the form back, the 进入 buttons
 * usable) and a plain line goes to `fail` (the view shows it as a notice); nothing is silent.
 */
import { ApiError } from "../../api/errors";
import type { GoalChatRow } from "../../assistant/goal-chat";
import { errorText } from "../format";
import type { Band, PageState, WriteProgressOut, GoalDraftOut, GoalMapOut, MapEditOpIn, MapNodeOut, PathEditIn, PathEditOut, PathItemOut, PathOut, QuestionOut, ReadingUnitOpenOut, SplitOut, WriteOut } from "../schema";
import type { Attached } from "./attach";
import { MAX_MESSAGE_CHARS } from "../../generated/assistant";
import { LEARN_COPY } from "./copy";
import type { GoalApi } from "./goal-api";

export type Stage = "goal" | "thinking-map" | "blocks" | "map" | "thinking-round" | "probe" | "path" | "entered";

export interface LearnRound {
  round: number;
  /** The server's round_title ("" → 第 N 轮). round_why is never shown. */
  title: string;
  questions: QuestionOut[];
  /** question index → picked option (−1 = 不知道). */
  picks: Record<number, number>;
  open: boolean;
  /** quiz index → right / wrong (from the next response's `results`). */
  graded: Record<number, boolean>;
}

/** A page row of the entered block's list (the demo's S.pages item; the path routes' row). */
export interface PageRow {
  /** The path row's key (the edits and 拆成多页 name it). */
  key: string;
  title: string;
  /** 学完能：… ("" for a page the learner added or a part of 拆成多页: the demo shows no line then). */
  aim: string;
  exam: number;
  sections: string[];
  /** 拆成多页 is offered (a row with a node, not a part of a split, its page not opened yet). */
  canSplit: boolean;
  /** The page made for this row (POST paths/{id}/next made it), null before: its writing progress shows on the card. */
  pageId: string | null;
  /** The server's row state (planned / writing / written / read / mastered / failed / trashed) and, while writing, how far. */
  state: PageState;
  progress: WriteProgressOut | null;
}

export interface Entered {
  /** The region's map node id ("" for a small goal that finished straight as a path / page). */
  region: string;
  /** 进入「name」: the region's name, or the goal's title without a map. */
  name: string;
  path: PathOut | null;
  pageId: string | null;
  /** The page list as shown (the path's pages, then the edits' answers). */
  rows: PageRow[];
  /** 正在把「X」拆成页…: the block is being split (POST …/enter in flight). */
  splitting: boolean;
  /** The split is one level into parts (`enter` "split": 正在把「X」分成几部分…), not into pages. */
  intoParts: boolean;
  /** The point has a page another goal wrote: the server is checking whether it fits here, nothing is being split. */
  checkingPage: boolean;
  /** The part whose 继续拆细 is running (null: none). */
  splittingPart: string | null;
  /** The row whose 拆成多页 is running (null: none). */
  splittingRow: string | null;
}

/** A block of the chat's draft as the cloud shows it while it is talked over. */
export interface DraftBlock { key: string; name: string; scope: string; level?: string }

export interface LearnState {
  stage: Stage;
  /** The topic the flow was opened with (prefilled into 想学什么). */
  topic: string;
  /** The goal that was sent (shown in the done goal card). */
  goal: string;
  /** The intake's summarized topic name, carried by the proposal and saved map. */
  title: string;
  draftId: string | null;
  map: GoalMapOut | null;
  rounds: LearnRound[];
  entered: Entered | null;
  busy: boolean;
  /** stage "blocks": the chat's draft (the cloud draws it). */
  blocks: DraftBlock[];
  /** The conversation 开始 started (null: none; then no goal_new row is followed). */
  chat: string | null;
  /** The goal_new row the view follows (the newest draft of that conversation). */
  actionId: string | null;
  /** 读懂这一篇: the unit being opened (正在看这一段讲什么… on its row), null: none. */
  unitBusy: string | null;
}

/** 开始 in the chat: the conversation's key at once, and the turn (false: it failed; the chat shows why). */
export interface ChatStart { key: string; done: Promise<boolean> }

export interface FlowHost {
  /** A failed call's plain line (a notice). */
  fail(text: string): void;
  /** A path or map edit landed (the wiki changed: progress and files are read again). */
  edited?(): void;
  /**
   * 开始 with the assistant (the chat-first blocks): sends `text` as the first message of a new conversation and opens the
   * 助手 tab. null: the assistant could not take it (not set up / busy). Absent: 开始 says 助手没连上 (a goal is made only
   * in the chat).
   */
  startChat?(text: string, learn: { goal: string; background: string },
    materials?: readonly { sourceId: string; title: string; kind: string; pages: number | null }[]): ChatStart | null;
  /** Why nothing can be sent now (offline, not set up), or null. */
  blocked?(): string | null;
  /** 生成第一页 with the live API: the wiki controller writes and opens the page (POST paths/{id}/next + organize). */
  writeFirstPage?(target: { pathId: string | null; pageId: string | null; title: string; goal: string; key?: string }): Promise<void>;
}

export const regionsOf = (map: GoalMapOut | null): MapNodeOut[] => (map ? map.nodes.filter((n) => n.kind === "region") : []);
/**
 * The blocks of the 学习路线 / 学习路径 cards: the top-level regions in map order (a region nested in another, made when a
 * block was entered, is that block's part, not a row).
 */
export function topRegions(map: GoalMapOut | null): MapNodeOut[] {
  const ids = new Set(map?.nodes.map((n) => n.id) ?? []);
  return regionsOf(map).filter((r) => r.parent === null || !ids.has(r.parent));
}
/** A node of the goal map by id: a block, a sub-block or a point, at any depth. */
export const nodeOf = (map: GoalMapOut | null, id: string): MapNodeOut | undefined => map?.nodes.find((n) => n.id === id);
/**
 * An entered node's parts, in map order (spec 2026-10-28 §2.2: entering shows them and the learner picks one; none is
 * opened by itself). A part's `enter` says what it is: "split" / "children" a sub-block (进入), "learn" a point (学这个).
 */
export const partsOf = (map: GoalMapOut | null, id: string): MapNodeOut[] => (map ? map.nodes.filter((n) => n.parent === id) : []);
/** The map id of the chat's draft drawn as a map (no route takes it). */
export const DRAFT_MAP_ID = "draft";
/** The chat's draft as a map the cloud can draw (each block a collapsed region, nothing known yet). */
export function draftMap(blocks: readonly DraftBlock[], title = ""): GoalMapOut {
  return { map_id: DRAFT_MAP_ID, goal: "", title, next_region: null, reading: null, nodes: blocks.map((b) => ({ id: b.key, kind: "region", name: b.name, parent: null, scope: b.scope, level: b.level ?? "",
    prereqs: [], target: false, state: "collapsed", path_id: null, page_id: null, reused: false, members: [], atomic: false, enter: "split", mix: {}, mastery: null, counts: {} })) };
}
/** 背景：… after the goal, as the first chat message carries it (no line when there is no background). */
export function chatText(goal: string, background: string): string {
  const b = background.trim();
  return b === "" ? goal : tr("runtime.backgroundLine", { goal, background: b });
}
/**
 * What 开始 sends to the 助手 as the chat shows it (the goal, or 按《X》学 for a material, and a 背景 line): one message, so
 * the one limit is the message's (MAX_MESSAGE_CHARS, as typed in the chat); the goal and background have none of their own.
 */
export function startText(goal: string, background: string, materials: readonly { title: string }[]): string {
  const g = goal.trim();
  return chatText(g !== "" ? g : LEARN_COPY.byMaterial(materials.map((m) => m.title).join("、")), background);
}
/** The code points of 开始's message when over the limit, else 0 (the form says n/4000 and 开始 waits). */
export function startOver(goal: string, background: string, materials: readonly { title: string }[]): number {
  const n = [...startText(goal, background, materials)].length;
  return n > MAX_MESSAGE_CHARS ? n : 0;
}
/** A bare knowledge point: a member id with no concept node on the map (name = the id, no evidence). */
const barePoint = (id: string, region: string): MapNodeOut => ({ id, kind: "concept", name: id, parent: region, scope: "", level: "", prereqs: [], target: false,
  state: "expanded", path_id: null, page_id: null, reused: false, members: [], atomic: null, enter: "learn", mix: {}, mastery: null, counts: {} });

/**
 * A region's knowledge points as the cloud draws them: the concept nodes whose parent is the region, then the ids in its
 * `members` not among them — the map's concept node for that id (the cloud resolves it against the existing map by id),
 * else a bare point. A member shared by two regions is one concept node, parented by the first: it is drawn there only
 * (membersOf lists it under both).
 */
export function conceptsOf(map: GoalMapOut | null, region: string): MapNodeOut[] {
  if (!map) return [];
  const regions = new Set(regionsOf(map).map((r) => r.id));
  return membersOf(map, region).filter((c) => c.parent === region || c.parent === null || !regions.has(c.parent));
}

/** Every knowledge point of a region (its concept nodes, then its `members`), a shared one too: the 进入 card lists these. */
export function membersOf(map: GoalMapOut | null, region: string): MapNodeOut[] {
  if (!map) return [];
  const out = map.nodes.filter((n) => n.kind === "concept" && n.parent === region);
  const reg = map.nodes.find((n) => n.kind === "region" && n.id === region);
  for (const id of reg?.members ?? []) {
    // a member that is a block of its own has its own row and group: it is not a point of this one
    if (out.some((c) => c.id === id) || map.nodes.some((n) => n.kind === "region" && n.id === id)) continue;
    out.push(map.nodes.find((n) => n.kind === "concept" && n.id === id) ?? barePoint(id, region));
  }
  return out;
}

const BAND_ORDER: Band[] = ["known", "likely", "shaky", "gap"];
/** A concept's band: the band with the largest share of its mix (none when it has no evidence). */
export function bandOf(n: MapNodeOut): Band {
  let best: Band = "none", w = 0;
  for (const b of BAND_ORDER) { const v = n.mix[b] ?? 0; if (v > w) { best = b; w = v; } }
  return best;
}

/** A path page's heading: its concepts' names joined with 「、」. */
export function pageHeading(p: PathOut["pages"][number]): string {
  return p.concepts.map((c) => c.name).join("、");
}

/** 学完能：<aim>, the aim's own 「学完这页你能」 lead-in dropped when present. */
export function pageAim(aim: string): string {
  return aim.replace(/^\s*学完这页你能[：:，,\s]*/, "");
}

/**
 * A failed goal call in plain words: the region cases first; an edit refused 409 / 422 shows the server's own line; then
 * the shared wiki lines (offline / 429 / 503 …).
 */
export function learnErrorText(e: unknown, during: "enter" | "draft" | "edit" | "other"): string {
  if (e instanceof ApiError) {
    if ((e.status === 409 || e.status === 422) && during === "edit" && plainDetail(e.detail)) return e.detail;
    if (e.status === 409 && during === "enter") return LEARN_COPY.errEnterConflict;
    if (e.status === 404 && during === "enter") return LEARN_COPY.errEnterGone;
    if (e.status === 404 && during === "draft") return LEARN_COPY.errDraftGone;
  }
  return errorText(e);
}

/** A server detail that is a sentence (not a JSON dump of validation errors, not empty). */
const plainDetail = (d: string): boolean => d.trim() !== "" && !/^\s*[[{]/.test(d);

export class LearnFlow {
  private _state: LearnState;
  private readonly subs = new Set<() => void>();
  private epoch = 0;
  /**
   * A diagnosis request failed (a backend restart, the network): the server may have taken it all the same, so the next
   * step reads the draft back first (`resynced`) instead of acting on a stage that may be stale.
   */
  private stale = false;

  constructor(private readonly api: GoalApi, private readonly host: FlowHost, topic: string) {
    this._state = { stage: "goal", topic, goal: "", title: "", draftId: null, map: null, rounds: [], entered: null, busy: false, blocks: [], chat: null, actionId: null, unitBusy: null };
  }

  get state(): LearnState { return this._state; }
  get live(): boolean { return this.api.live; }
  subscribe(fn: () => void): () => void { this.subs.add(fn); return () => this.subs.delete(fn); }
  dispose(): void { this.epoch++; this.subs.clear(); }

  private set(p: Partial<LearnState>): void {
    this._state = { ...this._state, ...p };
    for (const f of [...this.subs]) f();
  }

  /**
   * 开始: the goal (and the attached materials, read already; with them the goal may be empty) goes to the 助手 in a new
   * conversation (startChat); no assistant: 助手没连上, nothing else happens.
   */
  async start(goal: string, background: string, materials: readonly Attached[] = []): Promise<void> {
    const g = goal.trim();
    const sources = materials.map((m) => m.id);
    if (this._state.stage !== "goal" || this._state.busy || (g === "" && sources.length === 0)) return;
    // offline / not set up: said at once, nothing folds or waits
    const blocked = this.host.blocked?.() ?? null;
    if (blocked !== null) { this.host.fail(blocked); return; }
    if (!this.host.startChat) { this.host.fail(LEARN_COPY.noAssistant); return; }
    // over the message's limit: said, never cut (the form holds 开始 back with n/4000 first)
    const over = startOver(g, background, materials);
    if (over > 0) { this.host.fail(LEARN_COPY.tooLong(over)); return; }
    await this.startChat(g, background, materials);
  }

  /**
   * 开始 in the chat (app.js startBlocks): the goal card folds, 「正在把「X」分成几大块…」 shows until the assistant's draft
   * arrives (goalRow). A turn that ends with no draft leaves the folded card (the conversation goes on in the 助手); a turn
   * that failed, or a chat that could not start, puts the form back with a notice.
   */
  private async startChat(goal: string, background: string, materials: readonly Attached[]): Promise<void> {
    const ep = ++this.epoch;
    // a material goes with the message (the model runs new_goal on it); its goal may be empty: 按《X》学
    const shown = goal !== "" ? goal : LEARN_COPY.byMaterial(materials.map((m) => m.title).join("、"));
    const chat = this.host.startChat!(startText(goal, background, materials), { goal, background },
      materials.map((m) => ({ sourceId: m.id, title: m.title, kind: "pdf", pages: m.pages })));
    if (chat === null) { this.host.fail(LEARN_COPY.chatFailed); return; }
    this.set({ stage: "thinking-map", goal: shown, title: "", chat: chat.key, actionId: null, blocks: [] });
    let ok: boolean;
    try { ok = await chat.done; } catch { ok = false; }
    if (ep !== this.epoch || this._state.stage !== "thinking-map") return;
    if (ok) { this.set({ stage: "blocks" }); return; }
    this.backToGoal();
    this.host.fail(LEARN_COPY.chatFailed);
  }

  /** 取消 / 撤销 of the chat's goal: the goal card again, its text kept (the view keeps it); nothing of the goal stays. */
  private backToGoal(): void {
    this.epoch++;
    this.set({ stage: "goal", title: "", draftId: null, map: null, rounds: [], entered: null, busy: false, blocks: [], actionId: null });
  }

  /**
   * A goal_new row of the conversation 开始 started (AssistantController.onGoalRow): the draft → the cloud; executed → the
   * goal's round 1 (GET /wiki/goals/{draft_id}, the map with it or GET /wiki/maps); 取消 / 撤销 → the goal card. A row of
   * another conversation, or an older row than the one followed, changes nothing.
   */
  /** Rebuild the view from the conversation's existing proposal; all edits still use that row. */
  restorePending(row: GoalChatRow, goal: string): void {
    if (row.phase !== "wait" || row.blocks.length === 0) return;
    this.set({ chat: row.key, goal });
    this.goalRow(row);
  }

  goalRow(row: GoalChatRow): void {
    const s = this._state;
    if (s.chat === null || row.key !== s.chat) return;
    const drafting = s.stage === "thinking-map" || s.stage === "blocks" || s.stage === "goal";
    if (row.phase === "wait") {
      if (!drafting || row.blocks.length === 0) return;
      this.epoch++;
      this.set({ stage: "blocks", actionId: row.actionId, title: row.title, busy: false,
        blocks: row.blocks.map((b, i) => ({ key: b.key ?? `new-${i}-${b.name}`, name: b.name, scope: b.scope, level: b.level ?? "" })) });
      return;
    }
    if (row.actionId !== s.actionId) return;
    if (row.phase === "live") {
      if (s.stage !== "blocks" || s.busy) return;
      void this.confirmed(row);
      return;
    }
    if (row.phase === "rejected" && row.replaced) return;            // a newer draft follows
    if (row.phase === "rejected" || row.phase === "undone" || (row.phase === "failed" && drafting)) this.backToGoal();
  }

  /**
   * 确认 in the chat: the goal is open; its draft (round 1) and map come in as 开始 used to bring them. No draft (the goal
   * finished at once), or a draft the server no longer has (404) while the row names the map → the map.
   */
  private async confirmed(row: GoalChatRow): Promise<void> { await this.openGoal(row.result); }

  /**
   * A goal confirmed elsewhere (the chat's card) opened here: its draft (round 1) or its map, as 确认 brings them in. The
   * 助手's chat diagnosis (goal-run.ts) drives a flow of its own this way. Its goal is the name the server has (never the card's
   * title, 「X」分成 n 块): what was typed here stays, else the draft's title, then the map's goal.
   */
  async openGoal(r: GoalChatRow["result"]): Promise<void> {
    const ep = ++this.epoch;
    if (r === null || (r.draftId === null && r.mapId === null)) { this.host.fail(LEARN_COPY.errGoalLost); return; }
    this.set({ busy: true });
    const mapOnly = (mapId: string): GoalDraftOut => ({ draft_id: null, page_id: null, path_id: null, map_id: mapId, title: this._state.goal,
      concepts: [], questions: [], notes: { from_background: 0, reused_concepts: 0, dropped_questions: 0 }, round: 0, round_title: "",
      round_why: "", results: [], map: null, decisions: [] });
    let out: GoalDraftOut;
    try {
      if (r.draftId === null) out = mapOnly(r.mapId!);
      else {
        try { out = await this.api.getDraft(r.draftId); } catch (e) {
          if (!(e instanceof ApiError && e.status === 404 && r.mapId !== null)) throw e;
          out = mapOnly(r.mapId);
        }
      }
      if (out.map === null && r.mapId !== null && out.draft_id !== null) out = { ...out, map: await this.api.getMap(r.mapId) };
    } catch (e) {
      if (ep !== this.epoch) return;
      this.set({ busy: false });
      this.host.fail(learnErrorText(e, "draft"));
      return;
    }
    if (ep !== this.epoch) return;
    this.set({ busy: false, blocks: [], goal: this._state.goal || out.title });
    await this.land(out, ep, "map");
  }

  /** A radio picked (no re-render: the view enables 提交 itself). */
  pick(round: LearnRound, index: number, option: number): void { round.picks[index] = option; }

  canSubmit(round: LearnRound): boolean { return round.open && round.questions.every((q) => q.index in round.picks); }

  /** 提交: the round closes; the next round, or the finished map / path. */
  async submit(round: LearnRound): Promise<void> {
    if (this.stale && await this.resynced()) return;
    const s = this._state;
    if (s.busy || s.draftId === null || !this.canSubmit(round) || s.rounds[s.rounds.length - 1] !== round) return;
    const ep = ++this.epoch;
    round.open = false;
    const prev = s.stage;
    this.set({ stage: "thinking-round", busy: true, rounds: [...s.rounds] });
    let out: GoalDraftOut;
    try { out = await this.api.answer(s.draftId, { answers: round.questions.map((q) => round.picks[q.index]!) }); } catch (e) {
      if (ep !== this.epoch) return;
      round.open = true;
      this.stale = true;
      this.set({ stage: prev, busy: false, rounds: [...this._state.rounds] });
      this.host.fail(learnErrorText(e, "draft"));
      return;
    }
    if (ep !== this.epoch) return;
    for (const r of out.results) if (r.kind === "quiz" && r.correct !== null) round.graded[r.index] = r.correct;
    this.set({ busy: false });
    await this.land(out, ep, s.entered ? "entered" : "probe");
  }

  /** 跳过: every round closes; the server finishes with the evidence so far (answers {skip: true}). */
  async skip(): Promise<void> {
    if (this.stale && await this.resynced()) return;
    const s = this._state;
    if (s.busy || s.draftId === null) return;
    const ep = ++this.epoch;
    const open = s.rounds.filter((r) => r.open);
    open.forEach((r) => (r.open = false));
    this.set({ busy: true, rounds: [...s.rounds] });
    let out: GoalDraftOut;
    try { out = await this.api.answer(s.draftId, { skip: true }); } catch (e) {
      if (ep !== this.epoch) return;
      open.forEach((r) => (r.open = true));
      this.stale = true;
      this.set({ busy: false, rounds: [...this._state.rounds] });
      this.host.fail(learnErrorText(e, "draft"));
      return;
    }
    if (ep !== this.epoch) return;
    this.set({ busy: false });
    await this.land(out, ep, s.entered ? "entered" : "probe");
  }

  /**
   * A response: the new map (newly expanded regions start their expansion), then a round (draft_id), the finished map
   * (map_id; read with GET /wiki/maps when the response carries none), or a path / page (the region being entered, or a
   * small goal without a map).
   */
  private async land(out: GoalDraftOut, ep: number, roundStage: Stage): Promise<void> {
    if (out.map) this.setMap(out.map);
    if (out.draft_id !== null) {
      const round: LearnRound = { round: out.round, title: out.round_title, questions: out.questions, picks: {}, open: true, graded: {} };
      this.set({ draftId: out.draft_id, rounds: [...this._state.rounds, round], stage: roundStage });
      return;
    }
    // Finished: every round closes.
    this._state.rounds.forEach((r) => (r.open = false));
    const pending = this._state.entered;
    if (out.map_id !== null) {
      if (!out.map) {
        try { this.setMap(await this.api.getMap(out.map_id)); } catch (e) {
          if (ep !== this.epoch) return;
          this.host.fail(learnErrorText(e, "other"));
        }
        if (ep !== this.epoch) return;
      }
      this.set({ draftId: null, rounds: [...this._state.rounds], stage: pending ? "entered" : "path" });
      return;
    }
    if (out.path_id === null && out.page_id === null) {
      this.set({ draftId: null, rounds: [...this._state.rounds], stage: pending ? "entered" : "path" });
      return;
    }
    // A path (or a single page) for the region being entered, or for the whole goal when it is small.
    const region = pending?.region ?? "";
    const name = pending?.name ?? out.title;
    let path: PathOut | null = null;
    if (out.path_id !== null) {
      try { path = await this.api.getPath(out.path_id); } catch (e) {
        if (ep !== this.epoch) return;
        this.host.fail(learnErrorText(e, "other"));
      }
      if (ep !== this.epoch) return;
    }
    this.set({ draftId: null, rounds: [...this._state.rounds], stage: "entered", entered: enteredOf(region, name, path, out.page_id) });
  }

  /**
   * The draft read back (GET /wiki/goals/{draft_id}) after a lost request, or (`always`) before 进入 with a diagnosis
   * open. True when the caller should stop: the flow moved (the server had taken the answer: its next round or its result
   * is shown now) or the read failed (its line shown). False: the server has the same round open.
   */
  private async resynced(always = false): Promise<boolean> {
    const id = this._state.draftId;
    if (id === null || (!this.stale && !always)) return false;
    const ep = ++this.epoch;
    this.set({ busy: true });
    let out: GoalDraftOut;
    try { out = await this.api.getDraft(id); } catch (e) {
      if (ep !== this.epoch) return true;
      this.set({ busy: false });
      this.host.fail(learnErrorText(e, "draft"));
      return true;
    }
    if (ep !== this.epoch) return true;
    this.stale = false;
    const last = this._state.rounds[this._state.rounds.length - 1];
    if (out.draft_id !== null && last !== undefined && last.round === out.round) { this.set({ busy: false }); return false; }
    this._state.rounds.forEach((r) => (r.open = false));
    this.set({ busy: false, rounds: [...this._state.rounds] });
    await this.land(out, ep, this._state.entered ? "entered" : "probe");
    return true;
  }

  /** After a 409 on 进入: the map read back; true when the block now has its pages (the next 进入 shows them). */
  private async mapPlanned(mapId: string, regionId: string): Promise<boolean> {
    try {
      const map = await this.api.getMap(mapId);
      const r = nodeOf(map, regionId);
      if (!r || (r.path_id === null && r.page_id === null)) return false;
      this.setMap(map);
      return true;
    } catch { return false; }
  }

  /** The map read again (GET /wiki/maps/{id}): blocks planned or learned since show as they are. Its failure is the line. */
  async refreshMap(fresh?: GoalMapOut): Promise<void> {
    const id = this._state.map?.map_id;
    if (id === undefined || id === DRAFT_MAP_ID) return;
    try { this.setMap(fresh ?? await this.api.getMap(id)); } catch (e) { this.host.fail(learnErrorText(e, "other")); }
  }

  /**
   * 读懂这一篇 (reading-mode spec §3.5, §5): a unit of the goal's reading entered (POST …/open: 「看这一段」 the first time;
   * 正在看这一段讲什么… on its row meanwhile), `start` also starts its reading page; then the map is read again (the unit's
   * status, points and uses, a prerequisite it added). Null: nothing to open, or the notice said why (a refusal's own line).
   */
  async openUnit(unitId: string, start: boolean): Promise<ReadingUnitOpenOut | null> {
    const s = this._state, r = s.map?.reading;
    if (!r || !this.api.openUnit) return null;
    if (s.busy || s.unitBusy !== null) { this.host.fail(LEARN_COPY.errBusy); return null; }
    const ep = this.epoch;
    this.set({ unitBusy: unitId });
    let out: ReadingUnitOpenOut;
    try { out = await this.api.openUnit(r.reading_id, unitId, start); } catch (e) {
      if (ep === this.epoch) { this.set({ unitBusy: null }); this.host.fail(learnErrorText(e, "edit")); }
      return null;
    }
    if (ep !== this.epoch) return null;
    this.set({ unitBusy: null });
    await this.refreshMap();
    return out;
  }

  /** The cloud stages new regions / points and eases changed bands itself (goal-cloud.ts, grow.ts schedule). */
  private setMap(map: GoalMapOut): void {
    if (JSON.stringify(map) === JSON.stringify(this._state.map)) return;
    this.set({ map, title: map.title, goal: this._state.goal || map.goal });
  }

  /**
   * 开始学习 (a row of 学习路径 or of a node's parts, or a click on the region in the cloud): any map node, known or not
   * (known is a state, K stays as it is; spec 2026-10-28 §2.2). By its `enter`: "split" splits it one level (正在把「X」分成几部分… meanwhile),
   * "children" shows the parts it has (no model call) — either way the parts are shown and the learner picks one; "learn"
   * opens the point: a diagnosis round over it, or its path. A node already planned shows its path again.
   */
  async enter(regionId: string): Promise<void> {
    // never a silent no-op: a step still running or a diagnosis still open says so;
    // a diagnosis left open by a lost request is read back first and, once the server has it finished, it goes on
    if (this._state.busy) { this.host.fail(LEARN_COPY.errBusy); return; }
    if (this._state.draftId !== null) {
      if (await this.resynced(true) && this._state.draftId !== null) return;
      if (this._state.draftId !== null) { this.host.fail(LEARN_COPY.errDiagnosing); return; }
    }
    const s = this._state;
    const r = nodeOf(s.map, regionId);
    if (s.map === null || !r) { this.host.fail(LEARN_COPY.errEnterGone); return; }
    if (s.stage !== "path" && s.stage !== "entered") { this.host.fail(LEARN_COPY.errEnterConflict); return; }
    if (s.entered?.region === regionId) return;           // it is the block shown
    const ep = ++this.epoch;
    const before = s.entered, stageBefore = s.stage;
    // Already planned: show it again (POST enter would be a 409).
    if (r.path_id !== null || r.page_id !== null) {
      this.set({ entered: enteredOf(regionId, r.name, null, r.page_id), stage: "entered", busy: r.path_id !== null });
      if (r.path_id === null) return;
      try {
        const path = await this.api.getPath(r.path_id);
        if (ep !== this.epoch) return;
        this.set({ entered: enteredOf(regionId, r.name, path, null), busy: false });
      } catch (e) {
        if (ep !== this.epoch) return;
        this.set({ busy: false });
        this.host.fail(learnErrorText(e, "other"));
      }
      return;
    }
    // parts it has already are shown at once (no call is waited on); a split or an opening says it is running
    this.set({ busy: true, stage: "entered",
      entered: { ...enteredOf(regionId, r.name, null, null), splitting: r.enter !== "children", intoParts: r.enter === "split", checkingPage: r.enter === "page" } });
    let out: GoalDraftOut;
    try { out = await this.api.enterRegion(s.map.map_id, regionId); } catch (e) {
      if (ep !== this.epoch) return;
      this.set({ busy: false, entered: before, stage: stageBefore });
      // 409: the block has its pages already (an earlier 进入 whose answer was lost): the map read back shows them
      if (e instanceof ApiError && e.status === 409 && await this.mapPlanned(s.map.map_id, regionId)) { await this.enter(regionId); return; }
      this.host.fail(learnErrorText(e, "enter"));
      return;
    }
    if (ep !== this.epoch) return;
    this.set({ busy: false, entered: enteredOf(regionId, r.name, null, null), stage: "entered" });
    await this.land(out, ep, "entered");
  }

  /**
   * A reload: the node the server has him in, shown as it is. Only reads (entering is the learner's: POST enter can split
   * the node and spend a model call): its path or page, and the diagnosis round still open over it.
   */
  async showEntered(regionId: string, at: { draftId: string | null; pathId: string | null; pageId: string | null }): Promise<void> {
    const r = nodeOf(this._state.map, regionId);
    if (!r) { this.host.fail(LEARN_COPY.errEnterGone); return; }
    const ep = ++this.epoch;
    const pathId = at.pathId ?? r.path_id, pageId = at.pageId ?? r.page_id;
    this.set({ busy: true, stage: "entered", entered: enteredOf(regionId, r.name, null, pageId) });
    try {
      const path = pathId !== null ? await this.api.getPath(pathId) : null;
      const out = at.draftId !== null ? await this.api.getDraft(at.draftId) : null;
      if (ep !== this.epoch) return;
      this.set({ busy: false, entered: enteredOf(regionId, r.name, path, path === null ? pageId : null) });
      if (out !== null) await this.land(out, ep, "entered");
    } catch (e) {
      if (ep !== this.epoch) return;
      this.set({ busy: false });
      this.host.fail(learnErrorText(e, "other"));
    }
  }

  // ---- 学习路径: the map's blocks (POST /wiki/maps/{id}/edits) ----

  /**
   * One edit of the 学习路径 card, sent at once; the answer's map replaces the map. `shown`: the map as it should look right
   * away (a moved row), put back when the edit fails. Resolves to the inverse ops the server answered, sent back for 撤销
   * (null: refused, a notice said why — the server's 409 text, e.g. 这一块正在诊断，先答完或跳过).
   */
  private async editMap(ops: MapEditOpIn[], shown?: GoalMapOut): Promise<MapEditOpIn[] | null> {
    const s = this._state, map = s.map;
    if (map === null || map.map_id === DRAFT_MAP_ID || s.busy) return null;
    if (shown) this.set({ map: shown });
    try {
      const out = await this.api.editMap(map.map_id, ops);
      this.set({ map: out.map, ...this.keepEntered(out.map) });
      this.host.edited?.();
      return out.undo;
    } catch (e) {
      if (shown) this.set({ map });
      this.host.fail(learnErrorText(e, "edit"));
      return null;
    }
  }

  /** The entered card stays only while its node is still on the map. */
  private keepEntered(map: GoalMapOut): Partial<LearnState> {
    const e = this._state.entered;
    if (e === null || e.region === "" || nodeOf(map, e.region) !== undefined) return {};
    return { entered: null, stage: "path" };
  }

  /** Drag / Alt + ↑↓ on a block: every top-level block in its new order (reorder). */
  async moveBlock(from: number, to: number): Promise<void> {
    const map = this._state.map, rows = topRegions(map);
    if (map === null || from === to || from < 0 || to < 0 || from >= rows.length || to >= rows.length) return;
    const ids = rows.map((r) => r.id);
    const [x] = ids.splice(from, 1); ids.splice(to, 0, x!);
    // the map shown at once: each block (with the nodes that follow it up to the next block) in the new order
    const pos = new Set(ids);
    const groups = new Map<string, MapNodeOut[]>(), head: MapNodeOut[] = [];
    let cur: string | null = null;
    for (const n of map.nodes) {
      if (pos.has(n.id)) { cur = n.id; groups.set(cur, [n]); } else if (cur !== null) groups.get(cur)!.push(n); else head.push(n);
    }
    const shown: GoalMapOut = { ...map, nodes: [...head, ...ids.flatMap((id) => groups.get(id) ?? [])] };
    await this.editMap([{ op: "reorder", ids }], shown);
  }
  /** ⋯ 删除: resolves to its inverse ops (撤销 sends them back with undoEdit: the block back, later edits kept). */
  dropBlock(id: string): Promise<MapEditOpIn[] | null> { return this.editMap([{ op: "drop", id }]); }
  /** ⋯ 改名 → 保存. */
  async renameBlock(id: string, name: string): Promise<void> {
    const v = name.trim(), r = topRegions(this._state.map).find((x) => x.id === id);
    if (v === "" || !r || r.name === v) return;
    await this.editMap([{ op: "rename", id, name: v }]);
  }
  /** 加一块 … 加上: a new block at the end (it is split when entered). */
  async addBlock(name: string): Promise<boolean> {
    const v = name.trim();
    if (v === "") return false;
    return (await this.editMap([{ op: "add", name: v }])) !== null;
  }
  /** 撤销 of a 学习路径 edit: the inverse ops the edit answered (a stale one is refused: 后面又改过，不能这样撤销). */
  async undoEdit(undo: MapEditOpIn[]): Promise<void> { if (undo.length > 0) await this.editMap(undo); }

  /**
   * 继续拆细 on a point of the entered node's parts: POST /wiki/nodes/{id}/split with this goal's map (one model call; its
   * parts go under it on this map), then the map read again — the point is a sub-block now (进入 shows its parts; nothing
   * is entered by itself). Resolves to the split's answer; null when refused or failed (a notice said why).
   */
  async splitPart(nodeId: string): Promise<SplitOut | null> {
    const s = this._state, map = s.map, e = s.entered;
    if (s.busy) { this.host.fail(LEARN_COPY.errBusy); return null; }
    if (map === null || map.map_id === DRAFT_MAP_ID || e === null || e.splittingPart !== null) return null;
    this.set({ entered: { ...e, splittingPart: nodeId } });
    let out: SplitOut | null = null;
    try {
      out = await this.api.splitNode(nodeId, map.map_id);
      const fresh = await this.api.getMap(map.map_id);
      if (this._state.map?.map_id === map.map_id) this.set({ map: fresh, ...this.keepEntered(fresh) });
      this.host.edited?.();
    } catch (err) {
      this.host.fail(learnErrorText(err, "other"));
      out = null;
    }
    const cur = this._state.entered;
    if (cur !== null && cur.splittingPart === nodeId) this.set({ entered: { ...cur, splittingPart: null } });
    return out;
  }

  // ---- the entered block's page list (the path routes) ----

  /** Where the pages' writing is (GET /wiki/writes); null: this API cannot say (no progress is shown). */
  writes(pageIds: string[]): Promise<WriteOut[]> | null { return this.api.writes ? this.api.writes(pageIds) : null; }

  /**
   * The entered path read again (GET /wiki/paths/{id}): the rows learn the pages made since (生成第一页's POST next runs in
   * the wiki controller), so their cards can show the writing. The rows' own aims stay. False: no path, or the read failed
   * (nothing changes; the next read tries again).
   */
  async readPath(): Promise<boolean> {
    const e = this._state.entered;
    if (e === null || e.path === null || e.splitting) return false;
    const id = e.path.path_id;
    let path: PathOut;
    try { path = await this.api.getPath(id); } catch { return false; }
    const cur = this._state.entered;
    if (cur === null || cur.path?.path_id !== id) return false;
    const aim = new Map(cur.rows.map((r) => [r.key, r.aim]));
    this.set({ entered: { ...cur, path, rows: pathRows(path).map((r) => ({ ...r, aim: aim.get(r.key) ?? r.aim })) } });
    return true;
  }

  /** Keys of rows that are parts of 拆成多页 (the demo offers no second split for them). */
  private readonly parts = new Set<string>();

  /** One edit of the page list; the answer's rows replace the list. Null when it failed (a notice said why). */
  private async editRows(run: (pathId: string) => Promise<PathEditOut>, shown?: PageRow[]): Promise<PathEditOut | null> {
    const e = this._state.entered;
    if (e === null || e.path === null || e.splitting) return null;
    const path = e.path, before = e.rows;
    if (shown) this.set({ entered: { ...e, rows: shown } });
    try {
      const out = await run(path.path_id);
      const cur = this._state.entered;
      if (cur !== null && cur.path?.path_id === path.path_id) this.set({ entered: { ...cur, rows: rowsOf(out.path, before, this.parts) } });
      this.host.edited?.();
      return out;
    } catch (err) {
      const cur = this._state.entered;
      if (cur !== null && cur.path?.path_id === path.path_id && shown) this.set({ entered: { ...cur, rows: before } });
      this.host.fail(learnErrorText(err, "edit"));
      return null;
    }
  }

  async movePage(from: number, to: number): Promise<void> {
    const rows = this._state.entered?.rows ?? [];
    if (from === to || from < 0 || to < 0 || from >= rows.length || to >= rows.length) return;
    const shown = [...rows]; const [x] = shown.splice(from, 1); shown.splice(to, 0, x!);
    const edit: PathEditIn = { op: "move", from, to };
    await this.editRows((id) => this.api.editPath(id, edit), shown);
  }
  /** ⋯ 删除: resolves to the removed row's key (撤销 = unremovePage), null when it failed. */
  async removePage(index: number): Promise<string | null> {
    const rows = this._state.entered?.rows ?? [], row = rows[index];
    if (!row) return null;
    const edit: PathEditIn = { op: "remove", index };
    return (await this.editRows((id) => this.api.editPath(id, edit), rows.filter((_, k) => k !== index))) ? row.key : null;
  }
  async unremovePage(key: string): Promise<void> {
    const edit: PathEditIn = { op: "unremove", page_key: key };
    await this.editRows((id) => this.api.editPath(id, edit));
  }
  /** ⋯ 重命名 → 保存 (and its 撤销, with the old title). False: it failed (a notice said why). */
  async renamePage(key: string, title: string): Promise<boolean> {
    const rows = this._state.entered?.rows ?? [], v = title.trim();
    if (v === "") return false;
    const edit: PathEditIn = { op: "retitle", page_key: key, title: v };
    return (await this.editRows((id) => this.api.editPath(id, edit), rows.map((r) => (r.key === key ? { ...r, title: v } : r)))) !== null;
  }
  /** 加一页 … 加上. */
  async addPage(title: string): Promise<boolean> {
    const v = title.trim();
    if (v === "") return false;
    const edit: PathEditIn = { op: "add", title: v };
    return (await this.editRows((id) => this.api.editPath(id, edit))) !== null;
  }
  /**
   * ⋯ 拆成多页 (may call the model: the row says 正在拆成多页… meanwhile). Resolves to how many rows took its place and the
   * undo token (null undo: nothing to undo), `kept` the model's line when it left the node whole (no row changed); null
   * when it failed.
   */
  async splitPage(key: string): Promise<{ n: number; undo: string | null; kept: string } | null> {
    const e = this._state.entered;
    if (e === null || e.path === null || e.splittingRow !== null || !e.rows.some((r) => r.key === key && r.canSplit)) return null;
    const before = new Set(e.rows.map((r) => r.key));
    this.set({ entered: { ...e, splittingRow: key } });
    const out = await this.editRows((id) => this.api.splitRow(id, key));
    const cur = this._state.entered;
    if (out === null) { if (cur !== null) this.set({ entered: { ...cur, splittingRow: null } }); return null; }
    const fresh = out.path.filter((r) => !before.has(r.key)).map((r) => r.key);
    fresh.forEach((k) => this.parts.add(k));
    if (cur !== null) this.set({ entered: { ...cur, splittingRow: null, rows: cur.rows.map((r) => (this.parts.has(r.key) ? { ...r, canSplit: false } : r)) } });
    return { n: fresh.length, undo: out.undo, kept: out.kept };
  }
  async undoSplit(key: string, undo: string): Promise<void> {
    await this.editRows((id) => this.api.undoSplitRow(id, key, undo));
  }
}

/** The entered card before its pages are known (rows from the path once it is read). */
function enteredOf(region: string, name: string, path: PathOut | null, pageId: string | null): Entered {
  // a point that opens an existing page (a page another goal wrote that fits here, goals/page_fit.py): that page is its one row
  const page: PageRow[] = pageId === null ? [] : [{ key: pageId, title: name, aim: "", exam: 0, sections: [], canSplit: false, pageId, state: "written", progress: null }];
  return { region, name, path, pageId, rows: path ? pathRows(path) : page, splitting: false, intoParts: false, checkingPage: false, splittingRow: null, splittingPart: null };
}

/** A path's pages as rows, keyed by the server's row key (PathPageOut.key). */
export function pathRows(path: PathOut): PageRow[] {
  return path.pages.map((p) => ({ key: p.key, title: p.title !== "" ? p.title : pageHeading(p), aim: pageAim(p.aim),
    exam: p.exam, sections: p.sections, canSplit: p.concepts.some((c) => c.target) && p.page_id === null, pageId: p.page_id,
    state: p.state, progress: p.progress }));
}

/** The rows an edit answered: the aims stay from the rows they had (a new row has none). */
function rowsOf(items: readonly PathItemOut[], before: readonly PageRow[], parts: ReadonlySet<string>): PageRow[] {
  const was = new Map(before.map((r) => [r.key, r]));
  return items.map((i) => ({ key: i.key, title: i.title, aim: was.get(i.key)?.aim ?? "", exam: i.exam, sections: i.sections,
    canSplit: i.node_ids.length > 0 && !i.opened && !parts.has(i.key), pageId: i.page_id ?? was.get(i.key)?.pageId ?? null,
    state: i.state, progress: i.progress }));
}
