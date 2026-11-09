/**
 * 个性化's preference writes (app.js adoptPending / rejectPending / manageView) and its local view state. Every write is one
 * call on /wiki/teach/items (no LLM); the answer is the whole spec, which replaces what 我的学习偏好 shows. The backend's
 * write checkpoint (rules/authority.py) records the decisions: an edit or take-over of a pending rule = reaccepted, a
 * pending rule deleted = rejected (never proposed again), any other rule deleted = revoked, a text added back = reaccepted.
 * A failure is a notice with its reason; the spec is read again so what is shown is what the server has.
 */
import { ApiError, validationText } from "../../api/errors";
import { errorText } from "../format";
import { PERSONAL_COPY as C } from "../personal-copy";
import type { TeachSpecOut } from "../schema";
import type { PersonalUi } from "../state";
import type { WikiCore } from "./core";

export interface PersonalHooks {
  /** Re-reads 个性化 (the spec, after a failed write). */
  loadEngine(): Promise<void>;
  /** A rule write adds a change row (活动记录) and may land a ledger row: both are re-read, quietly. */
  rereadChanges(): void;
}

export class Personal {
  constructor(private readonly core: WikiCore, private readonly h: PersonalHooks) {}

  /** 管理偏好 / 修改 / 活动记录 / 更早: local only, no read. Leaving 管理偏好 also ends an edit. */
  view(patch: Partial<PersonalUi>): void {
    if (this.core.disposed) return;
    const next = { ...this.core.state.personal, ...patch };
    if (patch.manage === false) next.edit = null;
    this.core.set({ personal: next });
  }

  /** Text typed in a textarea: kept in the state WITHOUT notifying (a redraw would take the focus from the field). */
  draft(key: string, text: string): void {
    if (this.core.disposed) return;
    const p = this.core.state.personal;
    this.core.state = { ...this.core.state, personal: { ...p, drafts: { ...p.drafts, [key]: text } } };
  }

  private forget(key: string): void {
    const p = this.core.state.personal;
    if (!(key in p.drafts)) return;
    const drafts = { ...p.drafts };
    delete drafts[key];
    this.core.state = { ...this.core.state, personal: { ...p, drafts } };
  }

  private item(id: string): { text: string; authority: string } | null {
    const p = this.core.state.prefs;
    return p.kind === "ready" ? p.value.items.find((i) => i.item_id === id) ?? null : null;
  }

  /**
   * One write: nothing while another write runs (busy), offline or unconfigured (a notice says why). Success: the spec
   * shown, `done` as a notice, the changes re-read. True when it went through.
   */
  private async write(send: () => Promise<TeachSpecOut>, done: string): Promise<boolean> {
    if (this.core.disposed) return false;
    const blocked = this.core.blocked();
    if (blocked) { this.core.deps.notice?.(blocked); return false; }
    if (this.core.state.busy !== null) return false;
    this.core.set({ busy: { what: "prefs", abortable: false } });
    try {
      const spec = await this.core.call(send);
      if (this.core.disposed) return false;
      this.core.set({ busy: null, prefs: { kind: "ready", value: spec } });
      this.core.deps.notice?.(done);
      this.h.rereadChanges();
      return true;
    } catch (e) {
      if (this.core.disposed) return false;
      this.core.set({ busy: null });
      this.core.deps.notice?.(e instanceof ApiError && e.status === 422 && e.detail.trim() !== "" ? validationText(e.detail) : errorText(e));
      void this.h.loadEngine();
      return false;
    }
  }

  /** 设为我的偏好: the text as the user left it. Changed → PATCH {text} (the rule becomes theirs); unchanged → take_over. */
  async adopt(id: string, text: string): Promise<boolean> {
    const t = text.trim();
    if (t === "") { this.core.deps.notice?.(C.emptyPref); return false; }
    const cur = this.item(id);
    const api = this.core.deps.api;
    const send = cur !== null && cur.text.trim() === t ? () => api.takeOverSpecItem(id) : () => api.editSpecItem(id, { text: t });
    const ok = await this.write(send, C.adopted);
    if (ok) this.forget(`p:${id}`);
    return ok;
  }

  /** 不采用: DELETE (the backend remembers it: it is not proposed again). */
  async reject(id: string): Promise<boolean> {
    const ok = await this.write(() => this.core.deps.api.deleteSpecItem(id), C.rejected);
    if (ok) this.forget(`p:${id}`);
    return ok;
  }

  /** 管理偏好 · 保存: PATCH {text} (an AI 优化 rule edited becomes the user's); an unchanged text only closes the editor. */
  async save(id: string, text: string): Promise<boolean> {
    const t = text.trim();
    if (t === "") { this.core.deps.notice?.(C.emptyPref); return false; }
    if (this.item(id)?.text.trim() === t) { this.forget(`e:${id}`); this.view({ edit: null }); return true; }
    const ok = await this.write(() => this.core.deps.api.editSpecItem(id, { text: t }), C.saved);
    if (ok) { this.forget(`e:${id}`); this.view({ edit: null }); }
    return ok;
  }

  /** 管理偏好 · 删除: DELETE (revoked: it shows under 没采用和撤回的, where 恢复 brings it back). */
  remove(id: string): Promise<boolean> {
    return this.write(() => this.core.deps.api.deleteSpecItem(id), C.deleted);
  }

  /** 恢复: the text added again as the user's rule (POST: the checkpoint records reaccepted). */
  restore(text: string): Promise<boolean> {
    return this.write(() => this.core.deps.api.addSpecItem({ text: text.trim(), scope: "any", domain: null }), C.restored);
  }
}
