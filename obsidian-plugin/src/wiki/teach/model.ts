/**
 * 样板页 (the teaching spec's exemplars) for the settings section: its state and its requests. A small class of its own,
 * not WikiController state (R6.2): the section holds one per draw, so a new server / user id gets a fresh one and
 * whatever the old one still had in flight is dropped (dispose). Adding an exemplar is said in the assistant (照这一页
 *的写法写), not here; the section lists them and deletes one.
 *
 * A delete carries ?base_version=<the version shown>; a success replaces the shown spec with the TeachSpecOut it
 * returns (no GET). 409 → re-read + TEACH_COPY.conflict; 404 → re-read + exemplarGone. Server detail text is never
 * shown: 422 gets the plugin's own sentence, everything else goes through errorText.
 */
import { ApiError } from "../../api/errors";
import type { ConnState } from "../../connection";
import { SETTINGS_COPY, TEACH_COPY } from "../../copy";
import type { WikiApi } from "../api";
import { WIKI_COPY } from "../copy";
import { errorText } from "../format";
import type { TeachSpecOut } from "../schema";
import type { ResultNote } from "../state";

/** What the section works on: the current identity's wiki client (the fake in the demo), and its gates. */
export interface TeachPort {
  api: WikiApi;
  /** Server URL + user id set (always true for the demo). */
  configured(): boolean;
  conn: { readonly state: ConnState; track<T>(p: Promise<T>): Promise<T> };
  /**
   * The demo wiki (FakeWikiApi) is open: the 样板页 section writes nothing and says so (SETTINGS_COPY.demoNoEdit), so
   * nothing looks saved that is not. Absent = false.
   */
  demo?: boolean;
}

export type TeachLoad = { kind: "loading" } | { kind: "error"; message: string } | { kind: "ready"; spec: TeachSpecOut };
export type TeachBusy = null | "write";
export interface TeachState {
  spec: TeachLoad;
  /** One request at a time (the buttons are disabled while one runs). */
  busy: TeachBusy;
  /** The section's result line (a failed 删除). */
  note: ResultNote | null;
}
export type TeachResult = { ok: true } | { ok: false; message: string };

const bad = (text: string): ResultNote => ({ text, tone: "error" });
const status = (e: unknown): number | null => (e instanceof ApiError ? e.status : null);

export class TeachModel {
  private _state: TeachState = { spec: { kind: "loading" }, busy: null, note: null };
  private readonly fns = new Set<() => void>();
  private disposed = false;
  /** Bumped by every GET: an older GET's answer is dropped. */
  private loadSeq = 0;

  constructor(private readonly port: TeachPort) {}

  get state(): TeachState { return this._state; }

  subscribe(fn: () => void): () => void { this.fns.add(fn); return () => { this.fns.delete(fn); }; }

  /** The section was closed or redrawn for another identity: nothing more is shown or sent from here. */
  dispose(): void { this.disposed = true; this.fns.clear(); }

  /** One GET /wiki/teach/spec (loading, then the spec or a plain error). Unconfigured: needsSettings, nothing sent. */
  async open(): Promise<void> {
    if (this.disposed) return;
    if (!this.port.configured()) { this.set({ spec: { kind: "error", get message() { return SETTINGS_COPY.needsSettings; } } }); return; }
    this.set({ spec: { kind: "loading" } });
    await this.reload();
  }

  /**
   * GET the spec. True when it landed. A failure replaces the shown spec with the error when `keep` is false; with
   * `keep` the shown spec stays (its version may be stale; a write on it gets 409 and reads again).
   */
  private async reload(keep = false): Promise<boolean> {
    const seq = ++this.loadSeq;
    try {
      const spec = await this.port.conn.track(this.port.api.teachSpec());
      if (this.disposed || seq !== this.loadSeq) return false;
      this.set({ spec: { kind: "ready", spec } });
      return true;
    } catch (e) {
      if (this.disposed || seq !== this.loadSeq) return false;
      if (!keep || this._state.spec.kind !== "ready") this.set({ spec: { kind: "error", message: errorText(e) } });
      return false;
    }
  }

  /** 删除 (no confirmation): the result goes to the section's note only when it failed. */
  async deleteExemplar(exemplarId: string): Promise<void> {
    const r = await this.write((base) => this.port.api.deleteExemplar(exemplarId, base), TEACH_COPY.exemplarDeleteFailed,
      { landed: TEACH_COPY.exemplarGone, failed: TEACH_COPY.exemplarGoneRereadFailed });
    if (!this.disposed && !r.ok) this.set({ note: bad(r.message) });
  }

  /** Why a request may not go out now (null = go): busy, unconfigured, offline, or no spec shown. */
  private gate(): string | null {
    if (this.disposed) return WIKI_COPY.failed;
    if (this._state.busy !== null) return TEACH_COPY.busy;
    if (!this.port.configured()) return SETTINGS_COPY.needsSettings;
    if (this.port.conn.state === "offline") return WIKI_COPY.offline;
    if (this._state.spec.kind !== "ready") return TEACH_COPY.notReady;
    return null;
  }

  /**
   * A spec write on the shown version: success → the returned spec is shown. 409 → read again (conflict); 404 → read
   * again (`gone`); 422 → `invalid`; else errorText.
   */
  private async write(f: (base: number) => Promise<TeachSpecOut>, invalid: string,
                      gone: { landed: string; failed: string }): Promise<TeachResult> {
    const gate = this.gate();
    if (gate !== null) return { ok: false, message: gate };
    const cur = this._state.spec;
    if (cur.kind !== "ready") return { ok: false, message: TEACH_COPY.notReady };
    this.set({ busy: "write", note: null });
    try {
      const spec = await this.port.conn.track(f(cur.spec.version));
      if (this.disposed) return { ok: false, message: WIKI_COPY.failed };
      this.loadSeq++;               // a GET still out (none should be) must not replace this newer spec
      this.set({ busy: null, spec: { kind: "ready", spec } });
      return { ok: true };
    } catch (e) {
      if (this.disposed) return { ok: false, message: WIKI_COPY.failed };
      const st = status(e);
      let message: string;
      if (st === 409 || st === 404) {
        const landed = await this.reload(true);
        if (this.disposed) return { ok: false, message: WIKI_COPY.failed };
        message = st === 404 ? (landed ? gone.landed : gone.failed)
          : landed ? TEACH_COPY.conflict : TEACH_COPY.conflictRereadFailed;
      } else if (st === 422) message = invalid;
      else message = errorText(e);
      this.set({ busy: null });
      return { ok: false, message };
    }
  }

  private set(patch: Partial<TeachState>): void {
    if (this.disposed) return;
    this._state = { ...this._state, ...patch };
    for (const fn of [...this.fns]) fn();
  }
}
