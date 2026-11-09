import { labelNode } from "../../i18n/index";
import { localizedAttribute, localizedText } from "../../i18n/index";
/**
 * 知识图谱: a main-area tab that is the approved node-graph demo (the product specification)
 * on the API's graph (GET /wiki/graph, graph-page/). `div.ew.ew-graph` > `.ew-app` grid:
 * - `.ew-panel` (rebuilt by render() exactly as the demo's render()): 「你的目标」 (a button per goal with its share bar,
 *   no numbers; pressing one dims what it does not use and shows its path, pressing again clears), the node card of the
 *   selected node (K state only: dot, name and band word, the group names, 前置 with each one's dot and band word; no
 *   buttons) and the editable 学习路径 (drag, Alt + ↑ / ↓, a ⋯ menu per row with only 拆成多页 / 删除, the row's live
 *   status from the shared writes watcher, 重新写 for a page in the trash, 加一页 … 加上);
 * - `.ew-cloudwrap`: the cloud (graph-page/sigma-cloud.ts: Sigma.js + graphology, their own layout, labels, pan / zoom /
 *   pinch), the hover tip, ＋ / －, the 掌握 / 熟悉 / 不会 legend.
 * Not in the plugin (graph.html has them): the view switch (one stable graph; the goals are the focus) and the
 * 「图谱改了什么」 card (splits / merges are listed in 个性化 · 活动记录). A ledger action waiting for a yes / no that has no
 * page shows at the top of the panel as a slim bar (page-waits.ts, `host.waits`). Every action goes to the API; path moves and removals show
 * at once and are replaced by the server's answer (or put back with a notice when it fails).
 * Loading / error / empty / offline: one `.ew-muted` line centred in the cloud. A visually hidden list of the drawn nodes
 * (one button each) is the keyboard / screen-reader path to the node card.
 */
import { ItemView, type WorkspaceLeaf } from "obsidian";
import { btn, el } from "../../ui/ew";
import { ewNotice } from "../../ui/ew-notice";
import { reorderable } from "../../ui/reorder";
import { openMenuAt } from "../../ui/floating";
import { SCORE } from "../colors";
import { WIKI_COPY } from "../copy";
import { GRAPH_COPY } from "../graph-page/copy";
import { BAND_WORD } from "../format";
import { examTagTip } from "../exam/exam-model";
import { qTip } from "../q-tip";
import { canSplitRow, coverLine, rowSections, optimisticPath, parentNames, shareSegments } from "../graph-page/panel-logic";
import { buildScene, focusTest, realId, type Scene } from "../graph-page/scene";
import { GraphCloud, tipSpot, type GraphCloudHandle, type Hover } from "../graph-page/sigma-cloud";
import { GraphStore, type GraphSource } from "../graph-page/store";
import type { Band, GoalOut, GoalPathOut, GraphOut, PathEditIn, PathItemOut } from "../schema";
import { offlineBanner } from "../render-common";
import { STAR_COPY } from "./copy";
import { LiveRows } from "./live-rows";

export const STARMAP_VIEW_TYPE = "evowiki-starmap";
/** graph.html `li.fresh`: the rise lasts .35 s. */
const FRESH_MS = 350;

export interface StarMapHost {
  /** The node graph and the page's actions (the wiki controller's graphSource, resolved per call). */
  source: GraphSource;
  /** 去学点新的 (the empty state's link): the 学点新的 tab. */
  openLearn(): void;
  /** 看不懂 found a written page: open it, at the section that teaches the node when there is one. */
  showPage(pageId: string, sectionId: string | null): void;
  /** A page got a new title: its note follows. Absent: no note here (the fixture). */
  pageRenamed?(pageId: string): void;
  /** 重新写 / 重试: ask for the page to be written (a page in the trash is written again, never opened as a normal page). */
  writePage(pageId: string, title: string, goal: string): void;
  /** A click on a path row: its page opens; a page not written yet, or a row with none, opens its 开始生成 tab. */
  openRow(row: { pageId: string | null; pathId: string; key: string; title: string; goal: string }): void;
  /** Obsidian's `css-change` (theme switched): returns the unsubscribe. */
  onCssChange(fn: () => void): () => void;
  /**
   * The server connection (absent: the store's own offline flag of its last failed read). `retry`: the 重试 of the
   * banner, which reconnects and reloads everything (the sidebar's sections too); this page re-reads its graph after.
   */
  connection?: { offline(): boolean; onChange(fn: () => void): () => void; retry(): Promise<void> };
  /** The waits with no page (page-waits.ts): their bars, what they show (redrawn only when it changed), and change events. */
  waits?: { bars(): HTMLElement[]; sig(): string; subscribe(fn: () => void): () => void };
}

/** app.js region tip: the average SCORE of its concepts' bands (none excluded); "" without evidence (学点新的 uses it). */
export function regionWord(bands: Band[]): string {
  const bs = bands.filter((b): b is Exclude<Band, "none"> => b !== "none");
  if (bs.length === 0) return "";
  const avg = bs.reduce((s, b) => s + SCORE[b], 0) / bs.length;
  return avg > 0.65 ? STAR_COPY.regionKnown : avg > 0.3 ? STAR_COPY.regionShaky : STAR_COPY.regionGap;
}

const nodeButton = (list: HTMLElement, id: string): HTMLElement | undefined =>
  [...list.querySelectorAll<HTMLElement>("[data-node]")].find((b) => b.dataset.node === id);

/** A goal's paths in the map's order (a goal split on demand has several); a goal from a source without `paths`: its one path. */
function pathsOf(g: GoalOut): GoalPathOut[] {
  if (g.paths.length > 0) return g.paths;
  return g.path_id === null ? [] : [{ path_id: g.path_id, name: g.name, path: g.path }];
}

export class StarMapView extends ItemView {
  private store: GraphStore | null = null;
  private unsubStore: (() => void) | null = null;
  private canvasH: GraphCloudHandle | null = null;
  private readonly cleanups: (() => void)[] = [];
  private graph: GraphOut | null = null;
  private scene: Scene | null = null;
  private focus: string | null = null;
  private selected: string | null = null;
  private busy = false;
  /** The points of the focused goal's pages that are not skipped (graph.html onPath). */
  private lit: ReadonlySet<string> = new Set();
  /** The row whose ⋯ menu is open (page key). */
  private menuKey: string | null = null;
  /** Stops placing the open menu (it is drawn again by every render); the focus goes into it when it was just opened. */
  private menuStop: (() => void) | null = null;
  private menuFocus = false;
  /** The rows' live status (the shared writes watcher) and the 看不懂 waiting for a page being written. */
  private readonly live: LiveRows;
  /** Resolves when the action running now is over (undo and re-ask calls wait for their turn on it). */
  private busyDone: Promise<void> = Promise.resolve();
  private closed = false;
  private readonly seenRows = new Map<string, Set<string>>();
  private readonly freshAt = new Map<string, number>();
  private pendingFocus: string | null = null;
  /** The graph shown when a focus asked for a goal it did not have (a re-read was started then); undefined: none. */
  private focusReadFrom: GraphOut | undefined = undefined;
  private readonly addDrafts = new Map<string, string>();
  /** The row whose title box is open (⋯ 重命名). */
  private renaming: string | null = null;
  private readonly splitting = new Set<string>();
  /** The drag-reorder instances on the panel's path lists (destroyed when the panel is drawn again or closed). */
  private readonly sorts: (() => void)[] = [];
  private edits: Promise<void> = Promise.resolve();
  private listKey = "";
  private rover: string | null = null;
  private wasStale = false;
  private parked = new Set<string>();
  private els: {
    root: HTMLElement; panel: HTMLElement; wrap: HTMLElement; canvas: HTMLElement; tip: HTMLElement; zoom: HTMLElement;
    legend: HTMLElement; state: HTMLElement | null; list: HTMLElement;
  } | null = null;

  constructor(leaf: WorkspaceLeaf, private readonly host: StarMapHost) {
    super(leaf);
    this.live = new LiveRows({
      writes: host.source.writes,
      lookup: host.source.writes === undefined ? undefined : (ids) => host.source.writes!.lookup(ids),
      panel: () => this.els?.panel ?? null,
      retry: (pageId) => { const x = this.rowOfPage(pageId); if (x !== null) this.requestWrite(x.row, x.goal); },
    });
  }

  getViewType(): string { return STARMAP_VIEW_TYPE; }
  getDisplayText(): string { return STAR_COPY.title + (this.host.source.demo ? STAR_COPY.demoSuffix : ""); }
  override getIcon(): string { return "network"; }

  /** For tests: the canvas (zoom, what it drew) and the page state. */
  get canvasHandle(): GraphCloudHandle | null { return this.canvasH; }
  get focusGoal(): string | null { return this.focus; }
  get selectedNode(): string | null { return this.selected; }

  override onOpen(): Promise<void> {
    this.closed = false;
    const root = document.createElement("div");
    root.className = "ew ew-graph";
    const app = el("div", "app");
    const panel = el("div", "panel");
    const wrap = el("div", "cloudwrap");
    const canvas = el("div", "sigma");
    const tip = el("div", "tip"); tip.hidden = true;
    const zoom = el("div", "zoom"), btns = el("div", "btns");
    const zin = btn(() => (GRAPH_COPY.zoomIn), "icon", () => { this.canvasH?.zoomIn(); }); localizedAttribute(zin, "aria-label", () => (GRAPH_COPY.zoomInLabel));
    const zout = btn(() => (GRAPH_COPY.zoomOut), "icon", () => { this.canvasH?.zoomOut(); }); localizedAttribute(zout, "aria-label", () => (GRAPH_COPY.zoomOutLabel));
    btns.append(zin, zout); zoom.append(btns);
    const legend = el("div", "legend");
    const item = (band: "known" | "shaky" | "gap", label: string): HTMLElement => {
      const s = el("span"); const i = el("i"); i.style.background = `var(--${band})`; s.append(i, label); return s;
    };
    legend.append(item("known", GRAPH_COPY.legendKnown), item("shaky", GRAPH_COPY.legendShaky), item("gap", GRAPH_COPY.legendGap));
    wrap.append(canvas, tip, zoom, legend);
    app.append(panel, wrap);
    const list = document.createElement("ul");
    list.className = "evowiki-sr-only";
    localizedAttribute(list, "aria-label", () => (GRAPH_COPY.listLabel));
    root.append(app, list);
    this.contentEl.replaceChildren(root);
    this.els = { root, panel, wrap, canvas, tip, zoom, legend, state: null, list };

    this.canvasH = GraphCloud(canvas, {
      semantic: true,
      tokens: root,
      font: () => getComputedStyle(root).fontFamily,
      reduced: () => matchMedia("(prefers-reduced-motion: reduce)").matches,
      onSelect: (id) => { this.select(id); },
      onHover: (h) => { this.showTip(h); },
    });
    const cv = this.canvasH;
    this.cleanups.push(this.host.onCssChange(() => { cv.recolor(); this.render(); }));
    root.addEventListener("keydown", this.onKey);
    list.addEventListener("keydown", this.onListKey);
    list.addEventListener("focusin", this.onListFocus);
    // The ⋯ menu closes on a click anywhere else; the {evowiki:"focus", goal} message (今天) selects a goal.
    const doc = root.ownerDocument, win = doc.defaultView;
    doc.addEventListener("click", this.onDocClick);
    win?.addEventListener("message", this.onMessage);
    this.cleanups.push(() => {
      root.removeEventListener("keydown", this.onKey);
      list.removeEventListener("keydown", this.onListKey);
      list.removeEventListener("focusin", this.onListFocus);
      doc.removeEventListener("click", this.onDocClick);
      win?.removeEventListener("message", this.onMessage);
    });
    // The rows' status follows the one shared poll of GET /wiki/writes (no second poll here).
    this.live.start();
    this.cleanups.push(() => { this.live.stop(); });

    this.store = new GraphStore(this.host.source, true);
    this.unsubStore = this.store.subscribe(() => { this.update(); });
    const kChanged = this.host.source.kChanged?.(() => { void this.store?.refresh(); });
    if (kChanged) this.cleanups.push(kChanged);
    const conn = this.host.connection;
    if (conn) {
      // Back online: the banner goes and a read that failed is made again (a graph read fine stays as it is).
      this.cleanups.push(conn.onChange(() => {
        this.update();
        const st = this.store?.state;
        if (st && !conn.offline() && (st.status === "error" || st.stale)) this.refresh();
      }));
    }
    const waits = this.host.waits;
    if (waits) {
      let seen = waits.sig();
      this.cleanups.push(waits.subscribe(() => { const now = waits.sig(); if (now !== seen) { seen = now; this.render(); } }));
    }
    this.update();
    void this.store.refresh();
    this.loadParked();
    return Promise.resolve();
  }

  override onClose(): Promise<void> {
    this.closed = true;
    for (const stop of this.sorts.splice(0)) stop();
    this.menuStop?.(); this.menuStop = null;
    this.canvasH?.destroy();
    this.canvasH = null;
    for (const c of this.cleanups.splice(0)) c();
    this.unsubStore?.(); this.unsubStore = null;
    this.store?.dispose(); this.store = null;
    this.els = null;
    return Promise.resolve();
  }

  /** Reads the graph again (重试; main may call it when the tab is shown again). */
  refresh(): void { void this.store?.refresh(); this.loadParked(); }

  private focusedGoal(): GoalOut | null { return this.graph?.goals.find((g) => g.map_id === this.focus) ?? null; }

  // ---- the store → the scene, the states, the panel and the list ----
  private update(): void {
    const els = this.els, store = this.store;
    if (els === null || store === null) return;
    const s = store.state;
    const empty = s.status === "ready" && s.graph !== null && s.graph.nodes.length === 0;
    const show = s.status === "ready" && s.graph !== null && !empty;
    if (show && s.graph !== this.graph) {
      this.graph = s.graph;
      this.scene = buildScene(s.graph!, true);
      this.canvasH?.setScene(this.scene);
    } else if (!show && this.graph !== null) { this.graph = null; this.scene = null; this.canvasH?.setScene(null); }
    this.applyPendingFocus();
    if (this.focus !== null && this.focusedGoal() === null && this.pendingFocus === null) this.focus = null;
    this.lit = this.litNodes();
    if (this.selected !== null && !this.scene?.nodes.has(this.selected)) this.selected = null;
    els.root.dataset.state = empty ? "empty" : s.status === "idle" ? "loading" : s.status;
    els.canvas.hidden = !show; els.legend.hidden = !show; els.zoom.hidden = !show;
    if (!show) els.tip.hidden = true;
    this.renderList();
    this.renderState(empty);
    this.render();
    // Offline the banner says it (no notice); a stale graph while the server answers says so once.
    const stale = show && s.stale && !this.offlineNow();
    if (stale && !this.wasStale) ewNotice(STAR_COPY.staleNote);
    this.wasStale = stale;
  }

  /** Offline now: the connection's state, or (no connection given) the offline flag of the store's last failed read. */
  private offlineNow(): boolean {
    const conn = this.host.connection;
    if (conn) return conn.offline();
    const s = this.store?.state;
    return s !== undefined && s.offline && (s.status === "error" || s.stale);
  }

  /** The banner's 重试: reconnect and reload everything (host), then read this page's graph again. */
  private retryAll(): void {
    const conn = this.host.connection;
    if (!conn) { this.refresh(); return; }
    const after = (): void => { this.refresh(); };
    void conn.retry().then(after, after);
  }

  private renderState(empty: boolean): void {
    const els = this.els!, s = this.store!.state;
    els.state?.remove();
    els.state = null;
    let line: HTMLElement | null = null;
    // The one offline line of this page (未连接到服务器 · 重试): centred when nothing is drawn, at the top over a graph.
    const banner = offlineBanner(this.offlineNow(), () => { this.retryAll(); });
    if (banner !== null) {
      if (s.status === "ready" && s.graph !== null && !empty) banner.classList.add("ew-mapbanner");
      else banner.classList.add("ew-mapstate");
      els.wrap.append(banner);
      els.state = banner;
      return;
    }
    if (s.status === "idle" || s.status === "loading") {
      line = el("div", "muted", () => (STAR_COPY.loading));
      line.setAttribute("role", "status");
    } else if (s.status === "error") {
      line = el("div", "muted");
      line.setAttribute("role", "alert");
      const b = btn(() => (STAR_COPY.retry), "link", () => { this.refresh(); });
      b.dataset.action = "retry";
      line.append(el("span", "", () => (WIKI_COPY.readFailedShort)), b);
    } else if (empty) {
      line = el("div", "muted", () => (STAR_COPY.empty));
      line.append(" ", btn(() => (STAR_COPY.emptyAction), "link", () => { this.host.openLearn(); }));
    }
    if (line === null) return;
    line.classList.add("ew-mapstate");
    els.wrap.append(line);
    els.state = line;
  }

  // ---- graph.html render(): the panel ----
  private render(): void {
    for (const stop of this.sorts.splice(0)) stop();
    this.menuStop?.(); this.menuStop = null;
    const els = this.els;
    if (els === null) return;
    const p = els.panel, doc = p.ownerDocument;
    const active = doc.activeElement instanceof HTMLElement && p.contains(doc.activeElement) ? doc.activeElement.dataset.k : undefined;
    p.replaceChildren(...(this.host.waits?.bars() ?? []));
    const g = this.graph, S = this.scene;
    if (g === null || S === null) { this.fitPanel(); return; }
    if (g.goals.length) {
      const vc = el("div", "card"); vc.append(el("h2", "", () => (GRAPH_COPY.goalsTitle)));
      for (const goal of g.goals) {
        const b = el("button", "goal"); b.type = "button"; b.dataset.k = "goal:" + goal.map_id;
        b.setAttribute("aria-pressed", String(this.focus === goal.map_id));
        b.onclick = () => { this.setFocus(this.focus === goal.map_id ? null : goal.map_id); };
        const bar = el("span", "bar");
        for (const sgm of shareSegments(goal.shares)) { const i = el("i"); i.style.flex = String(sgm.flex); i.style.background = `var(--${sgm.band})`; bar.append(i); }
        b.append(el("span", "gname", goal.name), bar); vc.append(b);
      }
      p.append(vc);
    }
    if (this.selected !== null) p.append(this.nodeCard(S, this.selected));
    const fg = this.focusedGoal();
    if (fg !== null) {
      const paths = pathsOf(fg);
      for (const one of paths) p.append(this.pathCard(fg, one, paths.length > 1 ? one.name : GRAPH_COPY.pathTitle));
    }
    if (fg !== null) {
      const cover = coverLine(fg);
      if (cover) { const f = el("div", "faint", cover); f.dataset.cover = fg.map_id; p.append(f); }
    }
    if (active !== undefined) [...p.querySelectorAll<HTMLElement>("[data-k]")].find((e) => e.dataset.k === active)?.focus({ preventScroll: true });
    this.fitPanel();
    this.syncCloud();
  }

  /** The cloud shows what the panel says: the selected point, the focused goal's points and its path's points. */
  private syncCloud(): void {
    const S = this.scene;
    if (S === null) return;
    const goal = this.focusedGoal(), inGoal = focusTest(S, goal);
    this.canvasH?.setState({ sel: this.selected, focus: goal === null ? null : new Set(S.order.filter(inGoal)), lit: this.lit });
  }

  /** Frames the focused goal's points (a goal without points yet: the camera stays). */
  private frameGoal(): void {
    const S = this.scene, goal = this.focusedGoal();
    if (S === null || goal === null) return;
    this.canvasH?.frame(S.order.filter(focusTest(S, goal)), "block");
  }

  /** The hover tip: a point's name and band word, its prerequisites with their phrases, its other related points, its kind;
   *  a title's group name and summary. */
  private showTip(h: Hover | null): void {
    const tip = this.els?.tip, S = this.scene;
    if (!tip) return;
    if (h === null || S === null) { tip.hidden = true; return; }
    tip.replaceChildren();
    const rel = (t: string): HTMLElement => el("span", "rel", t);
    if (h.kind === "node") {
      const n = S.nodes.get(h.id);
      if (!n) { tip.hidden = true; return; }
      // a point whose name stands beside it: the tip does not say it again
      if (h.named !== true) tip.append(el("b", "", () => (GRAPH_COPY.tipName(n.name, BAND_WORD[n.band]))));
      for (const q of n.pre) tip.append(rel(GRAPH_COPY.tipPrereq(S.names.get(q.id) ?? q.id, q.phrase)));
      const more = h.near.filter((id) => !n.pre.some((q) => q.id === id)).map((id) => S.names.get(id) ?? id);
      if (more.length) tip.append(rel(GRAPH_COPY.tipRelated(more)));
      if (n.kind) tip.append(rel(n.kind));
      if (tip.childNodes.length === 0) { tip.hidden = true; return; }
    } else {
      const g = S.l1.find((x) => x.id === h.id) ?? S.l2.find((x) => x.id === h.id) ?? S.domains?.find((x) => x.id === h.id);
      if (!g) { tip.hidden = true; return; }
      tip.append(el("b", "", g.name));
      if (g.summary) tip.append(rel(g.summary));
    }
    const wrap = this.els?.wrap;
    tip.hidden = false;
    const { left, top } = tipSpot(h, tip.offsetWidth, tip.offsetHeight, wrap?.clientWidth ?? 0, wrap?.clientHeight ?? 0);
    tip.style.left = `${left}px`; tip.style.top = `${top}px`;
  }

  /** No goal, no selected point: the panel would be an empty 360 px column, so the cloud takes the whole width. */
  private fitPanel(): void {
    const els = this.els;
    if (els === null) return;
    const empty = els.panel.childElementCount === 0;
    els.panel.style.display = empty ? "none" : "";
    const app = els.panel.parentElement;
    if (app) app.style.gridTemplateColumns = empty ? "minmax(0,1fr)" : "";
  }

  /** graph.html nodeCard: only the learner model's state - this point's band, its group, its prerequisites' bands (no actions). */
  private nodeCard(S: Scene, id: string): HTMLElement {
    const n = S.nodes.get(id)!, c = el("div", "card"); c.dataset.card = "node";
    const h = el("h2", "nh"); const d = el("span", "dot"); d.style.background = `var(--${n.band})`; h.append(d, n.name);
    if (BAND_WORD[n.band]) h.append(el("span", "bandw", () => (BAND_WORD[n.band])));
    c.append(h);
    const ev = el("div", "ev");
    const parents = parentNames(S, n);
    if (parents.length) ev.append(el("span", "", parents.join("、")));
    c.append(ev);
    if (n.pre.length) {
      const pr = el("div", "ev"); pr.append(el("span", "prh", () => (GRAPH_COPY.prereqHead)));
      for (const q of n.pre) {
        const m = S.nodes.get(q.id), row = el("span", "prow"), dd = el("span", "dot");
        dd.style.background = `var(--${m?.band ?? "none"})`;
        row.append(dd, S.names.get(q.id) ?? q.id, el("span", "bandw", () => (m ? BAND_WORD[m.band] : "")));
        pr.append(row);
      }
      c.append(pr);
    }
    return c;
  }

  /**
   * One node action at a time; a failure says so (the graph stays as it was). Nothing is greyed out meanwhile. A click
   * while one runs is ignored; an undo or a waiting 看不懂 (`wait`) takes its turn after it - it is never dropped.
   */
  private async act(run: () => Promise<void>, wait = false): Promise<void> {
    while (this.busy) {
      if (!wait) return;
      await this.busyDone;
    }
    if (this.closed) return;                          // closed while it waited for its turn: nothing runs
    this.busy = true;
    let release!: () => void;
    this.busyDone = new Promise<void>((r) => { release = r; });
    try { await run(); } catch { ewNotice(GRAPH_COPY.actionFailed); } finally { this.busy = false; release(); }
  }

  /**
   * A path edit: moves and removals show at once; the server's answer replaces them, a failure puts the path back.
   * Resolves to whether the edit went through (edits run one after the other).
   */
  private edit(pathId: string, e: PathEditIn, focusAt?: number): Promise<boolean> {
    const store = this.store, one = this.graph?.goals.flatMap(pathsOf).find((x) => x.path_id === pathId);
    if (store === null || one === undefined) return Promise.resolve(false);
    const before = one.path, shown = optimisticPath(before, e);
    if (shown !== null) store.setPath(pathId, shown);
    if (focusAt !== undefined) this.focusPage(focusAt);
    const done = this.edits.then(async (): Promise<boolean> => {
      let ok = true;
      try { await store.editPath(pathId, e); } catch { ok = false; store.setPath(pathId, before); ewNotice(GRAPH_COPY.actionFailed); }
      if (focusAt !== undefined) this.focusPage(focusAt);
      return ok;
    });
    this.edits = done.then(() => undefined);
    return done;
  }

  private focusPage(i: number): void {
    setTimeout(() => { this.els?.panel.querySelectorAll<HTMLElement>(".ew-pages li")[i]?.focus(); }, 0);
  }

  /** B5: which goals are parked for the Bot. A failed read keeps what is shown (nothing is drawn for an unknown state). */
  private loadParked(): void {
    const src = this.host.source;
    if (src.parkedGoals === undefined) return;
    src.parkedGoals().then((ids) => { this.parked = new Set(ids); this.render(); }, () => { /* keep what is shown */ });
  }

  private resume(pathId: string): void {
    const src = this.host.source;
    if (src.setParked === undefined) return;
    void this.act(async () => {
      try { await src.setParked!(pathId, false); } catch { ewNotice(GRAPH_COPY.resumeFailed); return; }
      this.parked.delete(pathId);
      this.render();
    });
  }

  // ---- the goal in focus (graph.html: the goal buttons and the {evowiki:"focus", goal} message) ----

  /** The points of the focused goal's pages that are not skipped (graph.html onPath). */
  private litNodes(): ReadonlySet<string> {
    const g = this.focusedGoal();
    return g === null ? new Set() : new Set(pathsOf(g).flatMap((x) => x.path).filter((p) => !p.skipped).flatMap((p) => p.node_ids));
  }

  /** Selects a goal (or clears it): its points light up, the cloud zooms in and glides to it, the node card and menu close. */
  setFocus(mapId: string | null): void {
    this.focus = mapId;
    this.selected = null;
    this.menuKey = null;
    this.lit = this.litNodes();
    this.render();
    if (mapId !== null) this.frameGoal();
  }

  /**
   * A goal asked for before the graph arrived, or one the graph shown does not have yet (a goal made since it was read:
   * a re-read was started): applied when a graph that has it is there. When the re-read still lacks it (or fails) the
   * focus stays cleared: another goal is never focused instead (bug 2026-10-24: 蛋白结合基础 opened 黑壳虾).
   */
  private applyPendingFocus(): void {
    const id = this.pendingFocus;
    if (id === null || this.graph === null) return;
    const g = this.graph.goals.find((x) => x.map_id === id || x.path_id === id);
    if (g === undefined) {
      const reread = this.focusReadFrom === undefined || this.graph !== this.focusReadFrom;
      if (!reread && this.store?.state.status !== "error") return;          // the re-read is still on its way
      this.pendingFocus = null; this.focusReadFrom = undefined; this.focus = null;
      return;
    }
    this.pendingFocus = null; this.focusReadFrom = undefined;
    this.focus = g.map_id; this.selected = null; this.menuKey = null;
    this.lit = this.litNodes();
    queueMicrotask(() => { this.frameGoal(); });
  }

  /** The 今天 message `{evowiki: "focus", goal}`: goal = a goal's map id (or its path id); anything else clears the focus. */
  private readonly onMessage = (e: MessageEvent): void => {
    // only this window's own messages (the plugin's 今天 view); another frame's are not ours
    if (e.source !== null && e.source !== this.contentEl.ownerDocument.defaultView) return;
    const d = e.data as { evowiki?: unknown; goal?: unknown } | null;
    if (d === null || typeof d !== "object" || d.evowiki !== "focus") return;
    this.focusOn(typeof d.goal === "string" ? d.goal : null);
  };

  /** Focuses a goal by its map id (or path id); null clears the focus. Asked for before the graph arrived: applied when it is there. */
  focusOn(id: string | null): void {
    if (this.graph === null) { this.pendingFocus = id; return; }
    const g = id === null ? undefined : this.graph.goals.find((x) => x.map_id === id || x.path_id === id);
    if (id !== null && g === undefined) {
      // a goal this graph does not have yet: read again and focus it when it is there (no other goal meanwhile)
      this.setFocus(null);
      this.pendingFocus = id; this.focusReadFrom = this.graph;
      void this.store?.refresh();
      return;
    }
    this.pendingFocus = null; this.focusReadFrom = undefined;
    this.setFocus(g?.map_id ?? null);
  }

  private readonly onDocClick = (e: MouseEvent): void => {
    if (this.menuKey === null) return;
    const t = e.target;
    if (t instanceof Element && t.closest(".ew-menu, .ew-more") !== null) return;
    this.menuKey = null; this.render();
  };

  // ---- rows and their pages ----

  private rowOfPage(pageId: string): { row: PathItemOut; goal: GoalOut } | null {
    for (const goal of this.graph?.goals ?? []) {
      const row = pathsOf(goal).flatMap((x) => x.path).find((p) => p.page_id === pageId);
      if (row !== undefined) return { row, goal };
    }
    return null;
  }

  /** 重新写 / 重试: the page is written at the learner's priority (the host streams it); a page in the trash is not opened. */
  private requestWrite(row: PathItemOut, goal: GoalOut): void {
    if (row.page_id === null) return;
    this.host.writePage(row.page_id, row.title, goal.goal);
  }

  // ---- a row's ⋯ menu ----

  private removeRow(pathId: string, pg: PathItemOut, i: number): void {
    void this.edit(pathId, { op: "remove", index: i }).then((ok) => {
      if (!ok) return;
      // 撤销 = the inverse edit (the server shows the very row again, where it was)
      ewNotice(GRAPH_COPY.removed(pg.title), { label: GRAPH_COPY.undo, run: () => { void this.edit(pathId, { op: "unremove", page_key: pg.key }); } });
    });
  }

  private splitRow(pathId: string, pg: PathItemOut): void {
    const store = this.store;
    if (store === null || this.splitting.has(pg.key)) return;
    this.splitting.add(pg.key);
    this.render();                                           // the row says it is being split (the model takes a while)
    const before = new Set(this.graph?.goals.flatMap(pathsOf).find((x) => x.path_id === pathId)?.path.map((p) => p.key) ?? []);
    void store.splitRow(pathId, pg.key).then((out) => {
      if (pg.node_ids.length === 1) {
        const id = pg.node_ids[0]!, S = this.scene;
        this.canvasH?.frame([id, ...(S === null ? [] : S.order.filter((k) => S.nodes.get(k)!.from === id))]);
      }
      const undo = out.undo;
      const text = GRAPH_COPY.rowSplit(pg.title, out.path.filter((r) => !before.has(r.key)).length);
      if (out.kept !== "") ewNotice(out.kept);
      else if (undo !== null) ewNotice(text, { label: GRAPH_COPY.undo, run: () => { void this.act(async () => { await store.undoSplitRow(pathId, pg.key, undo); }, true); } });
      else ewNotice(text);
    }, () => { ewNotice(GRAPH_COPY.actionFailed); }).finally(() => { this.splitting.delete(pg.key); this.render(); });
  }

  /** 重命名 on a row: the server's page and row, then its note's file; 撤销 sends the old title the same way. */
  private renameRow(pathId: string, pg: PathItemOut, title: string): void {
    const old = pg.title, id = pg.page_id;
    if (title === old) return;
    const send = (t: string): Promise<boolean> => this.edit(pathId, { op: "retitle", page_key: pg.key, title: t }).then((ok) => {
      if (ok && id !== null) this.host.pageRenamed?.(id);
      return ok;
    });
    void send(title).then((ok) => {
      if (ok) ewNotice(GRAPH_COPY.renamed(title), { label: GRAPH_COPY.undo, run: () => { void send(old); } });
    });
  }

  /** graph.html's editBox for a row's title: the box, 保存, 取消; Enter saves, Escape leaves it. */
  private renameBox(pathId: string, pg: PathItemOut): HTMLElement {
    const box = el("div", "edit"), inp = el("input");
    inp.type = "text"; inp.value = pg.title; localizedAttribute(inp, "placeholder", () => (GRAPH_COPY.renamePlaceholder)); localizedAttribute(inp, "aria-label", () => (GRAPH_COPY.renamePlaceholder));
    const close = (): void => { this.renaming = null; this.render(); };
    const save = (): void => { const v = inp.value.trim(); if (v === "") return; close(); this.renameRow(pathId, pg, v); };
    inp.onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) save(); else if (e.key === "Escape") close(); };
    const r = el("div", "row");
    r.append(inp, btn(() => (GRAPH_COPY.save), "primary", save), btn(() => (GRAPH_COPY.cancel), "", close));
    box.append(r);
    window.setTimeout(() => { inp.focus(); inp.select(); }, 0);
    return box;
  }

  private rowMenu(pathId: string, pg: PathItemOut, i: number): HTMLElement {
    const m = el("div", "menu"); m.setAttribute("role", "menu");
    const item = (label: string, key: string, fn: () => void): void => {
      const b = el("button", "", label); b.type = "button"; b.setAttribute("role", "menuitem"); b.dataset.k = key;
      b.onclick = (e) => { e.stopPropagation(); this.menuKey = null; this.render(); fn(); };
      m.append(b);
    };
    const node = this.graph?.nodes.find((x) => x.id === pg.node_ids[0]);
    item(GRAPH_COPY.menuRename, "menu-rename:" + pg.key, () => { this.renaming = pg.key; this.render(); });
    if (canSplitRow(pg, node?.can_split ?? false)) item(GRAPH_COPY.menuSplit, "menu-split:" + pg.key, () => { this.splitRow(pathId, pg); });
    item(GRAPH_COPY.menuDelete, "menu-delete:" + pg.key, () => { this.removeRow(pathId, pg, i); });
    return m;
  }

  private pathCard(goal: GoalOut, one: GoalPathOut, title: string): HTMLElement {
    const pathId = one.path_id;
    const c = el("div", "card"); const h = el("div", "row"); h.style.justifyContent = "space-between"; h.append(el("h2", "", title)); c.append(h);
    if (this.parked.has(pathId)) {
      const p = el("span", "faint", () => (GRAPH_COPY.parked)); p.dataset.parked = pathId;
      const r = btn(() => (GRAPH_COPY.resume), "link", () => { this.resume(pathId); }); r.dataset.k = "resume";
      const w = el("span"); w.append(p, " ", r); h.append(w);
    }
    const ol = el("ol", "pages");
    const P = one.path;
    // rows that were not there at the last drawing of this path rise in (not the first drawing of a path)
    const seen = this.seenRows.get(pathId), now = performance.now();
    this.seenRows.set(pathId, new Set(P.map((x) => x.key)));
    for (const [k, at] of this.freshAt) if (now - at >= FRESH_MS) this.freshAt.delete(k);
    if (seen !== undefined) for (const pg of P) if (!seen.has(pg.key)) this.freshAt.set(pathId + "/" + pg.key, now);
    P.forEach((pg, i) => {
      // a row drawn again while it is still rising (the answer, then the re-read graph) carries on, it does not restart
      const since = this.freshAt.get(pathId + "/" + pg.key), fresh = since !== undefined && now - since < FRESH_MS;
      if (this.renaming === pg.key) {
        const row = el("li"); row.dataset.k = "page:" + pg.key;
        row.append(el("span", "n", String(i + 1)), this.renameBox(pathId, pg));
        ol.append(row);
        return;
      }
      const li = el("li", (pg.skipped ? "skip" : "") + (fresh ? " fresh" : ""));
      if (fresh) li.style.animationDelay = `-${Math.round(now - since)}ms`;
      li.draggable = true; li.tabIndex = 0; li.dataset.k = "page:" + pg.key;
      const openRow = (): void => { this.host.openRow({ pageId: pg.page_id, pathId, key: pg.key, title: pg.title, goal: goal.goal }); };
      // a page in the trash has its own 重新写; the row's controls and its menu keep their own clicks
      if (pg.trashed !== true) {
        li.addEventListener("click", (e) => { if (!(e.target as Element).closest("button, a, [role=menu]")) openRow(); });
        li.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === li) openRow(); });
      }
      localizedAttribute(li, "aria-label", () => (GRAPH_COPY.pageLabel(i + 1, pg.title)));
      li.append(el("span", "n", String(i + 1)));
      const t = el("div"); const tt = el("div", "t", pg.title); if (pg.mine) tt.append(el("span", "mine", () => (GRAPH_COPY.mine)));
      const secs = rowSections(pg.title, pg.sections);
      if (secs.length) tt.append(el("span", "sec", secs.join(" ")));
      const examTip = examTagTip(pg.exam);
      if (examTip !== null) tt.append(el("span", "exam", () => (GRAPH_COPY.examTag)), qTip(examTip, () => (GRAPH_COPY.examTag)));
      const w = el("div", "w"); w.hidden = true;
      if (this.splitting.has(pg.key)) {
        // 拆成多页 waits on the model: this row says so (graph.html's writing status), nothing else is greyed out
        w.hidden = false; w.dataset.splitting = "1"; w.append(el("i"), labelNode(() => (GRAPH_COPY.rowSplitting)));
      } else if (pg.trashed === true && pg.page_id !== null) {
        // a page in the trash (the learner undid the Bot's write): 重新写 asks for it again, the row is not a page to open
        w.hidden = false; w.dataset.trashed = "1";
        const b = btn(() => (GRAPH_COPY.rewrite), "link", () => { this.requestWrite(pg, goal); }); b.dataset.k = "rewrite:" + pg.key;
        w.append(b);
      } else if (pg.page_id !== null) { w.dataset.w = pg.page_id; this.live.paint(w); }
      const line = pg.skipped ? GRAPH_COPY.skippedLine : pg.question;           // never the summary (writer-only)
      t.append(tt, ...(line === "" ? [] : [el("div", "s", line)]), w); li.append(t);
      const ops = el("div", "ops");
      const more = el("button", "more", "⋯"); more.type = "button"; more.dataset.k = "more:" + pg.key;
      localizedAttribute(more, "aria-label", () => (GRAPH_COPY.moreLabel(pg.title))); more.setAttribute("aria-haspopup", "menu");
      more.setAttribute("aria-expanded", String(this.menuKey === pg.key));
      more.onclick = (e) => { e.stopPropagation(); this.menuFocus = this.menuKey !== pg.key; this.menuKey = this.menuFocus ? pg.key : null; this.render(); };
      ops.append(el("span", "grip", () => (GRAPH_COPY.grip)), more);
      li.append(ops);
      if (this.menuKey === pg.key) {
        const m = this.rowMenu(pathId, pg, i);
        li.append(m);
        // placed under the button by @floating-ui (flips above at the bottom of the panel); Esc / Tab put the focus back on it
        const key = pg.key;
        this.menuStop = openMenuAt(more, m, (refocus) => {
          this.menuKey = null; this.render();
          if (refocus) this.els?.panel.querySelector<HTMLElement>('[data-k="more:' + key + '"]')?.focus();
        }, this.menuFocus);
        this.menuFocus = false;
      }
      ol.append(li);
    });
    // drag (SortableJS, ui/reorder.ts: only these rows, never a file or text dragged in) or Alt + ↑ / ↓ (graph.html
    // li.onkeydown; the focus follows the row): the edit changes the path at once and the panel is drawn again
    this.sorts.push(reorderable(ol, {
      move: (from, to, byKey) => { void this.edit(pathId, { op: "move", from, to }, byKey ? to : undefined); },
    }));
    c.append(ol);
    const add = el("div", "row"); add.style.flexWrap = "nowrap";
    const inp = el("input"); inp.type = "text"; localizedAttribute(inp, "placeholder", () => (GRAPH_COPY.addPlaceholder)); inp.dataset.k = "add-page"; inp.value = this.addDrafts.get(pathId) ?? "";
    inp.oninput = () => { this.addDrafts.set(pathId, inp.value); };
    const go = (): void => { const v = inp.value.trim(); if (!v) return; this.addDrafts.delete(pathId); void this.edit(pathId, { op: "add", title: v }); };
    inp.onkeydown = (e) => { if (e.key === "Enter") go(); };      // graph.html: Enter adds
    add.append(inp, btn(() => (GRAPH_COPY.add), "", go));
    c.append(add);
    return c;
  }

  // ---- selection ----
  private select(id: string | null, focusCard = false): void {
    if (id !== null && !this.scene?.nodes.has(id)) id = null;
    this.selected = id;
    this.render();
    if (id !== null && focusCard) {
      // The card's first action, or its heading when it has none.
      const card = this.els?.panel.querySelector<HTMLElement>("[data-card=node]");
      const target = card?.querySelector<HTMLElement>("button:not(:disabled)") ?? card?.querySelector<HTMLElement>("h2");
      if (target) { if (target.tagName === "H2") target.tabIndex = -1; target.focus({ preventScroll: true }); }
    }
  }

  private readonly onKey = (e: KeyboardEvent): void => {
    if (e.key === "Escape" && this.menuKey !== null) {
      e.preventDefault(); e.stopPropagation();
      const key = this.menuKey;
      this.menuKey = null; this.render();
      this.els?.panel.querySelector<HTMLElement>('[data-k="more:' + key + '"]')?.focus();
      return;
    }
    if (e.key !== "Escape" || this.selected === null) return;
    e.preventDefault();
    e.stopPropagation();
    const id = this.selected;
    this.select(null);
    if (this.els !== null) nodeButton(this.els.list, id)?.focus();
  };

  /** Arrow keys / Home / End move between the list buttons (one Tab stop for the whole list). */
  private readonly onListKey = (e: KeyboardEvent): void => {
    const keys = ["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft", "Home", "End"];
    if (!keys.includes(e.key) || this.els === null) return;
    const buttons = [...this.els.list.querySelectorAll<HTMLButtonElement>("button")];
    const i = buttons.findIndex((b) => b === e.target);
    if (i < 0 || buttons.length === 0) return;
    e.preventDefault();
    const next = e.key === "Home" ? 0 : e.key === "End" ? buttons.length - 1
      : e.key === "ArrowDown" || e.key === "ArrowRight" ? Math.min(buttons.length - 1, i + 1) : Math.max(0, i - 1);
    buttons[next]!.focus();
  };

  private readonly onListFocus = (e: FocusEvent): void => {
    const id = (e.target as HTMLElement).dataset.node;
    if (id !== undefined) this.setRover(id);
  };

  private setRover(id: string | null): void {
    this.rover = id;
    if (this.els === null) return;
    for (const b of this.els.list.querySelectorAll<HTMLElement>("[data-node]")) b.tabIndex = b.dataset.node === id ? 0 : -1;
  }

  /** The keyboard / screen-reader path: one button per drawn node, visually hidden. */
  private renderList(): void {
    const els = this.els!, S = this.scene;
    const entries = S === null ? [] : S.order.filter((id) => realId(id) === id).map((id) => S.nodes.get(id)!);
    const key = entries.map((n) => `${n.id}:${n.band}:${n.name}`).join("|");
    if (key === this.listKey) return;
    this.listKey = key;
    const doc = els.list.ownerDocument;
    const focused = els.list.contains(doc.activeElement) ? (doc.activeElement as HTMLElement).dataset.node : undefined;
    if (!entries.some((n) => n.id === this.rover)) this.rover = entries[0]?.id ?? null;
    els.list.replaceChildren(...entries.map((n) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.node = n.id;
      b.tabIndex = n.id === this.rover ? 0 : -1;
      localizedText(b, () => (GRAPH_COPY.listItem(n.name, BAND_WORD[n.band])));
      b.onclick = () => { this.select(n.id, true); };
      li.append(b);
      return li;
    }));
    if (focused !== undefined) nodeButton(els.list, focused)?.focus({ preventScroll: true });
  }
}
