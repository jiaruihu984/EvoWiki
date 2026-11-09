/**
 * GraphStore: the node graph the 知识图谱 page draws (GET /wiki/graph), and the page's actions. Reading happens only on
 * `refresh()`; every action answers with what the page needs to redraw (the graph, or a path) and the store applies it.
 * Every answer is checked against the wire contract first (parse.ts): a malformed answer is a failure, never drawn.
 * A failed read keeps the graph already shown (stale) or, without one, becomes the error state; a failed action
 * rejects (the page says so) and leaves the graph as it was.
 */
import type { WriteOut } from "../schema";
import type { GraphOut, PathEditIn, PathEditOut, PathItemOut } from "../schema";
import { graphBus } from "./bus";
import { parseGraph, parseNodeAction, parsePathEdit, parseSplit } from "./parse";

/** Where the graph comes from (the wiki controller supplies it from the WikiApi; tests supply fakes). */
export interface GraphSource {
  load(): Promise<unknown>;
  split(nodeId: string): Promise<unknown>;
  want(nodeId: string, mapId: string): Promise<unknown>;
  editPath(pathId: string, edit: PathEditIn): Promise<unknown>;
  /** 拆成多页 (POST /wiki/paths/{id}/rows/{key}/split) and its undo. */
  splitRow(pathId: string, pageKey: string): Promise<unknown>;
  undoSplitRow(pathId: string, pageKey: string, changeId: string): Promise<unknown>;
  /** The live page writes (the shared background watcher's last answer of GET /wiki/writes); absent = no live rows. */
  writes?: {
    snapshot(): readonly WriteOut[] | null; subscribe(fn: () => void): () => void;
    /** How these pages' writes ended (GET /wiki/writes?page_ids=, free): the snapshot holds live writes only. */
    lookup(pageIds: string[]): Promise<readonly WriteOut[]>;
  };
  /** B5: the path ids of the goals parked for the Bot (GET /wiki/bot/goals); absent = this source has none. */
  parkedGoals?(): Promise<string[]>;
  /** B5: 搁置 / 恢复 a goal for the Bot. */
  setParked?(pathId: string, park: boolean): Promise<void>;
  /** K was read again (an answer, a declaration, a review): an open map reads the graph again. Returns the unsubscribe; absent = no such signal. */
  kChanged?(fn: () => void): () => void;
  /** The server cannot be reached right now. */
  isOffline(): boolean;
  /** The demo (fake data). */
  readonly demo: boolean;
}

export interface GraphState {
  status: "idle" | "loading" | "ready" | "error";
  graph: GraphOut | null;
  /** The last read failed but an older graph is still shown. */
  stale: boolean;
  offline: boolean;
}

const INITIAL: GraphState = { status: "idle", graph: null, stale: false, offline: false };

export class GraphStore {
  private _state: GraphState = INITIAL;
  private readonly subs = new Set<() => void>();
  private ticket = 0;
  private disposed = false;
  /** `announce`: the 知识图谱 page's store - its changes tell 今天 (graphBus). */
  constructor(private readonly source: GraphSource, private readonly announce = false) {}

  get state(): GraphState { return this._state; }
  get demo(): boolean { return this.source.demo; }

  subscribe(fn: () => void): () => void {
    this.subs.add(fn);
    return () => { this.subs.delete(fn); };
  }

  async refresh(): Promise<void> {
    if (this.disposed) return;
    const mine = ++this.ticket;
    const prev = this._state;
    if (prev.graph === null) this.set({ ...prev, status: "loading", stale: false, offline: false });
    try {
      const graph = parseGraph(await this.source.load());
      if (this.disposed || mine !== this.ticket) return;
      this.set({ status: "ready", graph, stale: false, offline: false });
    } catch {
      if (this.disposed || mine !== this.ticket) return;
      const offline = this.source.isOffline();
      if (prev.graph !== null) this.set({ ...prev, status: "ready", stale: true, offline });
      else this.set({ ...INITIAL, status: "error", offline });
    }
  }

  /** 看不懂: resolves to the children the node was split into (already in the graph). */
  async split(nodeId: string): Promise<string[]> {
    const out = parseSplit(await this.source.split(nodeId));
    this.applyGraph(out.graph);
    return out.children;
  }

  /** 我想学这个: the new row is in the answered path. */
  async want(nodeId: string, mapId: string): Promise<void> {
    await this.afterNodeAction(await this.source.want(nodeId, mapId));
  }

  private async afterNodeAction(answer: unknown): Promise<void> {
    const out = parseNodeAction(answer);
    if (out.path_id !== null) this.setPath(out.path_id, out.path);
    await this.refresh();
  }

  /** 拆成多页: the row is replaced by one row per part and the graph grew them; resolves to the undo token. */
  async splitRow(pathId: string, pageKey: string): Promise<PathEditOut> {
    const out = parsePathEdit(await this.source.splitRow(pathId, pageKey));
    this.setPath(pathId, out.path);
    await this.refresh();
    return out;
  }

  async undoSplitRow(pathId: string, pageKey: string, changeId: string): Promise<void> {
    const out = parsePathEdit(await this.source.undoSplitRow(pathId, pageKey, changeId));
    this.setPath(pathId, out.path);
    await this.refresh();
  }

  /** Sends one path edit; the path the server answers replaces the goal's path. */
  async editPath(pathId: string, edit: PathEditIn): Promise<void> {
    const out = parsePathEdit(await this.source.editPath(pathId, edit));
    this.setPath(pathId, out.path);
  }

  /** Shows `path` as the path of `pathId` right away (an optimistic edit, or the server's answer). */
  setPath(pathId: string, path: PathItemOut[]): void {
    const g = this._state.graph;
    if (this.disposed || g === null) return;
    const goals = g.goals.map((x) => ({ ...x, ...(x.path_id === pathId ? { path } : {}),
      paths: x.paths.map((p) => (p.path_id === pathId ? { ...p, path } : p)) }));
    this.set({ ...this._state, graph: { ...g, goals } });
  }

  dispose(): void {
    this.disposed = true;
    this.subs.clear();
  }

  private applyGraph(graph: GraphOut): void {
    if (this.disposed) return;
    // An action's answer is newer than a read still on its way.
    this.ticket++;
    this.set({ status: "ready", graph, stale: false, offline: false });
  }

  private set(next: GraphState): void {
    const changed = next.graph !== this._state.graph;
    this._state = next;
    for (const fn of [...this.subs]) fn();
    if (this.announce && changed && next.graph !== null) graphBus.emit();
  }
}
