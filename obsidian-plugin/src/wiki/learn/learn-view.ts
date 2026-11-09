import { goalLevelLabel } from "../../ui/goal-level";
import { labelNode } from "../../i18n/index";
import { localizedText, localizedAttribute } from "../../i18n/index";
/**
 * 学点新的: a main-area tab that is big-goal-demo.html as build.py transforms it (no header, one grid row, monochrome).
 * `div.ew.ew-learn` > `.ew-app` grid: `.ew-panel` (the cards, rebuilt by render() exactly as the demo's render()) and
 * `.ew-cloudwrap` (the goal cloud's canvas, the `.ew-tip` hover tip and the 掌握 / 熟悉 / 不会 legend).
 *
 * The flow runs on a GoalApi (host.api(): the big-goal routes; the scripted fake only for the screenshot fixture).
 * openTopic(topic) prefills 想学什么; a different topic starts a fresh flow, the same topic (or none) keeps the running one;
 * newGoal() is the empty form (新建目标 on the goal card, the command, 学点新的 with nothing typed).
 * A round card's heading is 第 N 轮 · round_title (第 N 轮 without one; never round_why); the cloud and the lists carry no numbers.
 *
 * 上传附件 (host.sources; the full demo's goalCard after the 2026-10-21 rulings): a file picked or dropped on the card goes
 * through 参数 · 资料's own upload (the original saved under `<notes>/资料/`). The material card shows the file name,
 * 「正在解析」 and ✕ while it is read, then a 目录 link (the sections: rename inline, ← → one level, 收起). ONE 开始, disabled
 * until the material is read; the plugin never guesses its use. A progress tick redraws only the material card and 开始,
 * so typing in the form is never interrupted.
 */
import { ItemView, type ViewStateResult, type WorkspaceLeaf } from "obsidian";
import { ApiError, OfflineError } from "../../api/errors";
import { btn, el } from "../../ui/ew";
import { ewNotice } from "../../ui/ew-notice";
import { reorderable } from "../../ui/reorder";
import { openMenuAt } from "../../ui/floating";
import { colorOfMix, readColors, rgb, type CloudColors } from "../colors";
import { tipSpot, GraphCloud, type GraphCloudHandle, type Hover } from "../graph-page/sigma-cloud";
import { GRAPH_COPY } from "../graph-page/copy";
import { examTagTip } from "../exam/exam-model";
import { qTip } from "../q-tip";
import { regionWord } from "../starmap/starmap-view";
import { buildScene, realId, type Scene } from "../graph-page/scene";
import { GraphStore, type GraphSource } from "../graph-page/store";
import { BAND_WORD, errorText } from "../format";
import { coverLine, rowSections } from "../graph-page/panel-logic";
import type {
  Band, BlockOut, GoalMapOut, GraphPlacesOut, MapNodeOut, ProgressOut, ReadingOut, ReadingPointOut, ReadingUnitOut, SectionEditIn, SourceDetail, SourceOut,
} from "../schema";
import { SourceBlocked, SourceRefused, type SourceUpdate } from "../sources/sources-controller";
import { SOURCES_COPY } from "../sources/sources-copy";
import { type Attached, canShift, canStart, shiftDepth, type TocRow, tocOf } from "./attach";
import { LEARN_COPY } from "./copy";
import { nextUnit, progressLine, unitMix, unknownUses, useTarget } from "./reading";
import { PAGE_PROGRESS_POLL_MS, rowPending, rowProgress, streamProgress, type StreamProgress } from "./page-progress";
import { fillLayer, paintFill } from "./writing-fill";
import type { GoalApi } from "./goal-api";
import type { GoalChatRow, PendingGoal } from "../../assistant/goal-chat";
import { pickTopic, TOPICS } from "./goal-fake";
import { learnScene, type LearnScene } from "./learn-scene";
import {
  bandOf, type ChatStart, draftMap, type DraftBlock, LearnFlow, learnErrorText, nodeOf, type PageRow, partsOf, regionsOf,
  startOver, topRegions,
} from "./learn-flow";

export const LEARN_VIEW_TYPE = "evowiki-learn";

/** A block row's state (its dot and word): 掌握 / 熟悉 / 不会 / 正在问 / 还没问. */
type BlockState = keyof typeof LEARN_COPY.stateWord;
/** A block row's state as the band its point is drawn in (正在问 is a ring, not a colour). */
const TINT: Record<BlockState, Band> = { known: "known", shaky: "shaky", gap: "gap", asking: "none", none: "none" };

export interface LearnHost {
  /** The goal API a fresh flow runs on (read per flow). */
  api(): GoalApi;
  /** Obsidian's `css-change` (theme switched): returns the unsubscribe. */
  onCssChange(fn: () => void): () => void;
  /** 生成第一页 on the live API: the wiki controller writes the page (POST paths/{id}/next + organize) and opens it. */
  writeFirstPage?(t: { pathId: string | null; pageId: string | null; title: string; goal: string; key?: string }, onPage?: (pageId: string) => void): Promise<void>;
  /** Opens a written wiki page (读懂这一篇: a unit read already, a 先补 chip's page). Absent: writeFirstPage opens it. */
  openPage?(pageId: string): void;
  /**
   * The wiki controller's page writes as they stream (outline, each section done, the end): the page cards fill with them.
   * Returns the remover. Absent: only GET /wiki/writes (runs written in the background) fills them.
   */
  writeProgress?(fn: (p: StreamProgress) => void): () => void;
  /** The page about to open comes from this goal (its note on the page). */
  pageFromGoal?(pageId: string, mapId: string): void;
  /** Opens a page (a page card's title once something of it is written). Absent: the titles are plain text. */
  openPage?(pageId: string): void;
  /**
   * GET /wiki/progress as the wiki last read it (null before it lands): the 学习路径 rows mark the blocks begun (学习中 ·
   * a / n 页, 评估中) and done (已学完). `onProgress`: told when a new read lands. Absent: no marks.
   */
  progress?(): ProgressOut | null;
  onProgress?(fn: () => void): () => void;
  /** Why nothing can be sent now (offline, not set up), or null: 开始 says it at once. */
  blocked?(): string | null;
  /** The node graph (GET /wiki/graph, as 知识图谱 reads it): the existing map the goal grows out of. Absent: none. */
  graph?(): GraphSource;
  /** Whether the wiki is set up (an unset plugin reads nothing). Absent: yes. */
  configured?(): boolean;
  /** A page got a new title: its note follows. Absent: no note here (the fixture). */
  pageRenamed?(pageId: string): void;
  /** 上传附件: 参数 · 资料's upload and its source routes. Absent: no 上传附件 on the goal card. */
  sources?: LearnSources;
  /**
   * The 助手 (chat-first blocks): 开始 sends the goal there in a new conversation (the 助手 tab opens) and the view follows
   * that conversation's goal_new rows. Absent: 开始 says 助手没连上 (a goal is made only in the chat).
   */
  assistant?: LearnAssistant;
}

/** What 开始 needs from the assistant (main.ts: the AssistantController). */
export interface LearnAssistant {
  /**
   * Opens the 助手 tab and sends `text` as the first message of a new conversation, carrying `learn` (the server runs
   * new_goal with it; null: it could not).
   */
  start(text: string, learn: { goal: string; background: string },
    materials?: readonly { sourceId: string; title: string; kind: string; pages: number | null }[]): ChatStart | null;
  /** Every goal_new row the chat shows or changes; returns the unsubscribe. */
  onGoalRow(fn: (row: GoalChatRow) => void): () => void;
  /**
   * The goal the chat drives (one flow: its diagnosis, the blocks entered, the pages written are all in the 助手): the run of
   * the goal_new row `actionId`, else the current one (the last started here, or restored after a reload). Null: none.
   */
  run(actionId?: string): LearnRun | null;
  /** After a reload (no current run): the goal GET /wiki/progress says the learner is on, restored there. Null: none. */
  resume?(): Promise<LearnRun | null>;
  /** Restore a specific saved goal from the server, without starting a new goal or writing a page. */
  openGoal?(mapId: string): Promise<LearnRun | null>;
  /** The saved goal conversation's unconfirmed proposal, before a GoalRun exists. */
  pending?(): Promise<PendingGoal | null>;
  showGoal?(actionId: string): void;
  /**
   * The waiting goal_new draft's rows, edited from 学点新的's list (the same calls the chat card made: each edit kept on
   * the row with POST …/draft), and its 确认 / 取消. Absent: the list is read-only and has no 确认.
   */
  draft?: LearnDraftOps;
  /** Where the chat's diagnosis is (the 摸底 grid's taps, the block being asked), or null; `onDiag` tells of changes. */
  diag?(): LearnDiag | null;
  onDiag?(fn: () => void): () => void;
}

/** A goal_new draft's rows, by its row id (AssistantController's moveDraft / removeDraft / addDraft / renameDraft / …). */
export interface LearnDraftOps {
  move(actionId: string, from: number, to: number): void;
  remove(actionId: string, k: number): void;
  add(actionId: string, title: string): void;
  rename(actionId: string, k: number, title: string): void;
  confirm(actionId: string): Promise<void>;
  cancel(actionId: string): Promise<void>;
}

/** The chat's diagnosis as 学点新的 shows it: the block whose question is open (正在问), the 摸底 grid's taps not yet sent. */
/** `open`: the grid still takes taps (they show over the map); sent, a mark shows only where the map has no band yet. */
export interface LearnDiag { asking: string | null; marks: Readonly<Record<string, RoughMark>>; open?: boolean }
export type { RoughMark } from "../../generated/assistant";
import type { RoughMark } from "../../generated/assistant";

/** A goal driven in the chat (assistant goal-run.ts): 学点新的 shows its flow and asks it to enter a block. */
export interface LearnRun { readonly actionId: string; readonly flow: LearnFlow; enterBlock(region: string | null): Promise<void> }

/** What the goal card's material needs (main.ts: the wiki controller's SourcesController; one upload path for both). */
export interface LearnSources {
  /**
   * Uploads one file from the computer and saves its original under `<notes>/资料/`. Rejects with SourceRefused (the file
   * itself) or SourceBlocked (the plugin's line for a state: offline, not set up, busy …).
   */
  upload(file: File): Promise<SourceOut>;
  /** Follows the source being read; returns stop. */
  follow(id: string, onUpdate: (u: SourceUpdate) => void): () => void;
  /** The source with its sections (the 目录). */
  detail(id: string): Promise<SourceDetail>;
  /** PATCH …/sections: rename / relevel; answers the source with its sections. */
  editSections(id: string, edits: SectionEditIn[]): Promise<SourceDetail>;
}

/** The material on the goal card: `id` is "" until the upload answers. Answers for a card no longer shown are dropped. */
interface MatCard { a: Attached; stop: (() => void) | null; open: boolean; toc: TocRow[] | null; edit: number; editSeq: number }

/** 目录 rows are indented this much per level (the demo's r.lv * 16). */
const TOC_INDENT_PX = 16;

const NO_K: Scene = { order: [], nodes: new Map(), names: new Map(), l1: [], l2: [] };

/** The picture's least height stacked over the list, and what each group of it needs. */

/** How long after 开始学习 the path is read again for the page the run starts. */
const ENTERED_WATCH_MS = 20_000;

export class LearnView extends ItemView {
  private flow: LearnFlow | null = null;
  /** The chat's goal run shown (its flow is the run's, never disposed here); null: this view's own flow (the form, the blocks). */
  private run: LearnRun | null = null;
  /** Pages streamed that are not this block's (another page written meanwhile): no path read for them. */
  private pgOther = new Set<string>();
  /** A block's page cards: what /wiki/writes said per page, which pages were asked, the next read (page-progress.ts). */
  private enteredAt = 0;
  private pgTimer: number | null = null;
  /** Pages the wiki controller is streaming (or streamed) here: their own sections done / all, ahead of /wiki/writes. */
  private pgStream = new Map<string, StreamProgress>();
  /** The highest fill each page showed while written (a writer pass that starts over must not move the bar back). */
  private pgBest = new Map<string, number>();
  private pgUnsub: (() => void) | null = null;
  private unsub: (() => void) | null = null;
  private cloud: GraphCloudHandle | null = null;
  private askFramed: string | null = null;
  /** What the cloud draws now, and what it was drawn from. */
  private scene: LearnScene | null = null;
  private drawnFrom: { k: Scene | null; map: GoalMapOut | null; tint: string; places: GraphPlacesOut | null } | null = null;
  private places: GraphPlacesOut | null = null;
  private placesKey = "";
  /** The goal map and how many of its points the camera last framed (a new or grown map is framed again). */
  private framed: { id: string; n: number } | null = null;
  private readonly cleanups: (() => void)[] = [];
  private C: CloudColors | null = null;
  private pendingTopic: string | null = null;
  private pendingNew = false;
  /** The form 新建目标 asked for: it stays until he leaves it. */
  private newAsked: LearnFlow | null = null;
  /** A run asked for before the view was built (showRun). */
  private pendingRun: LearnRun | null = null;
  private pendingMap: string | null = null;
  private goalOpen = 0;
  private goalText = "";
  /** The goal as 开始 sent it, kept for the done card once the chat's run drives the view (null: the flow's own). */
  private shownGoal: string | null = null;
  private bgText = "";
  private lastEntered: string | null = null;
  private store: GraphStore | null = null;
  private storeSource: GraphSource | null = null;
  private unsubStore: (() => void) | null = null;
  /** The existing map the cloud draws (null while it is being read). */
  private kData: Scene | null = null;
  /** The graph kData was built from. */
  private kFrom: object | null = null;
  private els: { root: HTMLElement; panel: HTMLElement; tip: HTMLElement; empty: HTMLElement } | null = null;
  private mat: MatCard | null = null;
  /** The goal card's material slot and 开始 (redrawn in place by a progress tick). */
  private matSlot: HTMLElement | null = null;
  private startBtn: HTMLButtonElement | null = null;
  private overLine: HTMLElement | null = null;
  /** The ⋯ menu that is open (`b:<region>` / `p:<row key>`), the row being renamed, the row being dragged. */
  private menuKey: string | null = null;
  /** Stops placing the open menu (it is drawn again by every render); the focus goes into it when it was just opened. */
  private menuStop: (() => void) | null = null;
  private menuFocus = false;
  private editKey: string | null = null;
  /** The drag-reorder instances on the panel's lists (destroyed when the panel is drawn again or closed). */
  private readonly sorts: (() => void)[] = [];
  /** 加一块 / 加一页: what is typed (kept across redraws). */
  private addText = { b: "", p: "" };
  /** The chat's draft drawn as a map (one object per draft, so the cloud stages only new blocks). */
  /** The open branch of 学习路径 folded at this node (its row 收起); null: open down to the entered node. */
  private collapsedAt: string | null = null;
  private draftDrawn: { blocks: readonly DraftBlock[]; map: GoalMapOut } | null = null;
  /** 读懂这一篇: the unit whose 先去学 / 直接读 hint is shown (its uses are not learned yet), null: none. */
  private hintUnit: string | null = null;

  constructor(leaf: WorkspaceLeaf, private readonly host: LearnHost) { super(leaf); }

  getViewType(): string { return LEARN_VIEW_TYPE; }
  getDisplayText(): string { return LEARN_COPY.title; }
  override getIcon(): string { return "sparkles"; }

  get state(): LearnFlow | null { return this.flow; }
  /** The cloud (tests). */
  get cloudHandle(): GraphCloudHandle | null { return this.cloud; }
  /** What the cloud draws now (tests). */
  get cloudScene(): LearnScene | null { return this.scene; }

  override onOpen(): Promise<void> {
    this.pgUnsub?.();
    this.pgUnsub = this.host.writeProgress?.((p) => { this.pgStream.set(p.pageId, p); this.paintPages(); this.schedulePages(); }) ?? null;
    // a new progress read: the path card's block marks (not while a menu or a rename box is open)
    let seen = this.host.progress?.() ?? null;
    const offProgress = this.host.onProgress?.(() => {
      const now = this.host.progress?.() ?? null;
      if (now === seen) return;
      seen = now;
      if (this.menuKey === null && this.editKey === null) this.render();
    });
    if (offProgress) this.cleanups.push(offProgress);
    const root = el("div", "learn"); root.className = "ew ew-learn";
    const app = el("div", "app");
    const panel = el("div", "panel");
    const wrap = el("div", "cloudwrap");
    const box = el("div", "sigma");
    const tip = el("div", "tip"); tip.hidden = true;
    const empty = el("div", "cloud-empty", () => (LEARN_COPY.empty)); empty.hidden = true;
    const legend = el("div", "legend");
    const item = (band: "known" | "shaky" | "gap", label: string): HTMLElement => {
      const sp = el("span"); const i = el("i"); i.style.background = `var(--${band})`; sp.append(i, label); return sp;
    };
    legend.append(item("known", LEARN_COPY.legendKnown), item("shaky", LEARN_COPY.legendShaky), item("gap", LEARN_COPY.legendGap));
    wrap.append(box, empty, tip, legend);
    app.append(panel, wrap);
    root.append(app);
    this.contentEl.replaceChildren(root);
    this.els = { root, panel, tip, empty };
    this.C = readColors(root);
    this.cloud = GraphCloud(box, {
      tokens: root,
      font: () => getComputedStyle(root).fontFamily,
      reduced: () => matchMedia("(prefers-reduced-motion: reduce)").matches,
      onSelect: (id) => { const r = id === null ? undefined : this.scene?.goalOf.get(id); if (r !== undefined) this.enterRegion(r); },
      onGroup: (id) => this.enterRegion(this.scene?.goalOf.get(id) ?? id),
      onHover: (h) => { this.hover(h); },
      // the grow demo: the goal's blocks and points grow in, every name written, a block's name bold and clear of others
      grows: (id) => this.scene?.goalOf.has(id) === true,
      semantic: true, washByBand: true,
      pinned: (id) => this.scene?.goalOf.get(id) === realId(id),
      canClick: (id) => { const r = this.scene?.goalOf.get(id); const n = r === undefined ? undefined : nodeOf(this.flow?.state.map ?? null, r); return n !== undefined && this.enterable(n); },
    });
    this.cleanups.push(this.host.onCssChange(() => { this.recolor(); }));
    const offDiag = this.host.assistant?.onDiag?.(() => { if (this.menuKey === null && this.editKey === null) this.render(); else this.syncCloud(); });
    if (offDiag) this.cleanups.push(offDiag);
    if (this.host.assistant) {
      const a = this.host.assistant;
      this.cleanups.push(a.onGoalRow((row) => {
        const f = this.flow;
        // 确认: the goal goes on in the chat (one flow); this view shows that run
        if (this.run === null && f !== null && row.phase === "live" && row.actionId === f.state.actionId && row.key === f.state.chat) {
          const run = a.run(row.actionId);
          if (run !== null) { this.attachRun(run); return; }
        }
        // a goal confirmed on its card in the chat (asked for there, or a draft read back): this view goes to it
        if (row.phase === "live" && row.result !== null && this.run?.actionId !== row.actionId) {
          const run = a.run(row.actionId);
          if (run !== null) { this.shownGoal = null; this.attachRun(run); return; }
        }
        f?.goalRow(row);
      }));
    }
    // the demo: a click outside the menu closes it; Escape closes the menu or the rename box
    const doc = root.ownerDocument;
    const onClick = (e: MouseEvent): void => {
      if (this.menuKey === null) return;
      const t = e.target;
      if (t instanceof Element && t.closest(".ew-menu, .ew-more") !== null) return;
      this.menuKey = null; this.render();
    };
    const onKey = (e: KeyboardEvent): void => {
      if (e.key !== "Escape" || (this.menuKey === null && this.editKey === null)) return;
      this.menuKey = null; this.editKey = null; this.render();
    };
    doc.addEventListener("click", onClick); doc.addEventListener("keydown", onKey);
    this.cleanups.push(() => { doc.removeEventListener("click", onClick); doc.removeEventListener("keydown", onKey); });
    this.bindStore();
    if (this.store && (this.host.configured?.() ?? true)) void this.store.refresh();
    const topic = this.pendingTopic ?? "";
    this.pendingTopic = null;
    const asked = this.pendingRun, wantsNew = this.pendingNew, mapId = this.pendingMap;
    this.pendingRun = null; this.pendingNew = false;
    if (mapId !== null) return this.showGoal(mapId);
    // reopened (or after a reload): the goal the chat drives, as the server has it; a new topic starts a new goal
    const run = asked ?? (topic === "" && !wantsNew ? this.host.assistant?.run() ?? null : null);
    if (run !== null) this.attachRun(run);
    else {
      this.fresh(topic);
      // after a reload: the goal the server has the learner on, unless something was typed or started meanwhile
      const fresh = this.flow;
      if (topic === "" && !wantsNew) void this.restoreGoal(fresh);
    }
    return Promise.resolve();
  }

  /** A chat turn ended: it may have edited this path (edit_path_blocks), so the map is read again. */
  refreshShownMap(): void { void this.flow?.refreshMap(); }

  private async restoreGoal(fresh: LearnFlow | null): Promise<void> {
    const goal = this.goalText, background = this.bgText, material = this.mat;
    const unchanged = (): boolean => this.run === null && this.flow === fresh && fresh !== this.newAsked
      && fresh !== null && fresh.state.stage === "goal" && fresh.state.goal === "" && !fresh.state.busy
      && this.goalText === goal && this.bgText === background && this.mat === material;
    const pending = await this.host.assistant?.pending?.();
    if (!unchanged()) return;
    if (pending != null) {
      this.goalText = pending.goal; this.bgText = "";
      fresh?.restorePending(pending.row, pending.goal);
      this.host.assistant?.showGoal?.(pending.row.actionId);
      return;
    }
    const run = await this.host.assistant?.resume?.();
    if (run != null && unchanged()) this.attachRun(run);
  }

  /** Shows the chat's goal run: its flow (shared, not owned) drives this view from now on. */
  private attachRun(run: LearnRun): void {
    this.unsub?.();
    // 开始 here then 确认: the done card keeps the goal as he typed it (the run's flow names the card's title)
    this.shownGoal = this.run === null && this.flow?.state.actionId === run.actionId && this.flow.state.goal !== "" ? this.flow.state.goal : null;
    if (this.run === null) this.flow?.dispose();
    this.run = run; this.flow = run.flow;
    this.menuKey = null; this.editKey = null;
    this.unsub = run.flow.subscribe(() => { this.render(); });
    if (run.flow.state.map === null) void run.flow.refreshMap();
    this.render();
    this.app?.workspace.requestSaveLayout?.();
  }

  override onClose(): Promise<void> {
    if (this.pgTimer !== null) { window.clearTimeout(this.pgTimer); this.pgTimer = null; }
    this.pgUnsub?.(); this.pgUnsub = null;
    this.pgStream.clear(); // A closed view cannot observe the stream ending.
    for (const stop of this.sorts.splice(0)) stop();
    this.menuStop?.(); this.menuStop = null;
    this.cloud?.destroy(); this.cloud = null;
    for (const c of this.cleanups.splice(0)) c();
    this.unsubStore?.(); this.unsubStore = null; this.store?.dispose(); this.store = null; this.storeSource = null;
    this.unsub?.(); this.unsub = null;
    if (this.run === null) this.flow?.dispose();
    this.flow = null; this.run = null;
    this.dropMat();
    this.els = null;
    return Promise.resolve();
  }

  /** Opened by 想学点新的 · 开始 / a recommendation / the command: a different topic starts a fresh flow; none keeps the flow. */
  /** Not `open`: Obsidian's View.open(containerEl) is its own lifecycle method. */
  /** 查看详情 of a goal card: this view shows that goal's run (its blocks, its path). */
  get shownRunId(): string | null { return this.run?.actionId ?? null; }

  override getState(): Record<string, unknown> { return { mapId: this.pendingMap ?? this.flow?.state.map?.map_id ?? null }; }

  override async setState(state: unknown, result: ViewStateResult): Promise<void> {
    const mapId = (state as { mapId?: unknown } | null)?.mapId;
    if (typeof mapId === "string" && mapId !== "") this.pendingMap = mapId;
    await super.setState(state, result);
    if (this.els !== null && this.pendingMap !== null) await this.showGoal(this.pendingMap);
  }

  /** A saved goal always opens the existing learning-path view, including after the chat run has left memory. */
  async showGoal(mapId: string): Promise<void> {
    this.pendingMap = mapId;
    if (this.els === null) return;
    const version = ++this.goalOpen;
    try {
      const run = await this.host.assistant?.openGoal?.(mapId);
      if (version !== this.goalOpen || this.els === null) return;
      if (run === null || run === undefined) { ewNotice(LEARN_COPY.errGoalLost); return; }
      this.pendingMap = null;
      this.attachRun(run);
    } catch (err) {
      if (version === this.goalOpen && this.els !== null) ewNotice(errorText(err));
    }
  }

  showRun(run: LearnRun): void {
    this.goalOpen++; this.pendingMap = null;
    if (this.els === null) { this.pendingRun = run; return; }
    if (this.run !== run) this.attachRun(run);
  }

  /** 新建目标 (the command, 学点新的 with nothing typed, the goal card's button): the empty form, whatever goal is showing. A form he has started stays; the goal it left goes on in the chat. */
  newGoal(): void {
    this.goalOpen++; this.pendingMap = null;
    if (this.els === null) { this.pendingNew = true; return; }
    const S = this.flow?.state;
    if (!S || this.run !== null || S.stage !== "goal") this.fresh("");
    this.newAsked = this.flow;       // a restore still on its way does not take its place
  }

  openTopic(topic: string): void {
    this.goalOpen++; this.pendingMap = null;
    const t = topic.trim();
    if (this.els === null) { this.pendingTopic = t; return; }
    if (this.flow === null || (t !== "" && (this.run !== null || t !== this.flow.state.topic || this.flow.state.stage !== "goal"))) this.fresh(t);
  }

  /** Follows the host's graph source (the wiki controller may have been rebuilt) and keeps kData in step with it. */
  private bindStore(): void {
    const source = this.host.graph?.() ?? null;
    if (source === this.storeSource && (source === null || this.unsubStore !== null)) { this.updateK(); return; }
    this.unsubStore?.(); this.unsubStore = null; this.store?.dispose();
    this.storeSource = source;
    this.store = source ? new GraphStore(source) : null;
    if (this.store) this.unsubStore = this.store.subscribe(() => { this.updateK(); });
    this.updateK();
  }

  /** The existing map: the 知识图谱's graph once read; none when there is none / it failed / the wiki is not set up. */
  private updateK(): void {
    const st = this.store?.state;
    if (!this.store || !st || !(this.host.configured?.() ?? true)) this.kData = NO_K;
    else if (st.graph !== null) { if (st.graph !== this.kFrom) { this.kFrom = st.graph; this.kData = buildScene(st.graph); } }
    else this.kData = st.status === "error" ? NO_K : st.status === "ready" ? NO_K : null;
    this.syncCloud();
  }

  /** The existing map the cloud draws now (tests). */
  get existing(): Scene | null { return this.kData; }

  /**
   * The cloud follows the existing map and the goal map: a new scene when either changed; the camera frames the goal's
   * points when the goal map is new or grew, and goes back to the whole map when there is none; the region being learned
   * has its names written; with nothing to draw (K read and empty, no goal), the empty line.
   */
  private syncCloud(): void {
    const els = this.els, cloud = this.cloud;
    if (els === null || cloud === null) return;
    const k = this.kData, requested = this.flow ? this.cloudMap() : null;
    // New blocks enter the picture at their canonical places, never in a temporary row.
    const map = this.askPlaces(requested) ? requested : null;
    // a block drawn as its own point takes its row's colour (a 摸底 tap shows there at once, then fades with the map)
    const tint = new Map(map ? topRegions(map).map((r) => [r.id, TINT[this.blockState(r, false)]] as const) : []);
    const tintKey = JSON.stringify([...tint]);
    if (this.drawnFrom?.k !== k || this.drawnFrom.map !== map || this.drawnFrom.tint !== tintKey || this.drawnFrom.places !== this.places) {
      this.drawnFrom = { k, map, tint: tintKey, places: this.places };
      this.scene = learnScene(k, map, this.places, this.goalTitle(map));
      // Only the region point follows its self-assessment; actual children have their own bands.
      for (const [id, r] of this.scene.goalOf) { const band = tint.get(r), n = this.scene.scene.nodes.get(id); if (band !== undefined && n !== undefined && realId(id) === r) n.band = band; }
      cloud.setScene(this.scene.scene);
      const goal = [...this.scene.goalDots], id = map?.map_id ?? "";
      if (goal.length === 0) { if (this.framed !== null) cloud.fit(); this.framed = null; }
      // the goal's new points: the map stays as it is, the camera goes there
      else {
        // another goal: the camera is the view's again
        if (this.framed?.id !== id) cloud.fit();
        // each new scene: the server's places can land after the first, and move what is framed
        if (this.askFramed === null) cloud.frame(goal, this.flow?.state.entered ? "block" : "top");
        this.framed = { id, n: goal.length };
      }
    }
    els.empty.hidden = requested !== null || !(k !== null && this.scene!.scene.order.length === 0);
    const learning = new Set(map ? regionsOf(map).filter((r) => r.state === "expanded").map((r) => r.id) : []);
    const entered = this.flow?.state.entered?.region;
    if (entered) learning.add(entered);
    const lit = new Set([...this.scene!.goalOf].filter(([, r]) => learning.has(r)).map(([id]) => id));
    // the block being asked in the chat pulses (its own point, or its group's points)
    const asking = this.asking();
    const mine = asking === null ? [] : [...this.scene!.goalOf].filter(([id, r]) => r === asking && this.scene!.goalDots.has(id)).map(([id]) => id);
    const pulse = new Set(mine.includes(asking!) ? [asking!] : mine.slice(0, 1));
    // the camera goes to the block being asked, at the level where a block and its points show
    if (asking !== this.askFramed) { this.askFramed = asking; cloud.frame(mine.length > 0 ? mine : [...this.scene!.goalDots], mine.length > 0 ? "block" : "top", mine.length > 0); }
    // the goal framed: the rest of the map stands back, its names give way
    cloud.setState({ sel: null, focus: this.scene!.goalDots.size > 0 ? this.scene!.goalDots : null, lit, pulse });
  }

  /** The intake's summarized title, carried on both proposal and saved maps. */
  private goalTitle(map: GoalMapOut | null): string {
    return map?.title.trim() ?? "";
  }

  /** Read canonical places once per goal/block set; keep the existing map while they are pending or failed. */
  private askPlaces(map: GoalMapOut | null): boolean {
    const api = this.flow ? this.host.api() : null;
    if (!map) { this.placesKey = ""; this.places = null; return true; }
    if (!api?.graphPlaces) return true;
    const blocks = topRegions(map).map((r) => ({ id: r.id, name: r.name, scope: r.scope }));
    if (blocks.length === 0) return true;
    const key = JSON.stringify([map.map_id, blocks]);
    if (key === this.placesKey) return this.places !== null;
    this.placesKey = key;
    this.places = null;
    void api.graphPlaces(blocks).then((p) => {
      if (this.placesKey !== key) return;
      this.places = p;
      this.syncCloud();
    }, () => {
      if (this.placesKey !== key) return;
      ewNotice(LEARN_COPY.graphPlacesFailed, { label: GRAPH_COPY.rowRetry, run: () => {
        if (this.placesKey !== key) return;
        this.placesKey = "";
        this.syncCloud();
      } });
    });
    return false;
  }

  /** A click on a goal region (its name or one of its points) enters it when it can be entered. */
  private fromThisGoal(pageId: string): void {
    const m = this.flow?.state.map;
    if (m) this.host.pageFromGoal?.(pageId, m.map_id);
  }

  private enterRegion(id: string): boolean {
    const r = nodeOf(this.flow?.state.map ?? null, id);
    if (r === undefined || !this.enterable(r)) return false;
    this.enter(r.id);                       // as its row's 开始学习 (the chat's run writes its first page)
    return true;
  }

  private fresh(topic: string): void {
    this.unsub?.();
    if (this.run === null) this.flow?.dispose();
    this.run = null;
    if (this.els) {
      const before = this.store;
      this.bindStore();
      if (this.store !== before && this.store && (this.host.configured?.() ?? true)) void this.store.refresh();
    }
    const api = this.host.api();
    const host = this.host;
    this.flow = new LearnFlow(api, {
      fail: (text) => { ewNotice(text); },
      ...(host.writeFirstPage ? { writeFirstPage: (t) => host.writeFirstPage!(t) } : {}),
      ...(host.assistant ? { startChat: (text, learn, materials) => host.assistant!.start(text, learn, materials) } : {}),
      ...(host.blocked ? { blocked: () => host.blocked!() } : {}),
    }, topic);
    this.goalText = topic !== "" ? topic : api.live ? "" : LEARN_COPY.demoGoal;
    // the screenshot fixture: a demo topic's own background (big-goal-demo.html TOPICS[…].bg), as the demo prefills it
    this.bgText = !api.live && this.goalText !== "" ? TOPICS[pickTopic(this.goalText)]?.bg ?? "" : "";
    this.menuKey = null; this.editKey = null; this.addText = { b: "", p: "" };
    this.shownGoal = null;
    this.dropMat();
    this.lastEntered = null;
    this.unsub = this.flow.subscribe(() => { this.render(); });
    if (this.els) this.els.tip.hidden = true;
    this.render();
  }

  private recolor(): void {
    if (!this.els) return;
    this.C = readColors(this.els.root);
    this.cloud?.recolor();
    this.render();
  }

  /** What the cloud draws: the chat's draft while it is talked over, else the goal map. */
  private cloudMap(): GoalMapOut | null {
    const S = this.flow!.state;
    if (S.stage !== "blocks") return S.map;
    if (S.blocks.length === 0) return null;
    if (this.draftDrawn?.blocks !== S.blocks || this.draftDrawn.map.title !== S.title) this.draftDrawn = { blocks: S.blocks, map: draftMap(S.blocks, S.title) };
    return this.draftDrawn.map;
  }

  /**
   * 开始学习 a block, a sub-block or a point: the chat's run enters it (a point's diagnosis in the chat, its first page
   * written; a block only shows its parts); no run: this flow.
   */
  private enter(region: string): void {
    this.collapsedAt = null;
    this.enteredAt = Date.now();
    const entered = this.run !== null ? this.run.enterBlock(region) : this.flow?.enter(region);
    // a page another goal wrote fits here: it opens by itself, nothing is written (user ruling 2026-11-04)
    void entered?.then(() => {
      const e = this.flow?.state.entered;
      if (e?.region === region && e.path === null && e.pageId !== null) { this.fromThisGoal(e.pageId); this.host.openPage?.(e.pageId); }
    });
  }

  private enterable(_r: MapNodeOut): boolean {
    const st = this.flow?.state.stage;
    return st === "path" || st === "entered";
  }

  /** The hover tip: a goal region its name, word and scope; a point its name and band word; an existing region its word. */
  private hover(h: Hover | null): void {
    const tip = this.els?.tip, sc = this.scene?.scene;
    if (!tip || !sc) return;
    // titles a tip stood over come back
    for (const e of this.els?.root.querySelectorAll<HTMLElement>(".ew-gtitle") ?? []) e.style.visibility = "";
    if (h === null) { tip.hidden = true; return; }
    const map = this.flow ? this.cloudMap() : null;
    const region = (h.kind === "group" ? this.scene!.goalOf.get(h.id) ?? h.id : this.scene!.goalOf.get(h.id) === h.id ? h.id : null);
    const goalRegion = region === null || !map ? undefined : regionsOf(map).find((r) => r.id === region);
    tip.replaceChildren();
    // a point whose name stands beside it: the tip does not say it again
    const named = h.named === true;
    if (goalRegion) tip.append(...(named ? [] : [el("b", "", goalRegion.name + "　")]), labelNode(() => (LEARN_COPY.stateWord[this.blockState(goalRegion)])), el("div", "", goalRegion.scope));
    else if (h.kind === "group") {
      const g = sc.l1.find((x) => x.id === h.id) ?? sc.l2.find((x) => x.id === h.id);
      if (!g) { tip.hidden = true; return; }
      const ids = "all" in g ? g.all : sc.l1.filter((x) => x.parent === g.id).flatMap((x) => x.all);
      tip.append(el("b", "", g.name + "　"), regionWord(ids.map((id) => sc.nodes.get(id)!.band)));
    } else {
      const n = sc.nodes.get(h.id);
      if (!n) { tip.hidden = true; return; }
      if (!named) tip.append(el("b", "", n.name + "　"), labelNode(() => (BAND_WORD[n.band])));
      if (h.near.length) tip.append(el("div", "", () => (GRAPH_COPY.tipRelated(h.near.map((id) => sc.names.get(id) ?? id)))));
      if (tip.childNodes.length === 0) { tip.hidden = true; return; }
    }
    const wrap = this.els!.root.querySelector<HTMLElement>(".ew-cloudwrap"), w = wrap?.clientWidth ?? 0, H = wrap?.clientHeight ?? 0;
    tip.hidden = false;
    let { left, top } = tipSpot(h, tip.offsetWidth, tip.offsetHeight, w, H);
    // a block's tip goes beside its title and its names, where it covers no other title
    if (h.around && wrap) {
      const a = h.around, tw = tip.offsetWidth, th = tip.offsetHeight, c = wrap.getBoundingClientRect();
      const titles = [...wrap.querySelectorAll<HTMLElement>(".ew-gtitle")].filter((e) => e.style.display === "block").map((e) => e.getBoundingClientRect());
      const clear = ([x, y]: [number, number]): boolean => x >= 0 && y >= 0 && x + tw <= w && y + th <= H
        && !titles.some((r) => x < r.right - c.left && r.left - c.left < x + tw && y < r.bottom - c.top && r.top - c.top < y + th);
      const places: [number, number][] = [[a.x1 + 8, a.y0], [a.x0 - tw - 8, a.y0], [a.x0, a.y1 + 6], [a.x0, a.y0 - th - 6],
        [a.x1 + 8, Math.max(0, a.y1 - th)], [a.x0 - tw - 8, Math.max(0, a.y1 - th)]];
      const at = places.find(clear) ?? places.find(([x, y]) => x >= 0 && y >= 0 && x + tw <= w && y + th <= H) ?? [left, top];
      [left, top] = at;
      // a small box has no clear place: the titles under the card are hidden while it shows, no text shows through or beside it
      for (const e of wrap.querySelectorAll<HTMLElement>(".ew-gtitle")) {
        if (e.style.display !== "block") continue;
        const r = e.getBoundingClientRect();
        if (left < r.right - c.left && r.left - c.left < left + tw && top < r.bottom - c.top && r.top - c.top < top + th) e.style.visibility = "hidden";
      }
    }
    tip.style.left = left + "px"; tip.style.top = top + "px";
  }

  /* ---------- panel: the demo's render() ---------- */
  render(): void {
    for (const stop of this.sorts.splice(0)) stop();
    this.menuStop?.(); this.menuStop = null;
    const els = this.els, flow = this.flow;
    if (!els || !flow) return;
    const S = flow.state, panel = els.panel;
    panel.replaceChildren();
    panel.append(this.goalCard());
    if (S.stage === "thinking-map") panel.append(this.spinLine(S.chat !== null ? LEARN_COPY.thinkingBlocks(goalTopic(S.goal)) : this.thinkingMapLine(S.goal)));
    // the goal's blocks: one list (the chat's draft is edited here, 确认 starts the diagnosis in the chat), then the
    // block being learned with its pages
    if (S.stage === "blocks" && S.blocks.length > 0) panel.append(this.draftCard());
    if (S.stage !== "goal" && S.stage !== "thinking-map" && S.stage !== "blocks" && S.map) {
      const reading = S.map.reading ?? null;
      // 读懂这一篇 (reading-mode spec §5): 先补 (the prerequisites, rows as ever; none: no card), then 按原文读
      if (reading === null || topRegions(S.map).length > 0) panel.append(this.pathCard());
      if (reading !== null) panel.append(this.readingCard(reading));
    }
    // the entered node opens under its row; a goal without a map (or a node no row shows) keeps a card of its own
    if (S.entered && !this.inTree()) panel.append(this.enterCard());
    // What the entered node opened is brought into view when it appears and again when it fills in (its pages arrive after
    // the path is read; the demo's list is complete at once).
    const last = panel.querySelector<HTMLElement>("[data-entered]") ?? panel.lastElementChild as HTMLElement | null;
    const entered = S.entered ? `${S.entered.region}:${last?.textContent?.length ?? 0}` : null;
    if (entered !== null && entered !== this.lastEntered) {
      const reduce = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (last && typeof last.scrollIntoView === "function") last.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "nearest" });
    }
    this.lastEntered = entered;
    this.schedulePages();
    this.syncCloud();
  }

  /**
   * The entered block's page cards fill with their pages' writing: while a row's page is not read yet or is being written
   * (or 生成第一页 runs and the first page is not made yet: the path is read again to learn it), /wiki/writes is read every
   * PAGE_PROGRESS_POLL_MS and the titles are painted in place (no redraw: a drag or an open menu stays).
   */
  private schedulePages(): void {
    const e = this.flow?.state.entered;
    if (this.pgTimer !== null || !e || e.splitting || e.path === null) return;
    const ids = e.rows.flatMap((r) => (r.pageId !== null ? [r.pageId] : []));
    // a page the controller streams that is not on a row yet (生成第一页's POST next made it): the path names it
    const unnamed = [...this.pgStream.keys()].some((id) => !ids.includes(id) && !this.pgOther.has(id));
    const moving = e.rows.some((r) => rowPending(r) && (r.pageId === null || !this.pgStream.has(r.pageId)));
    // the chat's run starts the first page from its own flow: until this view's rows name it, it would read 开始生成
    const justEntered = Date.now() - this.enteredAt < ENTERED_WATCH_MS && e.rows.some((r) => r.pageId === null && r.state === "planned");
    if (!unnamed && !moving && !justEntered) return;
    this.pgTimer = window.setTimeout(() => { this.pgTimer = null; void this.readPages(); }, PAGE_PROGRESS_POLL_MS);
  }

  /** The entered path read again: its rows carry the server's state and writing progress (a re-render paints them). */
  private async readPages(): Promise<void> {
    const flow = this.flow;
    if (!flow) return;
    const read = await flow.readPath();
    if (this.flow !== flow) return;
    const known = new Set(flow.state.entered?.rows.flatMap((r) => (r.pageId !== null ? [r.pageId] : [])) ?? []);
    for (const id of this.pgStream.keys()) if (!known.has(id)) this.pgOther.add(id);   // not this block's page
    if (!read) this.schedulePages();                         // a failed read: asked again
  }

  /** A page card's fill: its stream here when there is one (streamProgress), else the row's server state; never backwards. */
  private pagePct(row: { pageId: string | null; state: PageRow["state"]; progress: PageRow["progress"] }): number {
    const st = row.pageId === null ? undefined : this.pgStream.get(row.pageId);
    const now = st !== undefined ? streamProgress(st) : rowProgress(row);
    if (row.pageId === null) return now;
    const best = Math.max(now, st === undefined && row.state !== "writing" ? now : this.pgBest.get(row.pageId) ?? 0);
    this.pgBest.set(row.pageId, best);
    return best;
  }

  /** The page cards' fills from the rows' state (and a stream here), in place: a drag or an open menu stays. */
  private paintPages(): void {
    const writing = [...this.pgStream.values()].some((p) => !p.ended);
    for (const button of this.els?.panel.querySelectorAll<HTMLButtonElement>("[data-action=write-page]") ?? []) button.disabled = writing;
    const rows = new Map((this.flow?.state.entered?.rows ?? []).flatMap((r) => (r.pageId !== null ? [[r.pageId, r] as const] : [])));
    for (const f of this.els?.panel.querySelectorAll<HTMLElement>("[data-pg]") ?? []) {
      const r = rows.get(f.dataset.pg ?? "");
      if (r !== undefined) paintFill(f, this.cardState(r), this.pagePct(r));
    }
    let changed = false;
    for (const s of this.els?.panel.querySelectorAll<HTMLElement>("[data-pct]") ?? []) {
      const r = rows.get(s.dataset.pct ?? "");
      if (r === undefined) continue;
      if (this.cardState(r) !== s.dataset.st) changed = true;          // began, written or stopped: its button changes
      else if (s.dataset.st === "writing") localizedText(s, () => (LEARN_COPY.pagePct(this.pagePct(r))));
    }
    if (changed && this.menuKey === null && this.editKey === null) this.render();
  }

  /** A card's state for its fill: the server's row state, else its stream here (a page streamed before the path names it). */
  private cardState(r: PageRow): PageRow["state"] {
    const st = r.pageId === null ? undefined : this.pgStream.get(r.pageId);
    if (st === undefined) return r.state;
    return st.ended ? (st.ok ? "written" : r.state) : "writing";
  }

  private dot(background: string): HTMLElement { const d = el("span", "dot"); d.style.background = background; return d; }

  private goalCard(): HTMLElement {
    const S = this.flow!.state;
    const c = el("div", "card" + (S.stage === "goal" ? "" : " done"));
    c.append(el("h2", "", () => (LEARN_COPY.goalTitle)));
    this.matSlot = null; this.startBtn = null; this.overLine = null;
    if (S.stage === "goal") {
      const g = el("label", "field"); g.append(labelNode(() => (LEARN_COPY.goalLabel)));
      const gi = el("input"); gi.type = "text"; gi.value = this.goalText; gi.oninput = () => { this.goalText = gi.value; this.syncStart(); }; g.append(gi);
      const b = el("label", "field"); b.append(labelNode(() => (LEARN_COPY.backgroundLabel)));
      const bi = el("textarea"); bi.value = this.bgText; bi.oninput = () => { this.bgText = bi.value; this.syncStart(); }; b.append(bi);
      // over the message's limit (goal + 背景, as the 助手 shows it): n/4000, as the chat's composer says it
      const over = el("div", "muted"); over.dataset.role = "over"; over.hidden = true; this.overLine = over;
      c.append(g, b, over);
      if (this.host.sources) this.attachArea(c);
      const go = btn(() => (LEARN_COPY.start), "primary", () => {
        const m = this.mat;
        if (m !== null && (m.a.id === "" || !canStart([m.a]))) return;
        void this.flow?.start(this.goalText, this.bgText, m !== null ? [m.a] : []);
      });
      this.startBtn = go; this.syncStart();
      // The demo's #goal-foot: 开始 in a flex column (gap 8px) under the form.
      const foot = el("div"); foot.id = "ew-goal-foot"; foot.style.cssText = "display:flex;flex-direction:column;gap:8px";
      foot.append(go);
      c.append(foot);
    } else {
      c.append(el("div", "goaltext", this.shownGoal ?? S.map?.goal ?? S.goal));
      if (this.mat) c.append(el("div", "faint", () => (LEARN_COPY.attached(this.mat!.a.title))));
      const again = btn(() => (LEARN_COPY.newGoal), "sm", () => { this.newGoal(); });
      again.dataset.action = "new-goal"; again.style.alignSelf = "flex-start";
      c.append(again);
    }
    return c;
  }

  /* ---------- 上传附件: the material card (full demo goalCard / drawMat) ---------- */

  /** 📎 上传附件 (a hidden file picker) or the material card; a file dropped on the card is the same as picking it. */
  private attachArea(c: HTMLElement): void {
    const pick = el("input"); pick.type = "file"; pick.hidden = true;
    pick.onchange = () => { const f = pick.files?.[0]; pick.value = ""; if (f) void this.attach(f); };
    c.append(pick);
    if (this.mat === null) {
      const at = el("button", "attach", () => (LEARN_COPY.attach)); at.type = "button";
      at.onclick = () => { pick.click(); };
      c.append(at);
    } else {
      const slot = el("div"); c.append(slot);
      this.matSlot = slot; this.drawMat();
    }
    const on = (e: Event): void => { e.preventDefault(); c.classList.add("ew-dragover"); };
    c.addEventListener("dragenter", on); c.addEventListener("dragover", on);
    c.addEventListener("dragleave", (e) => { if (!c.contains(e.relatedTarget as Node | null)) c.classList.remove("ew-dragover"); });
    c.addEventListener("drop", (e) => {
      e.preventDefault(); c.classList.remove("ew-dragover");
      const f = e.dataTransfer?.files?.[0];
      if (f) void this.attach(f);
    });
  }

  /** Uploads `f` (a new material replaces the one on the card); a refusal says 「这个文件打不开」 and the card is plain again. */
  private async attach(f: File): Promise<void> {
    const src = this.host.sources;
    if (!src || this.flow?.state.stage !== "goal") return;
    this.dropMat();
    const m: MatCard = { a: { id: "", title: f.name, pages: null, status: "reading", progress: null }, stop: null, open: false, toc: null, edit: -1, editSeq: 0 };
    this.mat = m;
    this.render();
    let out: SourceOut;
    try { out = await src.upload(f); } catch (e) {
      if (this.mat === m) this.failMat(attachErrorText(e));
      return;
    }
    if (this.mat !== m) return;
    m.a = { ...m.a, id: out.id, pages: out.pages, status: out.status, progress: out.progress };
    if (out.status === "failed") { this.failMat(); return; }
    if (out.status === "reading") m.stop = src.follow(out.id, (u) => { this.matUpdate(m, u); });
    this.refreshMat();
  }

  private matUpdate(m: MatCard, u: SourceUpdate): void {
    if (this.mat !== m) return;
    const was = m.a.status;
    m.a = { ...m.a, status: u.status, progress: u.progress };
    // A failed import is the file's own refusal; a lost connection says so in the plugin's words.
    if (u.status === "failed") { this.failMat(u.error === SOURCES_COPY.streamLost ? u.error : LEARN_COPY.attachFailed); return; }
    if (u.status !== was) this.refreshMat();
  }

  /** The material leaves the card with a notice: 「这个文件打不开」 for the file's own refusal, else the state's line. */
  private failMat(text: string = LEARN_COPY.attachFailed): void {
    this.dropMat();
    this.render();
    ewNotice(text);
  }

  /** The material leaves the card (✕, a new one, a fresh flow); its progress stream stops. The source stays in 资料. */
  private dropMat(): void {
    this.mat?.stop?.();
    this.mat = null;
  }

  /** A progress tick: only the material card and 开始 are redrawn. */
  private refreshMat(): void {
    if (this.matSlot?.isConnected) this.drawMat();
    this.syncStart();
  }

  private syncStart(): void {
    const m = this.mat;
    const over = startOver(this.goalText, this.bgText, m !== null ? [m.a] : []);
    if (this.overLine) { this.overLine.hidden = over === 0; localizedText(this.overLine, () => (over === 0 ? "" : LEARN_COPY.tooLong(over))); }
    if (this.startBtn) this.startBtn.disabled = over > 0 || (m !== null && (m.a.id === "" || !canStart([m.a])));
  }

  private drawMat(): void {
    const slot = this.matSlot, m = this.mat;
    if (!slot || !m) return;
    slot.replaceChildren();
    const card = el("div", "mat"), t = el("div", "mt");
    const x = el("button", "x", "✕"); x.type = "button"; localizedAttribute(x, "aria-label", () => (LEARN_COPY.removeMaterial));
    x.onclick = () => { this.dropMat(); this.render(); };
    const read = m.a.id !== "" && canStart([m.a]);
    t.append(el("span", "mn", m.a.title), el("span", "ms", () => (read ? "" : LEARN_COPY.parsing)), x);
    card.append(t);
    if (read) {
      const tg = btn(() => (m.open ? LEARN_COPY.tocFold : LEARN_COPY.toc), "link", () => { void this.toggleToc(m); });
      tg.style.alignSelf = "flex-start"; tg.style.fontSize = "12.5px"; tg.setAttribute("aria-expanded", String(m.open));
      if (m.open && m.toc !== null) card.append(this.tocList(m));
      card.append(tg);
    }
    slot.append(card);
  }

  /** 目录 / 收起: the sections are read once, when first opened; a failed read folds it again with a notice. */
  private async toggleToc(m: MatCard): Promise<void> {
    m.open = !m.open; m.edit = -1;
    this.drawMat();
    if (!m.open || m.toc !== null || !this.host.sources) return;
    try {
      const d = await this.host.sources.detail(m.a.id);
      if (this.mat !== m) return;
      m.toc = tocOf(d);
    } catch (e) {
      if (this.mat !== m) return;
      m.open = false;
      ewNotice(tocErrorText(e));
    }
    this.drawMat();
  }

  private tocList(m: MatCard): HTMLElement {
    const ul = el("ul", "toc");
    m.toc!.forEach((r, i) => {
      const li = el("li"); li.style.paddingLeft = r.depth * TOC_INDENT_PX + "px";
      if (m.edit === i) {
        const inp = el("input"); inp.type = "text"; inp.value = r.title; localizedAttribute(inp, "aria-label", () => (LEARN_COPY.tocNameLabel));
        inp.style.flex = "1"; inp.style.minWidth = "0";
        let ended = false;
        const end = (save: boolean): void => {
          if (ended) return;
          ended = true; m.edit = -1;
          const v = inp.value.trim();
          if (save && v !== "" && v !== r.title) {
            const old = r.title;
            r.title = v;
            void this.sendEdit(m, { id: r.id, title: v }, () => { r.title = old; });
          }
          this.drawMat();
        };
        inp.onkeydown = (e) => { if (e.key === "Enter") end(true); if (e.key === "Escape") end(false); };
        inp.onblur = () => { end(true); };
        li.append(inp);
        window.setTimeout(() => { inp.focus(); }, 0);
      } else {
        const n = el("button", "tn", r.title); n.type = "button";
        n.onclick = () => { m.edit = i; this.drawMat(); };
        li.append(n);
      }
      const mv = (d: -1 | 1): HTMLButtonElement => {
        const b = el("button", "lv", d < 0 ? "←" : "→"); b.type = "button";
        localizedAttribute(b, "aria-label", () => (d < 0 ? LEARN_COPY.tocUp(r.title) : LEARN_COPY.tocDown(r.title)));
        b.disabled = !canShift(r, d);
        b.onclick = () => {
          const old = r.depth;
          r.depth = shiftDepth(r, d);
          this.drawMat();
          void this.sendEdit(m, { id: r.id, depth: r.depth }, () => { r.depth = old; });
        };
        return b;
      };
      if (r.page !== null) li.append(el("span", "pg", () => (LEARN_COPY.tocPage(r.page!))));
      li.append(mv(-1), mv(1));
      ul.append(li);
    });
    return ul;
  }

  /**
   * One 目录 edit, shown at once and sent; the server's sections replace the rows (only the latest answer is used). A
   * refusal undoes it with a notice. A row being renamed is never redrawn under the caret.
   */
  private async sendEdit(m: MatCard, edit: SectionEditIn, undo: () => void): Promise<void> {
    const src = this.host.sources;
    if (!src) return;
    const seq = ++m.editSeq;
    try {
      const d = await src.editSections(m.a.id, [edit]);
      if (this.mat !== m || seq !== m.editSeq) return;
      m.toc = tocOf(d);
    } catch (e) {
      if (this.mat !== m) return;
      undo();
      ewNotice(tocErrorText(e));
    }
    if (m.edit === -1) this.drawMat();
  }

  /* ---------- editable rows (big-goal-demo.html rowOps / dragRow / renameBox / addRow) ---------- */

  /** The demo's rowOps: `div.ops` (⋮⋮ grip, ⋯ button) and, while open, the `div.menu`. */
  private rowOps(key: string, label: string, items: [string, () => void][]): HTMLElement[] {
    const ops = el("div", "ops");
    const more = el("button", "more", () => (LEARN_COPY.more)); more.type = "button"; more.dataset.k = "more:" + key;
    localizedAttribute(more, "aria-label", () => (LEARN_COPY.moreLabel(label))); more.setAttribute("aria-haspopup", "menu");
    more.setAttribute("aria-expanded", String(this.menuKey === key));
    more.onclick = (e) => { e.stopPropagation(); this.menuFocus = this.menuKey !== key; this.menuKey = this.menuFocus ? key : null; this.render(); };
    ops.append(el("span", "grip", () => (LEARN_COPY.grip)), more);
    if (this.menuKey !== key) return [ops];
    const m = el("div", "menu"); m.setAttribute("role", "menu");
    // placed under the button by @floating-ui (flips above at the bottom of the pane); Esc / Tab put the focus back on it
    this.menuStop = openMenuAt(more, m, (refocus) => {
      this.menuKey = null; this.render();
      if (refocus) this.els?.panel.querySelector<HTMLElement>(`[data-k="more:${key}"]`)?.focus();
    }, this.menuFocus);
    this.menuFocus = false;
    for (const [t, fn] of items) {
      const b = el("button", "", t); b.type = "button"; b.setAttribute("role", "menuitem");
      b.onclick = (e) => { e.stopPropagation(); this.menuKey = null; this.render(); fn(); };
      m.append(b);
    }
    return [ops, m];
  }

  /** The demo's dragRow: a row that can be dragged (no dragging while a row is renamed) and focused for Alt + ↑ / ↓. */
  private dragRow(li: HTMLLIElement, list: "b" | "p", k: number): void {
    li.draggable = this.editKey === null; li.tabIndex = 0; li.dataset.k = `${list}:${k}`;
  }

  /**
   * The list's rows reorder by drag (SortableJS, ui/reorder.ts) or Alt + ↑ / ↓ (the focus follows the row): `move` changes
   * the flow's state, which draws the panel again. The instance is destroyed by the next render.
   */
  private reorder(ol: HTMLElement, list: "b" | "p", move: (from: number, to: number) => void): void {
    this.sorts.push(reorderable(ol, {
      disabled: this.editKey !== null,
      move: (from, to, byKey) => {
        move(from, to);
        if (byKey) this.els?.panel.querySelector<HTMLElement>(`[data-k="${list}:${to}"]`)?.focus();
      },
    }));
  }

  /** The demo's renameBox: `div.edit` > `div.row` (nowrap): the name, 保存, 取消; Enter saves. */
  private renameBox(value: string, save: (v: string) => void): HTMLElement {
    const box = el("div", "edit"), inp = el("input"); inp.type = "text"; inp.value = value; localizedAttribute(inp, "aria-label", () => (LEARN_COPY.nameLabel));
    const ok = (): void => { const v = inp.value.trim(); this.editKey = null; if (v) save(v); this.render(); };
    inp.onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) ok(); };
    const r = el("div", "row"); r.style.flexWrap = "nowrap";
    r.append(inp, btn(() => (LEARN_COPY.save), "primary", ok), btn(() => (LEARN_COPY.cancel), null, () => { this.editKey = null; this.render(); }));
    box.append(r);
    window.setTimeout(() => { inp.focus(); }, 0);
    return box;
  }

  /** The demo's addRow: `div.row.add` (nowrap): the box (placeholder = its label) and 加上; Enter adds. */
  private addRow(list: "b" | "p", ph: string, fn: (v: string) => Promise<boolean>): HTMLElement {
    const r = el("div", "row add"); r.style.flexWrap = "nowrap";
    const inp = el("input"); inp.type = "text"; inp.placeholder = ph; inp.setAttribute("aria-label", ph); inp.value = this.addText[list];
    inp.oninput = () => { this.addText[list] = inp.value; };
    const go = (): void => {
      const v = inp.value.trim();
      if (!v) return;
      void fn(v).then((ok) => { if (ok) { this.addText[list] = ""; this.render(); } });
    };
    inp.onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) go(); };
    r.append(inp, btn(() => (LEARN_COPY.addGo), null, go));
    return r;
  }

  /**
   * 学习路径 = the demo's pathCard: `ol.elist`, one row per block (dot, name + scope, 进入 or 已掌握 · 跳过, ⋮⋮ / ⋯ 改名 · 删除),
   * then 加一块 … 加上. Every change goes to POST /wiki/maps/{id}/edits; 删除 shows a notice with 撤销 (restore).
   */
  /**
   * One block row (big-goal-demo.html pathCard): the state is the dot, no word; the right column only ever holds 进入, and
   * takes no room without it; ⋮⋮ / ⋯ show on hover at the first line's right end.
   */
  private blockRow(k: number, name: string, level: string, scope: string, st: BlockState, right: HTMLElement): HTMLLIElement {
    const li = el("li");
    this.dragRow(li, "b", k);
    localizedAttribute(li, "aria-label", () => (LEARN_COPY.blockLabel(k + 1, name, LEARN_COPY.stateWord[st])));
    const nm = el("div", "rname", name);
    if (level) nm.append(el("span", "tg", () => goalLevelLabel(level)));
    const t = el("div"); t.append(nm, el("div", "scope", scope));
    li.append(this.stateDot(st), t, right);
    return li;
  }

  private stateDot(st: BlockState): HTMLElement {
    const d = this.dot(`var(--${st === "asking" ? "shaky" : st})`);
    if (st === "asking") d.classList.add("ew-asking");
    return d;
  }

  /** The block the chat's diagnosis is asking now (its question open), else null. */
  private asking(): string | null { return this.host.assistant?.diag?.()?.asking ?? null; }

  /** A block's state: 正在问 while its question is open, the 摸底 grid's tap, else its band on the map (还没问 without one). */
  private blockState(r: MapNodeOut, withAsking = true): BlockState {
    const d = this.host.assistant?.diag?.() ?? null;
    if (withAsking && d?.asking === r.id) return "asking";
    const m = d?.marks[r.id];
    const mark: BlockState | null = m === undefined ? null : m === "know" ? "known" : m === "partly" ? "shaky" : "gap";
    if (mark !== null && d?.open !== false) return mark;
    // 掌握 only as the server has it (state "known": every cell likely or known in K, the block the goal skips); a
    // block partly known is 熟悉 — so the row, the block the goal enters next and the chat's start line agree
    const b = bandOf(r);
    const learned = (r.mix.known ?? 0) + (r.mix.likely ?? 0) >= 0.999;     // entered again: K still has every cell
    const band: BlockState = r.state === "known" || learned ? "known" : b === "known" || b === "likely" || b === "shaky" ? "shaky"
      : b === "gap" ? "gap" : "none";
    return band === "none" && mark !== null ? (mark === "known" ? "shaky" : mark) : band;
  }

  /**
   * 开始 → the chat's draft as 学点新的's list (big-goal-demo.html pathCard while S.stage is "blocks"): drag / Alt + ↑↓, ⋯
   * 改名 · 删除, 添加学习主题 … 添加, then 确认 / 取消. Each edit is kept on the goal_new row (the chat card's calls).
   */
  private draftCard(): HTMLElement {
    const S = this.flow!.state, ops = this.host.assistant?.draft, id = S.actionId;
    const c = el("div", "card");
    c.append(el("h2", "", () => (LEARN_COPY.pathTitle)));
    const ol = el("ol", "elist blocks");
    S.blocks.forEach((b, k) => {
      if (this.editKey === "d:" + b.key) {
        const li = el("li"); this.dragRow(li, "b", k);
        li.append(this.stateDot("none"), this.renameBox(b.name, (v) => { if (ops && id) ops.rename(id, k, v); }));
        ol.append(li); return;
      }
      const li = this.blockRow(k, b.name, b.level ?? "", b.scope, "none", el("span", "act"));
      if (ops && id) li.append(...this.rowOps("d:" + b.key, b.name, [
        [LEARN_COPY.menuRename, () => { this.editKey = "d:" + b.key; this.render(); }],
        [LEARN_COPY.menuDelete, () => { ops.remove(id, k); ewNotice(LEARN_COPY.blockRemoved(b.name)); }],
      ]));
      else li.append(el("span"));
      ol.append(li);
    });
    if (ops && id) this.reorder(ol, "b", (from, to) => { ops.move(id, from, to); });
    c.append(ol);
    if (ops && id) {
      c.append(this.addRow("b", LEARN_COPY.addBlock, (v) => { ops.add(id, v); return Promise.resolve(true); }));
      const r = el("div", "row");
      r.append(btn(() => (LEARN_COPY.confirm), "primary", () => { void ops.confirm(id); }), btn(() => (LEARN_COPY.cancel), null, () => { void ops.cancel(id); }));
      c.append(r);
    }
    return c;
  }

  private pathCard(): HTMLElement {
    const flow = this.flow!, S = flow.state;
    const c = el("div", "card");
    const reading = S.map?.reading ?? null;
    c.append(el("h2", "", () => (reading !== null ? LEARN_COPY.prereqTitle : LEARN_COPY.pathTitle)));
    const ol = el("ol", "elist blocks");
    const rows = topRegions(S.map);
    // the buttons only once the asking is done (the demo's path / entered stages)
    const asked = (S.stage === "path" || S.stage === "entered") && S.draftId === null;
    const chain = this.openChain();
    rows.forEach((r, k) => {
      if (this.editKey === "b:" + r.id) {
        const li = el("li"); this.dragRow(li, "b", k);
        li.append(this.stateDot(this.blockState(r)), this.renameBox(r.name, (v) => { void flow.renameBlock(r.id, v); }));
        ol.append(li); return;
      }
      const right = el("span", "act");
      if (asked) right.append(this.nodeAct(r, chain));
      const st = this.blockState(r), li = this.blockRow(k, r.name, r.level, r.scope, st, right);
      if (chain.includes(r.id) || st === "asking") li.classList.add("ew-cur");
      if (asked) this.rowClick(li, r, chain);
      const sole = this.soleRow(r);
      const items: [string, () => void][] = [
        [LEARN_COPY.menuRename, () => { this.editKey = "b:" + r.id; this.render(); }],
        [LEARN_COPY.menuDelete, () => {
          void flow.dropBlock(r.id).then((undo) => {
            if (undo !== null) ewNotice(LEARN_COPY.blockRemoved(r.name), { label: LEARN_COPY.undo, run: () => { void flow.undoEdit(undo); } });
          });
        }],
      ];
      if (sole !== null && sole.canSplit && S.entered?.splittingRow === null) items.splice(1, 0, [LEARN_COPY.menuSplit, () => { void this.splitPage(sole); }]);
      li.append(...this.rowOps("b:" + r.id, r.name, items));
      ol.append(li);
      if (this.isOpen(r.id, chain)) ol.append(this.nest(r, chain));
    });
    this.reorder(ol, "b", (from, to) => { void flow.moveBlock(from, to); });
    c.append(ol, this.addRow("b", LEARN_COPY.addBlock, (v) => flow.addBlock(v)));
    return c;
  }

  /**
   * 按原文读 (reading-mode spec §5): the progress line, then one row per unit in the material's order — its K colour once
   * read (never 已掌握), title + location, the one-line about, 「先补：X」 for each use not learned yet (its page, else its
   * node), and 读这一段 (primary on the next unit) / 在读 · 继续读 / 读完 / 不读. A click on a row or its button reads it: a
   * unit whose uses are not learned yet first shows 这一段用到 X，你还没学：先去学 / 直接读 (a hint, never a block).
   */
  private readingCard(r: ReadingOut): HTMLElement {
    const S = this.flow!.state;
    const c = el("div", "card");
    c.dataset.reading = r.reading_id;
    const prog = el("div", "faint", progressLine(r)); prog.dataset.role = "progress";
    c.append(el("h2", "", () => (LEARN_COPY.readTitle)), prog);
    const next = nextUnit(r);
    const ol = el("ol", "elist units");
    for (const u of r.units) {
      const li = el("li"); li.dataset.unit = u.unit_id;
      if (u.status === "skipped") li.classList.add("ew-skipped");
      const nm = el("div", "rname", u.title);
      if (u.location) nm.append(el("span", "sec", u.location));
      if (u.uncertain) nm.append(el("span", "tg", () => (LEARN_COPY.unitUncertain)), qTip(() => (LEARN_COPY.unitUncertainWhy), () => (LEARN_COPY.unitUncertain)));
      if (u.status === "reading") nm.append(el("span", "tg", () => (LEARN_COPY.unitReading)));
      const t = el("div"); t.append(nm);
      if (u.about) t.append(el("div", "scope", u.about));
      const unknown = u.status === "done" || u.status === "skipped" ? [] : unknownUses(u);
      if (unknown.length > 0) {
        const chips = el("div", "chips");
        for (const p of unknown) {
          const b = btn(() => (LEARN_COPY.useChip(p.name)), "link", () => { this.goUse(p, r); });
          b.dataset.use = p.id; chips.append(b);
        }
        t.append(chips);
      }
      if (S.unitBusy === u.unit_id) t.append(this.spinLine(LEARN_COPY.unitOpening));
      else if (this.hintUnit === u.unit_id && unknown.length > 0) {
        const h = el("div", "faint", () => (LEARN_COPY.usesHint(unknown.map((p) => p.name))));
        h.dataset.hint = u.unit_id;
        h.append(btn(() => (LEARN_COPY.learnFirst), "link", () => { this.goUse(unknown[0]!, r); }),
          btn(() => (LEARN_COPY.readAnyway), "link", () => { void this.readUnit(u, true); }));
        t.append(h);
      }
      li.append(this.dot(rgb(colorOfMix(unitMix(u), this.C!))), t);
      if (u.status === "done") li.append(el("span", "chip skip", () => (LEARN_COPY.unitDone)));
      else if (u.status === "skipped") li.append(el("span", "chip later", () => (LEARN_COPY.unitSkipped)));
      else {
        li.append(btn(() => (u.status === "reading" ? LEARN_COPY.continueUnit : LEARN_COPY.readUnit), next?.unit_id === u.unit_id ? "primary" : null, () => { void this.readUnit(u, false); }));
      }
      // the whole row reads the unit too; its buttons do their own thing only
      if (u.status !== "skipped") li.onclick = (e) => { if (!(e.target instanceof HTMLElement && e.target.closest("button"))) void this.readUnit(u, false); };
      ol.append(li);
    }
    c.append(ol);
    return c;
  }

  /** A 先补 chip: the point's page, else the unit that teaches it (read as its row reads it), else its node on the map. */
  private goUse(p: ReadingPointOut, r: ReadingOut): void {
    const to = useTarget(p, r);
    if (to.kind === "node") { this.enter(to.nodeId); return; }
    if (to.kind === "unit") {
      const u = r.units.find((x) => x.unit_id === to.unitId);
      if (u) void this.readUnit(u, false);
      return;
    }
    this.fromThisGoal(to.pageId);
    if (this.host.openPage) this.host.openPage(to.pageId);
    else void this.host.writeFirstPage?.({ pathId: null, pageId: to.pageId, title: p.name, goal: this.flow?.state.goal ?? "" });
  }

  /**
   * Reads unit `u`: entered first when it was never looked at (its uses are known only then); uses not learned yet and not
   * `direct` → the 先去学 / 直接读 hint on its row; else its page is started (the open route) and opened — written first
   * when it has no body yet (the wiki controller writes and opens it).
   */
  private async readUnit(u: ReadingUnitOut, direct: boolean): Promise<void> {
    const flow = this.flow;
    if (!flow || u.status === "skipped") return;
    let unit = u;
    if (!direct && unit.page_id === null) {
      if (!unit.analyzed) {
        const out = await flow.openUnit(unit.unit_id, false);
        if (out === null || this.flow !== flow) return;
        unit = out.unit;
      }
      if (unknownUses(unit).length > 0) { this.hintUnit = unit.unit_id; this.render(); return; }
    }
    const out = await flow.openUnit(unit.unit_id, true);
    if (out === null || out.page_id === null || this.flow !== flow) return;
    if (this.hintUnit === unit.unit_id) { this.hintUnit = null; this.render(); }
    // 继续读 opens what is there (a page not written yet opens its 开始生成 tab): it never writes the page again
    if ((out.unit.status === "done" || u.status === "reading") && this.host.openPage) this.host.openPage(out.page_id);
    else {
      try {
        await this.host.writeFirstPage?.({ pathId: null, pageId: out.page_id, title: out.unit.title, goal: flow.state.goal });
      } catch (e) {
        console.warn("EvoWiki: a unit's page was not written", e);
        ewNotice(LEARN_COPY.unitNotWritten(out.unit.title));   // its row still says 继续读
      }
    }
  }

  /** Where each block is, as the server says it (GET /wiki/progress): the goal of this map's blocks by node id. */
  private blockStates(): Map<string, BlockOut> {
    const mapId = this.flow?.state.map?.map_id ?? null;
    const goal = mapId === null ? undefined : this.host.progress?.()?.goals.find((g) => g.goal_id === mapId);
    return new Map((goal?.blocks ?? []).map((x) => [x.region_id, x]));
  }

  /** 「正在完善…」: the goal text, else the attached material's name (its file name without the extension). */
  private thinkingMapLine(goal: string): string {
    const g = goal.trim();
    if (g) return LEARN_COPY.thinkingMap(g);
    const name = (this.mat?.a.title ?? "").replace(/\.[^./\\]{1,8}$/, "").trim();
    return name ? LEARN_COPY.thinkingMapMaterial(name) : LEARN_COPY.thinkingMapPlain;
  }

  /** graph.html spinLine: a wait is a spinner and one sentence. */
  private spinLine(text: string): HTMLElement {
    const d = el("div", "thinking"); d.append(el("i", "spin"), text); return d;
  }

  /** The entered node and the nodes above it, top first; empty when nothing is entered or it is not on the map. */
  private openChain(): string[] {
    const S = this.flow!.state, out: string[] = [];
    for (let n = S.entered ? nodeOf(S.map, S.entered.region) : undefined; n !== undefined; n = n.parent === null ? undefined : nodeOf(S.map, n.parent)) out.unshift(n.id);
    return out;
  }

  /** The entered node opens under a row of 学习路径 (its top block is one). */
  private inTree(): boolean {
    const S = this.flow!.state, top = this.openChain()[0];
    if (top === undefined || !topRegions(S.map).some((r) => r.id === top)) return false;
    return S.stage !== "goal" && S.stage !== "thinking-map" && S.stage !== "blocks";
  }

  /** This node of the open branch shows what is under it (a row folded with 收起, and all below it, does not). */
  private isOpen(id: string, chain: readonly string[]): boolean {
    const at = chain.indexOf(id);
    if (at < 0) return false;
    const fold = this.collapsedAt === null ? -1 : chain.indexOf(this.collapsedAt);
    return fold < 0 || at < fold;
  }

  /** The entered node's one page when it has the node's own name: that page is the node's row, no level of its own. */
  private soleRow(node: MapNodeOut): PageRow | null {
    const e = this.flow!.state.entered;
    if (e === null || e.region !== node.id || e.splitting || e.rows.length !== 1) return null;
    return e.rows[0]!.title === node.name ? e.rows[0]! : null;
  }

  /** A row's one button: 收起 on an open row, its page's button on a row that is its one page, else 开始学习 / 继续学习. */
  private nodeAct(node: MapNodeOut, chain: readonly string[]): HTMLElement {
    const sole = this.soleRow(node);
    if (sole !== null) return this.pageAct(sole);
    if (this.isOpen(node.id, chain)) return this.goBtn(LEARN_COPY.collapse, () => { this.toggle(node.id, chain); });
    if (this.blockStates().get(node.id)?.state === "done") return this.goBtn(LEARN_COPY.pageOpen, () => { this.pick(node.id, chain); });
    const begun = chain.includes(node.id) || node.path_id !== null || node.page_id !== null || (node.state === "expanded" && node.enter !== "learn");
    return this.goBtn(begun ? LEARN_COPY.continueBlock : LEARN_COPY.startLearn, () => { this.pick(node.id, chain); });
  }

  private goBtn(text: string, fn: () => void): HTMLElement {
    const b = el("button", "go", text); b.type = "button"; b.onclick = (e) => { e.stopPropagation(); fn(); };
    return b;
  }

  /** 开始学习 / 继续学习: a row of the open branch only opens again; any other is entered. */
  private pick(id: string, chain: readonly string[]): void {
    if (chain.includes(id)) { this.collapsedAt = null; this.render(); } else this.enter(id);
  }

  private toggle(id: string, chain: readonly string[]): void {
    if (this.isOpen(id, chain)) { this.collapsedAt = id; this.render(); } else this.pick(id, chain);
  }

  /** A click on a row's text does what its folder would: open it, or fold it when open. */
  private rowClick(li: HTMLElement, node: MapNodeOut, chain: readonly string[]): void {
    const text = li.children[1] as HTMLElement | undefined;
    if (text === undefined) return;
    text.classList.add("ew-rowhit");
    text.onclick = () => { this.toggle(node.id, chain); };
  }

  /**
   * What opens under a row of the branch: spec 2026-10-28's parts (each a row of the same kind, the next one down open under
   * its own row) or the entered node's pages; a node that is its one page shows only 添加页面.
   */
  private nest(node: MapNodeOut, chain: readonly string[]): HTMLElement {
    const S = this.flow!.state, e = S.entered!;
    const box = el("li", "nest");
    const body = el("div");
    box.append(body);
    if (e.region !== node.id) { body.append(this.partList(node, chain)); return box; }
    body.dataset.entered = node.id;
    if (e.splitting) { body.append(this.spinLine((e.checkingPage ? LEARN_COPY.checkingPage : e.intoParts ? LEARN_COPY.splittingParts : LEARN_COPY.splittingBlock)(e.name))); return box; }
    if (e.path === null && e.pageId === null && node.path_id === null && node.page_id === null) { body.append(this.partList(node, chain)); return box; }
    if (this.soleRow(node) === null) body.append(this.pageList());
    if (e.path !== null) body.append(this.addRow("p", LEARN_COPY.addPage, (v) => this.flow!.addPage(v)));
    // a point split further after its page was written: its parts under that page
    if (node.kind === "concept" && partsOf(S.map, node.id).length > 0) body.append(this.partList(node, chain));
    return box;
  }

  /** The entered node with no row in 学习路径 (a goal without a map, a reading goal's point): its own card. */
  private enterCard(): HTMLElement {
    const flow = this.flow!, S = flow.state, e = S.entered!;
    const c = el("div", "card");
    c.append(el("h2", "", e.name));
    if (e.splitting) { c.append(this.spinLine((e.checkingPage ? LEARN_COPY.checkingPage : e.intoParts ? LEARN_COPY.splittingParts : LEARN_COPY.splittingBlock)(e.name))); return c; }
    const node = nodeOf(S.map, e.region);
    if (node !== undefined && e.path === null && e.pageId === null && node.path_id === null && node.page_id === null) {
      c.append(this.partList(node, this.openChain()));
      return c;
    }
    c.append(this.pageList());
    if (e.path !== null) c.append(this.addRow("p", LEARN_COPY.addPage, (v) => flow.addPage(v)));
    // A goal without a map: the coverage line the 知识图谱 shows under a goal's path, here under this path.
    const cover = S.map === null && e.path ? coverLine(e.path) : "";
    if (cover) { const f = el("div", "faint", cover); f.dataset.cover = e.path!.path_id; c.append(f); }
    return c;
  }

  /** The entered node's pages `ol.elist.pg`: ⋮⋮ drag / Alt + ↑↓, the row's button, ⋯ 拆成多页 · 删除 (正在拆成多页… while split). */
  private pageList(): HTMLElement {
    const flow = this.flow!, e = flow.state.entered!;
    const ol = el("ol", "elist pg");
    e.rows.forEach((p, k) => {
      const li = el("li");
      this.dragRow(li, "p", k);
      if (this.editKey === "p:" + p.key) {
        li.append(el("span", "n", String(k + 1)), this.renameBox(p.title, (v) => { void this.renamePage(p, v); }));
        ol.append(li); return;
      }
      localizedAttribute(li, "aria-label", () => (LEARN_COPY.pageLabel(k + 1, p.title)));
      // the whole card is its page's writing progress (writing-fill.ts: the fill, its stripes and wave)
      const fill = fillLayer(this.cardState(p), this.pagePct(p));
      if (p.pageId !== null) fill.dataset.pg = p.pageId;
      const act = el("span", "act"); act.append(this.pageAct(p));
      const text = this.pageText(p, e.splittingRow === p.key);
      const id = p.pageId;
      if (id !== null) { text.classList.add("ew-rowhit"); text.onclick = () => { this.fromThisGoal(id); this.host.openPage?.(id); }; }
      li.append(fill, el("span", "n", String(k + 1)), text, act);
      const items: [string, () => void][] = [[LEARN_COPY.menuDelete, () => { void this.removePage(k, p); }]];
      if (p.canSplit && e.splittingRow === null) items.unshift([LEARN_COPY.menuSplit, () => { void this.splitPage(p); }]);
      items.unshift([LEARN_COPY.menuRenamePage, () => { this.editKey = "p:" + p.key; this.render(); }]);
      li.append(...this.rowOps("p:" + p.key, p.title, items));
      ol.append(li);
    });
    this.reorder(ol, "p", (from, to) => { void flow.movePage(from, to); });
    return ol;
  }

  /** A page's one button, the same column as a block's: 开始生成, its progress while written, 打开; 重新生成 after a failure. */
  private pageAct(p: PageRow): HTMLElement {
    const state = this.cardState(p), id = p.pageId;
    const shown = state === "writing" && id !== null ? el("span", "pct", () => (LEARN_COPY.pagePct(this.pagePct(p))))
      : id !== null && (state === "written" || state === "read" || state === "mastered") ? this.goBtn(LEARN_COPY.pageOpen, () => { this.fromThisGoal(id); this.host.openPage?.(id); })
      : this.goBtn(state === "failed" ? LEARN_COPY.pageRewrite : LEARN_COPY.pageWrite, () => { void this.writeRow(p); });
    if (shown instanceof HTMLButtonElement && (id === null || (state !== "written" && state !== "read" && state !== "mastered"))) {
      shown.dataset.action = "write-page";
      shown.disabled = [...this.pgStream.values()].some((p) => !p.ended);
    }
    if (id !== null) { shown.dataset.pct = id; shown.dataset.st = state; }      // paintPages keeps it current
    return shown;
  }

  /** 开始生成 on a row: its page made when it has none, then written here (the row fills; this tab stays in front). */
  private async writeRow(p: PageRow): Promise<void> {
    const flow = this.flow, e = flow?.state.entered;
    if (!flow || !e) return;
    try {
      await this.host.writeFirstPage?.(p.pageId !== null
        ? { pathId: null, pageId: p.pageId, title: p.title, goal: flow.state.goal }
        : { pathId: e.path?.path_id ?? null, pageId: null, key: p.key, title: p.title, goal: flow.state.goal },
      () => { void flow.readPath(); });                      // the row names its page at once: its progress shows from now on
    } catch (err) {
      ewNotice(err instanceof Error && err.message !== "" ? err.message : LEARN_COPY.pageWriteFailed);
    }
    if (this.flow === flow) await flow.readPath();
  }

  /**
   * A node's parts (spec 2026-10-28 §2.2), one row each, the same row as a block's: dot, name, scope, one button (开始学习,
   * 继续学习 once begun, 收起 while open); ⋯ 继续拆细 on a point. The one open part has what it opened under it.
   */
  private partList(node: MapNodeOut, chain: readonly string[]): HTMLElement {
    const S = this.flow!.state, e = S.entered!, blocks = this.blockStates();
    const ol = el("ol", "elist blocks parts");
    for (const p of partsOf(S.map, node.id)) {
      const li = el("li"); li.dataset.part = p.id;
      const nm = el("div", "rname", p.name);
      const tag = blockTag(blocks.get(p.id));
      if (tag !== "") nm.append(el("span", "tg", tag));
      const t = el("div"); t.append(nm);
      if (p.scope) t.append(el("div", "scope", p.scope));
      if (e.splittingPart === p.id) t.append(this.spinLine(LEARN_COPY.partSplitting));
      const act = el("span", "act"); act.append(this.nodeAct(p, chain));
      li.append(this.stateDot(this.blockState(p)), t, act);
      if (chain.includes(p.id)) li.classList.add("ew-cur");
      this.rowClick(li, p, chain);
      if ((p.enter === "learn" || p.enter === "page") && e.splittingPart === null) {
        const sole = this.soleRow(p);
        li.append(...this.rowOps("part:" + p.id, p.name, [
          [LEARN_COPY.splitFurther, () => { void this.splitPart(p); }],
          ...(sole !== null && sole.canSplit && e.splittingRow === null ? [[LEARN_COPY.menuSplit, () => { void this.splitPage(sole); }] as [string, () => void]] : []),
        ]));
      }
      ol.append(li);
      if (this.isOpen(p.id, chain)) ol.append(this.nest(p, chain));
    }
    return ol;
  }

  private async splitPart(p: MapNodeOut): Promise<void> {
    const out = await this.flow?.splitPart(p.id);
    if (out) ewNotice(out.kept !== "" ? out.kept : LEARN_COPY.partSplit(p.name, out.children.length));
  }

  /** A page row's text: `.t` the title (+ the materials' § labels, 常考 with its ?), `.scope` 学完能：…, the split status. */
  private pageText(p: PageRow, splitting: boolean): HTMLElement {
    const t = el("div"), tt = el("div", "t");
    // the row's text opens the page once some of it is written (pageList); 正在写… under it while written
    tt.append(p.title);
    const writing = this.cardState(p) === "writing" && this.pagePct(p) < 100;
    const secs = rowSections(p.title, p.sections);
    if (secs.length) tt.append(el("span", "sec", secs.join(" ")));
    const examTip = examTagTip(p.exam);
    if (examTip !== null) tt.append(el("span", "exam", () => (GRAPH_COPY.examTag)), qTip(examTip, () => (GRAPH_COPY.examTag)));
    t.append(tt);
    if (writing) t.append(el("div", "scope", () => (LEARN_COPY.writing))); else if (p.aim) t.append(el("div", "scope", () => (LEARN_COPY.aim(p.aim))));
    if (splitting) t.append(this.spinLine(LEARN_COPY.rowSplitting));
    return t;
  }

  /** 重命名: the server's page and row, then its note's file (撤销 sends the old title the same way). */
  private async renamePage(p: PageRow, title: string): Promise<void> {
    const flow = this.flow, old = p.title;
    if (!flow || title === old) return;
    if (!await flow.renamePage(p.key, title)) return;
    const id = p.pageId;
    if (id !== null) this.host.pageRenamed?.(id);
    ewNotice(LEARN_COPY.pageRenamed(title), { label: LEARN_COPY.undo, run: () => {
      void flow.renamePage(p.key, old).then((ok) => { if (ok && id !== null) this.host.pageRenamed?.(id); });
    } });
  }

  private async removePage(k: number, p: PageRow): Promise<void> {
    const flow = this.flow;
    const key = await flow?.removePage(k);
    if (flow && key) ewNotice(LEARN_COPY.pageRemoved(p.title), { label: LEARN_COPY.undo, run: () => { void flow.unremovePage(key); } });
  }

  private async splitPage(p: PageRow): Promise<void> {
    const flow = this.flow;
    const out = await flow?.splitPage(p.key);
    if (!flow || !out) return;
    const undo = out.undo;
    if (out.kept !== "") ewNotice(out.kept);
    else if (undo !== null) ewNotice(LEARN_COPY.pageSplit(p.title, out.n), { label: LEARN_COPY.undo, run: () => { void flow.undoSplit(p.key, undo); } });
    else ewNotice(LEARN_COPY.pageSplit(p.title, out.n));
  }

}

/** A block's tag as GET /wiki/progress has it: 学习中 · a / n 页, 评估中, 已展开 (parts shown, none picked); "" otherwise. */
function blockTag(b: BlockOut | undefined): string {
  if (b?.state === "learning") return LEARN_COPY.blockLearning(b.pages_done, b.pages_total);
  if (b?.state === "diagnosing") return LEARN_COPY.blockDiagnosing;
  if (b?.state === "choosing") return LEARN_COPY.blockChoosing;
  return "";
}

/** 「正在把「X」分成几大块…」: the goal without its 我要学 / 我想学 lead-in (app.js names the topic, not the sentence). */
export function goalTopic(goal: string): string {
  const t = goal.trim().replace(/^(我(要|想)学(习)?|想学|学)\s*/, "").trim();
  return t !== "" ? t : goal.trim();
}

/** A failed 目录 read / edit: the server's own line for 409 / 422, a blocked write's reason, else the shared wiki line. */
function tocErrorText(e: unknown): string {
  if (e instanceof SourceBlocked) return e.message;
  if (e instanceof ApiError || e instanceof OfflineError) return learnErrorText(e, "edit");
  return errorText(e);
}

/**
 * A failed 上传附件: 「这个文件打不开」 only when the file itself was refused; a write that could not be tried now (not set
 * up, offline, a write running, the server busy) says the plugin's own line for that state; anything else the shared one.
 */
function attachErrorText(e: unknown): string {
  if (e instanceof SourceRefused) return LEARN_COPY.attachFailed;
  if (e instanceof SourceBlocked) return e.message;
  return errorText(e);
}
