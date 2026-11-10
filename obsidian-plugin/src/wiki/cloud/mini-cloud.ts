/**
 * The small knowledge cloud of 今天: a live miniature of the 知识图谱 page itself (user ruling 2026-10-21). One persistent
 * node, made once per open sidebar and moved (never rebuilt) when 今天 redraws: the `.ew-mini` box (click -> the
 * big map). A failed read shows the one 没能读到，重试 line of the goal rows under it (same store). One GraphCloud in
 * small mode (the page's own renderer, Sigma.js: same scene, layout and colours; no text at all, the whole graph fitted
 * into the box, no gestures) runs in the box for the life of the view and draws the GraphStore of the goal rows under
 * it (GET /wiki/graph, read when the
 * box is on screen and again when the last read is old): the thumbnail changes whenever the graph does. Its colours are
 * re-read on Obsidian's css-change, and it is destroyed on close.
 */
import { el } from "../../ui/ew";
import { TODAY_COPY } from "../today-copy";
import { GraphCloud, type GraphCloudHandle } from "../graph-page/sigma-cloud";
import { buildScene, type Scene } from "../graph-page/scene";
import type { GraphOut } from "../schema";
import type { GraphStore } from "../graph-page/store";
import { GoalProgress, type GoalProgressHost } from "./goal-progress";

export interface MiniCloudHost {
  /** Opens (or shows) the big map tab; `goal`: focused on that goal (map id), null: unfocused (the thumbnail). */
  openStarMap(goal?: string | null): void;
  /** The graph source and the goal rows under the cloud: the rows and the thumbnail read one GraphStore. */
  goals: GoalProgressHost;
  /** Obsidian's css-change (the theme switched); returns the unsubscribe. */
  onCssChange(fn: () => void): () => void;
}

export class MiniCloud {
  readonly root: HTMLDivElement;
  /** The cloud's own box (Sigma draws its canvases in it). */
  readonly canvas: HTMLElement;
  /** The goal rows (`div.gls`) and their failure line. */
  readonly goals: GoalProgress;
  private cloud: GraphCloudHandle | null = null;
  private graph: GraphOut | null = null;
  private scene: Scene | null = null;
  private store: GraphStore;
  private unsubStore: () => void;
  private readonly unCss: () => void;
  /** The empty graph's line. */
  private readonly none: HTMLElement;
  private destroyed = false;

  constructor(private readonly host: MiniCloudHost) {
    this.root = el("div", "mini");
    this.canvas = el("div", "sigma");
    this.none = el("div", "mini-none", () => (TODAY_COPY.mapEmpty));
    this.none.hidden = true;
    this.root.append(this.canvas, this.none);
    this.root.onclick = () => { host.openStarMap(null); };
    this.goals = new GoalProgress(host.goals);
    this.store = this.goals.graphStore;
    this.unsubStore = this.store.subscribe(() => { this.update(); });
    this.unCss = host.onCssChange(() => { this.cloud?.recolor(); });
    try {
      // Graph motion is enabled within EvoWiki, independently of Windows UI animations.
      this.cloud = GraphCloud(this.canvas, { tokens: this.root, small: true, font: () => "", reduced: () => false });
    } catch (e) {
      // No WebGL: the box stays empty; the rest of 今天 is unaffected.
      console.warn("EvoWiki: the small knowledge cloud could not start", e);
    }
    this.update();
  }

  /** Called after every redraw of 今天 (the nodes may have moved): follows a rebuilt store, reads it once when shown. */
  sync(): void {
    if (this.destroyed || !this.root.isConnected) return;
    this.goals.sync();                                   // reads the graph (once, and again when it is old)
    const current = this.goals.graphStore;
    if (current !== this.store) {
      this.unsubStore();
      this.store = current;
      this.unsubStore = current.subscribe(() => { this.update(); });
      this.update();
    }
    // The tokens live on the .ew ancestor: re-read them now that the canvas sits under it (cheap; also covers a pop-out).
    this.cloud?.recolor();
  }

  /** The graph the thumbnail draws now (tests). */
  get drawn(): GraphOut | null { return this.graph; }

  /** The scene of the current graph, handed to the cloud when it changes. Offline or failed: the box stays empty. */
  private update(): void {
    const s = this.store.state;
    const g = s.status === "ready" ? s.graph : null;
    if (g !== this.graph) {
      this.graph = g; this.scene = g !== null && g.nodes.length > 0 ? buildScene(g) : null;
      this.cloud?.setScene(this.scene);
      this.none.hidden = !(g !== null && g.nodes.length === 0);
      // the thumbnail is the whole graph, always
      this.cloud?.fit();
    }
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.cloud?.destroy(); this.cloud = null;
    this.unsubStore();
    this.unCss();
    this.goals.destroy();
  }
}
