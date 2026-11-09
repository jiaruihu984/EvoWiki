/**
 * K (P4: results under the cell detail = kNote): 我会 / 我不会 on a cell, a section or a whole concept, and the stores the
 * 知识星图 and the 知识图谱 page read from.
 */
import { WIKI_COPY } from "../copy";
import { cellLabel, opLine, opResult } from "../format";
import { sectionQuote } from "../section-quote";
import type { CatalogOut, CellBeliefOut, DeclareOut, Declared, KGridOut } from "../schema";
import type { GoalMembership } from "../starmap/model";
import { StarMapStore, type StarMapSource } from "../starmap/store";
import type { GraphSource } from "../graph-page/store";
import { MAX_STAR_PATHS, STAR_REREAD_MS, ok, changeMarks } from "./helpers";
import type { WikiCore } from "./core";
import type { Notes } from "./notes";
import type { Reads } from "./reads";

export class Knowledge {
  constructor(private readonly core: WikiCore, private readonly notes: Notes, private readonly reads: Reads) {}

  /** The star maps' shared store (built on first use), the K it last saw, and the pending debounced re-read. */
  starStore: StarMapStore | null = null;
  private starKSeen: KGridOut | null = null;
  private starTimer: ReturnType<typeof setTimeout> | null = null;
  private starUnsub: (() => void) | null = null;
  /** declareConcept calls running: they re-read the store once at the end instead of after every cell. */
  private conceptDeclares = 0;

  /** The controller's dispose: the pending re-read, the K subscription and the store end. */
  dispose(): void {
    if (this.starTimer !== null) clearTimeout(this.starTimer);
    this.starTimer = null;
    this.starUnsub?.(); this.starUnsub = null;
    this.starStore?.dispose();
  }

  /** Local only; the selected cell again clears the selection. */
  selectCell(cell: string | null): void {
    if (this.core.disposed) return;
    this.core.set({ kSelected: this.core.state.kSelected === cell ? null : cell });
  }

  /** 我会 / 我不会 / 清除声明: one POST declare; the cell is replaced in K at once; then P5 refresh. */
  async declareCell(cell: string, v: Declared | null): Promise<void> { await this.declareCellDone(cell, v); }

  /** declareCell, saying whether the declaration was recorded (false: blocked, busy or failed). */
  private async declareCellDone(cell: string, v: Declared | null): Promise<boolean> {
    if (!this.core.beginArea("k", { what: "declare", abortable: false })) return false;
    const ctx = this.core.currentCtx();
    let out: DeclareOut;
    try {
      out = await this.core.call(() => this.core.deps.api.declare(cell, v));
    } catch (e) { this.core.areaFailed("k", e); return false; }
    if (this.core.disposed) return false;
    this.replaceCell(out.cell);
    const changed = this.notes.noteChange(out.change);
    if (ctx !== null) this.core.touch(ctx, changeMarks(out.change));
    const hd = ctx !== null ? this.core.headings(ctx.pageId) : (): null => null;
    const label = cellLabel(this.conceptNameOf(out.cell.concept_id), out.cell.aspect);
    this.core.set({ kNote: ok(opLine(opResult(WIKI_COPY.declared(label), out.change, hd), true)) });
    await this.reads.refresh(ctx, changed, { kind: "area", area: "k" });
    return true;
  }

  /**
   * 「⋯ → 这里的概念我不会」: declares 不会 for every cell of the section (each once), one after the other; the first
   * failure stops (fail closed) and the Notice says how many were recorded. All recorded: 助手 opens on the section with
   * 重讲这一节 waiting in the composer; the note stays as it is until a draft is confirmed there.
   */
  async declareSectionDont(sid: string): Promise<void> {
    const view = this.core.actionView();
    const sec = view?.page.sections.find((x) => x.id === sid);
    if (!view || !sec) return;
    const cells = [...new Map(sec.cells.map((c) => [c.cell, c])).values()];
    if (cells.length === 0) return;
    let done = 0;
    for (const c of cells) {
      if (!(await this.declareCellDone(c.cell, "dont"))) break;
      done++;
    }
    if (this.core.disposed) return;
    const names = (xs: typeof cells): string[] => [...new Set(xs.map((c) => c.concept_name))];
    this.core.deps.notice?.(done === cells.length ? WIKI_COPY.dontDone
      : done === 0 ? WIKI_COPY.dontNone : WIKI_COPY.dontPartial(done, names(cells.slice(0, done))));
    if (done === cells.length) this.core.deps.askAssistant?.(view.page.page_id, view.page.title, sectionQuote(sec), WIKI_COPY.reteachDraft);
  }

  /** Replaces one cell of the loaded K grid with the server's copy (declare). */
  private replaceCell(cell: CellBeliefOut): void {
    const k = this.core.state.k;
    if (k.kind !== "ready") return;
    const concepts = k.value.concepts.map((c) => (c.id !== cell.concept_id ? c
      : { ...c, cells: c.cells.map((x) => (x.cell === cell.cell ? cell : x)) }));
    this.core.set({ k: { kind: "ready", value: { ...k.value, concepts } } });
  }

  /** Concept name for a cell label (P7): the K grid, then everything else loaded, then 未命名概念 (never the id). */
  private conceptNameOf(conceptId: string): string {
    const k = this.core.state.k;
    const fromK = k.kind === "ready" ? k.value.concepts.find((c) => c.id === conceptId)?.name : undefined;
    return fromK ?? this.core.names.concept(conceptId) ?? WIKI_COPY.unnamedConcept;
  }

  /**
   * Where the 知识星图 reads from: K and the catalog (its learning paths; a catalog that fails only costs those), plus the
   * concepts of each learning path (at most MAX_STAR_PATHS) as goal membership. Throws when K cannot be read.
   */
  starMapSource(): StarMapSource {
    return {
      demo: this.core.deps.demo,
      isOffline: () => this.core.deps.conn.state === "offline",
      load: async () => {
        if (!this.core.deps.configured()) throw new Error("not configured");
        const [k, catalog] = await Promise.all([
          this.core.call(() => this.core.deps.api.kGrid()),
          this.core.call(() => this.core.deps.api.catalog()).catch((): CatalogOut | null => null),
        ]);
        const paths = Array.isArray(catalog?.paths) ? catalog.paths.slice(0, MAX_STAR_PATHS) : [];
        const plans = await Promise.all(paths.map((p) => this.core.call(() => this.core.deps.api.getPath(p.path_id)).catch(() => null)));
        const goals: GoalMembership = { goals: [], membership: {} };
        plans.forEach((plan, i) => {
          if (plan === null) return;
          const id = paths[i]!.path_id;
          goals.goals.push({ id, name: plan.goal });
          goals.membership[id] = [...new Set(plan.pages.flatMap((pg) => pg.concepts.map((c) => c.concept_id)))];
        });
        return { k, catalog, goals };
      },
    };
  }

  /**
   * Where the 知识图谱 page reads the node graph from and sends its actions to (GET /wiki/graph, split / want,
   * path edits). Reads throw when the wiki is not set up; the store checks every answer against the wire contract.
   */
  graphSource(): GraphSource {
    const api = this.core.deps.api;
    return {
      demo: this.core.deps.demo,
      isOffline: () => this.core.deps.conn.state === "offline",
      load: async () => {
        if (!this.core.deps.configured()) throw new Error("not configured");
        return this.core.call(() => api.graph());
      },
      kChanged: (fn) => {
        const read = (): unknown => (this.core.state.k.kind === "ready" ? this.core.state.k.value : null);
        let seen = read();
        return this.core.subscribe(() => { const now = read(); if (now !== seen) { seen = now; if (now !== null) fn(); } });
      },
      split: (id) => this.core.call(() => api.splitNode(id)),
      want: (id, mapId) => this.core.call(() => api.wantNode(id, mapId)),
      // A path edit or 拆成多页 may have queued a page write: the watcher looks now.
      editPath: async (pathId, edit) => { const out = await this.core.call(() => api.editPath(pathId, edit)); this.core.watcher?.kick(); return out; },
      splitRow: async (pathId, key) => { const out = await this.core.call(() => api.splitRow(pathId, key)); this.core.watcher?.kick(); return out; },
      undoSplitRow: (pathId, key, changeId) => this.core.call(() => api.undoSplitRow(pathId, key, changeId)),
      ...(this.core.watcher === null ? {} : { writes: {
        snapshot: () => this.core.watcher?.snapshot()?.writes ?? null,
        // the 知识图谱 rows need the watcher even when 今天 never refreshed (it starts there otherwise)
        subscribe: (fn: () => void) => { this.core.watcher?.start(); return this.core.watcher?.subscribe(fn) ?? (() => undefined); },
        lookup: (ids: string[]) => this.core.call(() => this.core.deps.api.writes(ids)),
      } }),
      parkedGoals: async () => {
        if (!this.core.deps.configured()) throw new Error("not configured");
        const goals = await this.core.call(() => api.botGoals());
        return goals.filter((g) => g.parked).map((g) => g.path_id);
      },
      setParked: async (pathId, park) => { await this.core.call(() => api.botPark(pathId, park)); },
    };
  }

  /**
   * The one store the big star map and the small map in 今天 share: a read (or a 我会 / 我不会 from either) shows in
   * both. When K is re-read (the page changed, a declaration, a review) and a map has already been read, the store reads
   * again after a short pause, so the maps do not go stale.
   */
  starMapStore(): StarMapStore {
    if (this.starStore === null) {
      this.starStore = new StarMapStore(this.starMapSource());
      const k = this.core.state.k;
      this.starKSeen = k.kind === "ready" ? k.value : null;
      this.starUnsub = this.core.subscribe(() => { this.onKChanged(); });
    }
    return this.starStore;
  }

  private onKChanged(): void {
    const k = this.core.state.k.kind === "ready" ? this.core.state.k.value : null;
    if (k === this.starKSeen) return;
    this.starKSeen = k;
    const store = this.starStore;
    if (k === null || store === null || store.state.status === "idle" || this.conceptDeclares > 0) return;
    if (this.starTimer !== null) clearTimeout(this.starTimer);
    this.starTimer = setTimeout(() => { this.starTimer = null; if (!this.core.disposed) void store.refresh(); }, STAR_REREAD_MS);
  }

  /**
   * 星图里的「我会 / 我不会」 for a whole concept: declares it for each of the concept's aspects (cells) one after the
   * other, skipping those already declared the same; the first failure stops. One Notice says what was recorded; the
   * shared store is re-read once at the end. Resolves to whether every cell was recorded.
   */
  /** `quietOk`: the caller shows its own notice when everything was recorded (the 知识图谱 card's ewNotice); failures still notice here. */
  async declareConcept(conceptId: string, v: Declared, opts: { quietOk?: boolean } = {}): Promise<boolean> {
    if (this.core.disposed) return false;
    if (this.core.state.k.kind !== "ready") await this.reads.loadK(null);
    const k = this.core.state.k;
    const c = k.kind === "ready" ? k.value.concepts.find((x) => x.id === conceptId) : undefined;
    if (c === undefined) { this.core.deps.notice?.(WIKI_COPY.dontNone); return false; }
    const own = new Set(c.aspects);
    const mine = [...new Map(c.cells.filter((x) => own.has(x.aspect)).map((x) => [x.cell, x])).values()];
    if (mine.length === 0) { this.core.deps.notice?.(WIKI_COPY.conceptNoAspects); return false; }
    const todo = mine.filter((x) => x.declared !== v);
    let done = 0;
    this.conceptDeclares++;
    try {
      for (const cell of todo) {
        if (!(await this.declareCellDone(cell.cell, v))) break;
        done++;
      }
    } finally { this.conceptDeclares--; }
    if (this.core.disposed) return false;
    const all = done === todo.length;
    if (!(all && opts.quietOk === true)) {
      this.core.deps.notice?.(all ? WIKI_COPY.conceptDeclared(c.name, v) : done === 0 ? WIKI_COPY.dontNone : WIKI_COPY.conceptDeclarePartial(c.name));
    }
    if (this.conceptDeclares === 0) await this.starStore?.refresh();
    return all;
  }
}
