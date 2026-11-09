/**
 * 个性化 (tab id "engine"): its reads (engine, the teaching spec, prompts), 提示词设置's 保存 / 恢复默认, and 活动记录's
 * RSI changes (results in rsiNote: 撤销, 采用 / 不要 a code change). The preference writes are controller/personal.ts.
 */
import { ApiError, OfflineError, validationText } from "../../api/errors";
import { WIKI_COPY } from "../copy";
import { PROMPT_COPY } from "../prompt-copy";
import { errorText } from "../format";
import { conflictViews } from "../memory-conflicts";
import { rsiErrorText, undoneNotice } from "../rsi-changes";
import type { PromptStepOut } from "../schema";
import type { ResultNote } from "../state";
import { refreshText, bad } from "./helpers";
import type { WikiCore } from "./core";
import type { Reads } from "./reads";

/** The Bot's and 资料's parts of 个性化, and the materials drafts (the facade wires them). */
export interface EngineHooks {
  botLoadEngine(): Promise<void>;
  botLoadActions(): Promise<void>;
  sourcesLoad(): Promise<void>;
  reviseRow(id: string): { pageId: string | null } | null;
  reviseDecide(actionId: string, op: "approve" | "reject" | "undo"): Promise<void>;
}

export class Engine {
  constructor(private readonly core: WikiCore, private readonly reads: Reads, private readonly h: EngineHooks) {}

  /** Bumped by every 个性化 read: an older read's answer is dropped. */
  private engineSeq = 0;

  /**
   * 个性化: GET /wiki/engine (prompts + θ_infer) and GET /wiki/teach/spec (the preferences), reads only (no LLM), in
   * parallel and each failing on its own. Run when the tab is shown and by its 重试 links. What is not shown yet goes to
   * loading then the value or the error; what is shown stays while it is re-read and stays when the re-read fails.
   * Unconfigured: nothing is sent, both say so.
   */
  async loadEngine(): Promise<void> {
    if (this.core.disposed) return;
    if (!this.core.deps.configured()) {
      const error = { kind: "error" as const, message: WIKI_COPY.needsSettings };
      this.core.set({ engine: error, prefs: error, prompts: error, conflicts: error });
      return;
    }
    const seq = ++this.engineSeq;
    const s0 = this.core.state;
    const loading = { kind: "loading" as const };
    if (s0.engine.kind !== "ready" || s0.prefs.kind !== "ready" || s0.prompts.kind !== "ready") {
      this.core.set({ ...(s0.engine.kind !== "ready" ? { engine: loading } : {}),
        ...(s0.prompts.kind !== "ready" ? { prompts: loading } : {}),
        ...(s0.prefs.kind !== "ready" ? { prefs: loading } : {}) });
    }
    const live = (): boolean => !this.core.disposed && seq === this.engineSeq;
    const engine = async (): Promise<void> => {
      try {
        const value = await this.core.call(() => this.core.deps.api.engine());
        if (live()) this.core.set({ engine: { kind: "ready", value } });
      } catch (e) {
        if (live() && this.core.state.engine.kind !== "ready") this.core.set({ engine: { kind: "error", message: errorText(e) } });
      }
    };
    const rules = async (): Promise<void> => {
      try {
        const spec = await this.core.call(() => this.core.deps.api.teachSpec());
        if (live()) this.core.set({ prefs: { kind: "ready", value: spec } });
      } catch (e) {
        if (!live()) return;
        const error = { kind: "error" as const, message: errorText(e) };
        if (this.core.state.prefs.kind !== "ready") this.core.set({ prefs: error });
      }
    };
    const prompts = async (): Promise<void> => {
      try {
        const value = await this.core.call(() => this.core.deps.api.prompts());
        if (live()) this.core.set({ prompts: { kind: "ready", value } });
      } catch (e) {
        if (live() && this.core.state.prompts.kind !== "ready") this.core.set({ prompts: { kind: "error", message: errorText(e) } });
      }
    };
    const rsiSwitch = async (): Promise<void> => {
      try {
        const { on } = await this.core.call(() => this.core.deps.api.rsiSwitch());
        if (live()) this.core.set({ rsiSwitch: { kind: "ready", value: on } });
      } catch (e) {
        if (live() && this.core.state.rsiSwitch.kind !== "ready") this.core.set({ rsiSwitch: { kind: "error", message: errorText(e) } });
      }
    };
    await Promise.all([engine(), rules(), prompts(), rsiSwitch(), this.loadConflicts(live), this.h.botLoadEngine(), this.h.sourcesLoad()]);
  }

  /**
   * 待确认的记忆: GET MEMORY_CONFLICTS_PATH with the tab (no LLM). Each read replaces the card (处理 settles groups in
   * the chat; coming back to the tab shows what is left); a failed read shows the card's 重试 line only while none is shown.
   */
  private async loadConflicts(live: () => boolean = () => !this.core.disposed): Promise<void> {
    try {
      const groups = await this.core.call(() => this.core.deps.api.memoryConflicts());
      if (live()) this.core.set({ conflicts: { kind: "ready", value: conflictViews(groups, new Date()) } });
    } catch (e) {
      if (live() && this.core.state.conflicts.kind !== "ready") this.core.set({ conflicts: { kind: "error", message: errorText(e) } });
    }
  }

  /** The card's 重试. */
  reloadConflicts(): Promise<void> {
    if (this.core.disposed || !this.core.deps.configured()) return Promise.resolve();
    this.core.set({ conflicts: { kind: "loading" } });
    return this.loadConflicts();
  }

  /**
   * 个性化's red count needs the preferences waiting for confirmation before the tab is first shown: one read of the spec
   * with 今天's (no LLM). A failure is kept only while nothing is shown (the tab reads it again when opened).
   */
  async loadPrefs(): Promise<void> {
    if (this.core.disposed || !this.core.deps.configured()) return;
    try {
      const spec = await this.core.call(() => this.core.deps.api.teachSpec());
      if (!this.core.disposed) this.core.set({ prefs: { kind: "ready", value: spec } });
    } catch (e) {
      if (!this.core.disposed && this.core.state.prefs.kind !== "ready") this.core.set({ prefs: { kind: "error", message: errorText(e) } });
    }
    await this.loadConflicts();
  }

  /** A failed save's notice: a 422 says its own plain reason (its list of messages joined), anything else the usual line. */
  private promptFailure(e: unknown): string {
    return e instanceof ApiError && e.status === 422 && e.detail.trim() !== "" ? validationText(e.detail) : errorText(e);
  }

  /** After a prompt / rules write: 活动记录's changes and the Bot's actions are read again together, quietly (the new ledger row). */
  rereadChanges(): void {
    void Promise.all([this.rereadRsi(), this.h.botLoadActions()]);
  }

  /** Puts one step's answer into the shown list (a list not read yet stays as it is). */
  private setPromptStep(out: PromptStepOut): void {
    const p = this.core.state.prompts;
    if (p.kind !== "ready") return;
    this.core.set({ prompts: { kind: "ready", value: p.value.map((x) => (x.step === out.step ? out : x)) } });
  }

  /**
   * 保存 on a step: PUT. Success: 「已保存。以后这一步按这个写。」 and the step in the list. Failure (a 422 with its reason,
   * offline, ...): the reason as a notice; nothing changes, the typed text stays in the editor. True when saved.
   */
  async savePrompt(step: string, text: string): Promise<boolean> {
    if (this.core.disposed) return false;
    const blocked = this.core.blocked();
    if (blocked) { this.core.deps.notice?.(blocked); return false; }
    try {
      const out = await this.core.call(() => this.core.deps.api.putPrompt(step, text));
      if (this.core.disposed) return false;
      this.setPromptStep(out);
      this.core.deps.notice?.(PROMPT_COPY.saved);
      this.rereadChanges();
      return true;
    } catch (e) {
      if (!this.core.disposed) this.core.deps.notice?.(this.promptFailure(e));
      return false;
    }
  }

  /** 恢复默认 on a step: DELETE. The default text on success (the editor shows it), else null with the reason as a notice. */
  async resetPrompt(step: string): Promise<string | null> {
    if (this.core.disposed) return null;
    const blocked = this.core.blocked();
    if (blocked) { this.core.deps.notice?.(blocked); return null; }
    try {
      const out = await this.core.call(() => this.core.deps.api.resetPrompt(step));
      if (this.core.disposed) return null;
      this.setPromptStep(out);
      this.core.deps.notice?.(PROMPT_COPY.resetDone);
      this.rereadChanges();
      return out.text;
    } catch (e) {
      if (!this.core.disposed) this.core.deps.notice?.(this.promptFailure(e));
      return null;
    }
  }

  // ---------------- 最近为你改了什么 (RSI changes; results in rsiNote) ----------------

  /** 设置 · 自动改进: the switch shows only what the server answered (a refused write leaves it as it was, with the notice). */
  async setRsiSwitch(on: boolean): Promise<void> {
    if (this.core.disposed) return;
    try {
      const out = await this.core.call(() => this.core.deps.api.putRsiSwitch(on));
      this.core.set({ rsiSwitch: { kind: "ready", value: out.on } });
    } catch (e) {
      this.core.deps.notice?.(errorText(e));
      this.core.set({ rsiSwitch: { ...this.core.state.rsiSwitch } });
    }
  }

  /** 重试 under a failed list: one GET /wiki/rsi/changes (loading, then the list or the error). */
  async reloadRsi(): Promise<void> {
    if (this.core.disposed || this.core.state.rsi.kind === "loading" || this.core.state.busy?.what === "rsi") return;
    this.core.set({ rsiNote: null });
    await this.reads.loadRsi(null);
  }

  /** A quiet re-read for the Bot (the list stays shown; a failure keeps it). One at a time: a request made meanwhile re-runs once after it. */
  private rsiQuiet: Promise<void> | null = null;
  private rsiAgain = false;
  rereadRsi(): Promise<void> {
    if (this.core.disposed) return Promise.resolve();
    this.core.watcher?.kick();   // a decided action (approve, confirm...) may have started a write
    if (this.rsiQuiet !== null) { this.rsiAgain = true; return this.rsiQuiet; }
    this.rsiQuiet = (async () => {
      do { this.rsiAgain = false; await this.reads.loadRsi(() => undefined); } while (this.rsiAgain && !this.core.disposed);
    })().finally(() => { this.rsiQuiet = null; });
    return this.rsiQuiet;
  }

  /** 撤销 (only offered when can_undo): POST undo, then re-read the list. */
  async undoPatch(patchId: string): Promise<void> {
    // a confirmed materials draft (its change row's id is the ledger row's): the ledger's undo, then the note follows
    if (this.h.reviseRow(patchId) !== null) { await this.h.reviseDecide(patchId, "undo"); return; }
    const r = this.core.state.rsi;
    const was = r.kind === "ready" ? r.value.find((c) => c.patch_id === patchId) : undefined;
    await this.decidePatch(patchId, (id) => this.core.deps.api.undoPatch(id), undoneNotice(was));
  }

  /** 采用 a code change waiting for review: POST approve (it goes live later), then re-read the list. */
  async approvePatch(patchId: string): Promise<void> {
    await this.decidePatch(patchId, (id) => this.core.deps.api.approvePatch(id), WIKI_COPY.rsiAdopted);
  }

  /** 不要 a code change waiting for review: POST discard (it never goes live), then re-read the list. */
  async discardPatch(patchId: string): Promise<void> {
    await this.decidePatch(patchId, (id) => this.core.deps.api.discardPatch(id), WIKI_COPY.rsiDiscarded);
  }

  /**
   * One 撤销: nothing while any write is in flight or in the demo; offline / unconfigured → the text in the row, nothing
   * sent. Otherwise one POST. Success: `done` is a Notice and the list is re-read; failure: its plain sentence
   * (rsiErrorText: never the server's detail) stays in the row (a round in progress: only a Notice), and the list
   * is re-read too (a 404 / 409 means it moved on) unless the server could not be reached. A failed re-read says so.
   */
  private async decidePatch(patchId: string, send: (id: string) => Promise<unknown>, done: string): Promise<void> {
    if (this.core.disposed || this.core.state.busy || this.core.deps.demo) return;
    const err = this.core.blocked();
    if (err) { this.core.set({ rsiNote: { patchId, note: bad(err) } }); return; }
    this.core.set({ busy: { what: "rsi", sid: patchId, abortable: false }, rsiNote: null });
    let note: ResultNote | null = null;
    let busy = false;
    let reread = true;
    try {
      await this.core.call(() => send(patchId));
    } catch (e) {
      const text = rsiErrorText(e, patchId);
      busy = text === WIKI_COPY.rsiBusy;
      if (busy) this.core.deps.notice?.(text);
      else note = bad(text);
      reread = !(e instanceof OfflineError);
    }
    if (this.core.disposed) return;
    if (busy) { /* said in a notice: nothing stays on the row */ } else if (note === null) {
      this.core.deps.notice?.(done);
      // A decided change may change the preferences and the θ_infer rows: re-read them once they were read.
      if (this.core.state.engine.kind !== "idle") void this.loadEngine();
    } else {
      this.core.set({ rsiNote: { patchId, note } });
    }
    if (reread) {
      await this.reads.loadRsi((e) => {
        const cur = this.core.state.rsiNote;
        if (cur !== null) this.core.set({ rsiNote: { ...cur, note: { ...cur.note, sub: refreshText(e) } } });
        else this.core.set({ rsiNote: { patchId: null, note: bad(refreshText(e)) } });
      });
    }
    this.core.set({ busy: null });
  }
}
