import { localizedAttribute } from "../../i18n/index";
/**
 * The goal rows under 今天's small cloud (app.js `goalsBlock`): `div.gls` > per goal `div.gl` > `button.glb` (name + a
 * share bar, no numbers; opens 知识图谱 on that goal) and `div.gw` (「第 a / n 页」 + `button.gnext`, the next page's title,
 * or 全部学完了), newest goal first. Persistent nodes (like the cloud's), made once per open sidebar and moved when 今天 redraws; the data is
 * GET /wiki/graph (goals with their path rows) read through a GraphStore of its own, when the box is on screen.
 * Offline the block stays empty (the sidebar's one banner says it); a single failed read online shows 没能读到，重试 here only.
 *
 * a / n: the rows the learner can still see (not trashed, not skipped); a row is read once its page is done or was opened.
 * The next page is the first row not read yet; a row whose page is not written yet opens the goal instead (app.js).
 */
import { btn, el } from "../../ui/ew";
import { WIKI_COPY } from "../copy";
import { shareSegments } from "../graph-page/panel-logic";
import { graphBus } from "../graph-page/bus";
import { GraphStore, type GraphSource } from "../graph-page/store";
import { finished, stepLabel } from "../progress";
import type { GoalOut, GoalProgressOut, ProgressOut, StepOut } from "../schema";
import { TODAY_COPY } from "../today-copy";

export interface GoalProgressHost {
  source(): GraphSource;
  /** Opens 知识图谱 focused on this goal (map id). */
  openGoal(mapId: string): void;
  /** The server's progress (GET /wiki/progress, read with the catalog); null before it lands. */
  progress(): ProgressOut | null;
  /** Does what a goal's step says (the wiki controller's followStep). */
  followStep(step: StepOut): void;
  /** Whether the wiki is set up. Absent: yes. */
  configured?(): boolean;
  /** The server cannot be reached right now: the rows are hidden (the sidebar's one banner says it). Absent: online. */
  offline?(): boolean;
  /**
   * What the graph depends on besides the page's own edits (the ledger's applied / undone actions, the pages being
   * written): when it changes the graph is read again at once. With `onChange`, which tells when to look.
   */
  stamp?(): string;
  onChange?(fn: () => void): () => void;
}

/** A graph read older than this is read again when 今天 is drawn. */
const FRESH_MS = 20_000;

/** Where the learner is on a goal's path: the rows that count, how many come before the next one, and the next row (null: all read). */
/** A graph goal's progress (by its map, or its lone path). */
export function progressOf(p: ProgressOut | null, g: GoalOut): GoalProgressOut | undefined {
  // its own row first: goals sharing a point share its path, and the older one would answer for both
  return p?.goals.find((x) => x.goal_id === g.map_id) ?? p?.goals.find((x) => g.path_id !== null && x.path_ids.includes(g.path_id));
}

export class GoalProgress {
  readonly root: HTMLDivElement;
  readonly line: HTMLDivElement;
  private store: GraphStore;
  private source: GraphSource;
  private unsub: () => void;
  private readonly unBus: () => void;
  private unHost: () => void = () => undefined;
  private stamp: string | null = null;
  private readAt = 0;
  private destroyed = false;
  private hidden = false;

  constructor(private readonly host: GoalProgressHost, private readonly now: () => number = Date.now) {
    this.root = el("div", "gls");
    this.line = el("div", "muted");
    this.line.hidden = true;
    this.source = host.source();
    this.store = new GraphStore(this.source);
    this.unsub = this.store.subscribe(() => { this.draw(); });
    // The graph page changed the graph (an edit, a split, an undo): read it again now, not after the freshness window.
    this.unBus = graphBus.subscribe(() => { this.changed(); });
    this.stamp = host.stamp?.() ?? null;
    this.unHost = host.onChange?.(() => { this.stampChanged(); }) ?? this.unHost;
    this.draw();
  }

  private changed(): void {
    if (this.destroyed) return;
    this.markStale();
    if (this.root.isConnected && (this.host.configured?.() ?? true) && this.store.state.status !== "loading") this.read();
  }

  private stampChanged(): void {
    const st = this.host.stamp?.();
    if (st === undefined || st === this.stamp) return;
    const first = this.stamp === null;
    this.stamp = st;
    if (!first) this.changed();
  }

  /** Called after every redraw of 今天: reads once when shown, again when the last read is old or the map changed. */
  sync(): void {
    if (this.destroyed || !this.root.isConnected) return;
    const src = this.host.source();
    if (src !== this.source) {
      this.unsub(); this.store.dispose();
      this.source = src; this.store = new GraphStore(src);
      this.unsub = this.store.subscribe(() => { this.draw(); });
      this.readAt = 0;
      this.draw();
    }
    if ((this.host.offline?.() ?? false) !== this.hidden) this.draw();
    const st = this.store.state.status;
    if (!(this.host.configured?.() ?? true) || st === "loading") return;
    if (st === "idle" || this.now() - this.readAt > FRESH_MS) this.read();
  }

  /** The graph this block reads (replaced when the source is; 今天's thumbnail draws the same one). */
  get graphStore(): GraphStore { return this.store; }

  /** The star map changed (a read, a declaration, a review): the goals' shares may have, read again at the next redraw. */
  markStale(): void { this.readAt = 0; }

  /** Re-reads now (重试). */
  read(): void {
    this.readAt = this.now();
    void this.store.refresh();
  }

  private draw(): void {
    const s = this.store.state;
    this.line.replaceChildren();
    this.line.hidden = true;
    this.root.replaceChildren();
    this.hidden = this.host.offline?.() ?? false;
    if (this.hidden) return;
    if (s.status === "error" && !s.offline) {
      const retry = btn(() => (WIKI_COPY.retry), "link", () => { this.read(); });
      retry.dataset.action = "retry";
      this.line.append(el("span", "", () => (WIKI_COPY.readFailedShort)), retry);
      this.line.hidden = false;
      return;
    }
    if (s.graph === null) return;
    // newest goal first (app.js goalsBlock); the server lists them in the order they were made (知识图谱 keeps that)
    for (const g of [...s.graph.goals].reverse()) this.root.append(this.row(g));
  }

  private row(g: GoalOut): HTMLDivElement {
    const r = el("div", "gl");
    const p = progressOf(this.host.progress(), g);
    // the goal as the learner set it (progress: the map's title / goal), not the graph goal's own node name
    const name = p?.goal || g.name;
    const top = el("button", "glb"); top.type = "button";
    localizedAttribute(top, "aria-label", () => (TODAY_COPY.goalOpenAria(name)));
    top.onclick = () => { this.host.openGoal(g.map_id); };
    const bar = el("span", "gbar");
    for (const sg of shareSegments(g.shares)) { const i = el("i"); i.style.flex = String(sg.flex); i.style.background = `var(--${sg.band})`; bar.append(i); }
    const gt = el("span", "gt", name);
    top.append(gt, bar);
    r.append(top);
    // where the goal goes on, as the server says it (GET /wiki/progress): the page row names the page being read until
    // its end's 学完; the next block by its name; 全部学完了 only when every block is done
    const w = el("div", "gw");
    const step = p?.step;
    if (p === undefined || step === undefined) {
      // not read yet: nothing guessed
    } else if (finished(step)) {
      w.append(el("span", "", () => (p.state === "done" ? TODAY_COPY.goalDone : TODAY_COPY.goalNotStarted)));
    } else if (step.kind === "enter_block" || step.kind === "diagnosis") {
      const nb = el("button", "gnext", stepLabel(step)); nb.type = "button";
      nb.onclick = () => { this.host.followStep(step); };
      w.append(nb);
    } else {
      const nb = el("button", "gnext", step.title); nb.type = "button";
      nb.onclick = () => { this.host.followStep(step); };
      w.append(nb);
    }
    r.append(w);
    return r;
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.unsub();
    this.unBus();
    this.unHost();
    this.store.dispose();
  }
}
