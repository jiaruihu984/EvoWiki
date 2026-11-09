/**
 * StarMapStore: holds the current StarMap (学点新的's existing map). Reading happens only on `refresh()`. The StarMap is
 * treated as immutable; the cloud lays it out itself (graph-page/sigma-cloud.ts).
 */
import type { CatalogOut, KGridOut } from "../schema";
import { buildStarMap, type GoalMembership, type StarMap } from "./model";

export interface StarMapData { k: KGridOut; catalog: CatalogOut | null; goals: GoalMembership }
/** Where the data comes from (the wiki controller supplies it; tests supply fakes). */
export interface StarMapSource {
  load(): Promise<StarMapData>;
  /** The server cannot be reached right now. */
  isOffline(): boolean;
  /** The demo (fake data). */
  readonly demo: boolean;
}

export interface StarMapState {
  status: "idle" | "loading" | "ready" | "error";
  map: StarMap | null;
  goals: { id: string; name: string }[];
  /** The last read failed but an older map is still shown. */
  stale: boolean;
  offline: boolean;
}

const INITIAL: StarMapState = { status: "idle", map: null, goals: [], stale: false, offline: false };

export class StarMapStore {
  private _state: StarMapState = INITIAL;
  private readonly subs = new Set<() => void>();
  private ticket = 0;
  private disposed = false;
  constructor(private readonly source: StarMapSource) {}

  get state(): StarMapState { return this._state; }
  get demo(): boolean { return this.source.demo; }

  subscribe(fn: () => void): () => void {
    this.subs.add(fn);
    return () => { this.subs.delete(fn); };
  }

  /** Reads K and the catalog. A failure keeps the previous map (stale) or, without one, becomes the error state. */
  async refresh(): Promise<void> {
    if (this.disposed) return;
    const mine = ++this.ticket;
    const prev = this._state;
    if (prev.map === null) this.set({ ...prev, status: "loading", stale: false, offline: false });
    try {
      const data = await this.source.load();
      if (this.disposed || mine !== this.ticket) return;
      const map = buildStarMap(data.k, data.goals);
      const present = new Set(map.nodes.flatMap((n) => n.goalIds));
      this.set({
        status: "ready", map, stale: false, offline: false,
        goals: data.goals.goals.filter((g) => present.has(g.id)).map((g) => ({ id: g.id, name: g.name })),
      });
    } catch {
      if (this.disposed || mine !== this.ticket) return;
      const offline = this.source.isOffline();
      if (prev.map !== null) this.set({ ...prev, status: "ready", stale: true, offline });
      else this.set({ ...INITIAL, status: "error", offline });
    }
  }

  dispose(): void {
    this.disposed = true;
    this.subs.clear();
  }

  private set(next: StarMapState): void {
    this._state = next;
    for (const fn of [...this.subs]) fn();
  }
}
