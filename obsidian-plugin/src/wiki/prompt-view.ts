import { localizedAttribute } from "../i18n/index";
/**
 * 提示词设置 editors: a main-area tab per step (app.js renderPrompt). `div.ew` > `div.ew-scroll` > `div.ew-set`: h1 (its
 * name), its `format_note` as a `.ew-muted` line when it has one (its wording may not fit the step's new output),
 * `textarea.ew-ped#prompt-edit` (aria-label 「<名>的提示词」), a row with 保存 (primary) and 恢复默认. The texts live in the
 * wiki controller (GET /wiki/prompts); saving and resetting go through it, which says the result in a notice. A failed save
 * keeps what is typed. The view draws again only when its data first arrives, so typing is never replaced by a re-read.
 * `key` is the step key (kept in the leaf's state). The teaching rules are edited in 个性化 · 管理偏好.
 */
import { ItemView, type ViewStateResult, type WorkspaceLeaf } from "obsidian";
import { btn, el } from "../ui/ew";
import type { WikiController } from "./controller";
import { PROMPT_COPY } from "./prompt-copy";

export const PROMPT_VIEW_TYPE = "evowiki-prompt";
export interface PromptHost {
  /** The wiki controller in force now (read at call time). */
  controller(): WikiController;
}

export class PromptView extends ItemView {
  private key = "";
  private unsub: (() => void) | null = null;
  /** Whether the editor (not the loading / failed line) is drawn. */
  private drawn = false;
  private working = false;
  /** The editor's textarea and the text it was last filled with from the controller (an edit shows as a difference). */
  private ta: HTMLTextAreaElement | null = null;
  private shown = "";
  private headed = false;

  constructor(leaf: WorkspaceLeaf, private readonly host: PromptHost) { super(leaf); }

  getViewType(): string { return PROMPT_VIEW_TYPE; }
  /** The step key this editor is for. */
  get stepKey(): string { return this.key; }
  getDisplayText(): string { return this.name() ?? PROMPT_COPY.title; }
  override getIcon(): string { return "file-pen-line"; }

  /** Test hook: the key without a workspace (setState needs Obsidian's View). */
  setStateKey(key: string): void { this.key = key; }

  override getState(): Record<string, unknown> { return { key: this.key }; }
  override async setState(state: unknown, result: ViewStateResult): Promise<void> {
    const k = (state as { key?: unknown } | null)?.key;
    if (typeof k === "string" && k !== "") this.key = k;
    await super.setState(state, result);
    if (this.contentEl.isConnected && this.unsub !== null) this.draw();
  }

  override onOpen(): Promise<void> {
    this.draw();
    this.unsub = this.host.controller().subscribe(() => this.onState());
    const c = this.host.controller();
    if (c.state.prompts.kind !== "ready") void c.loadEngine();
    return Promise.resolve();
  }

  override onClose(): Promise<void> {
    this.unsub?.();
    this.unsub = null;
    this.contentEl.replaceChildren();
    return Promise.resolve();
  }

  /** The step's learner-facing name (null until the list is read). */
  private name(): string | null {
    const p = this.host.controller().state.prompts;
    const step = p.kind === "ready" ? p.value.find((x) => x.step === this.key) : undefined;
    return step ? step.name : null;
  }

  /** A controller change: draw when the data first arrives; later, bring in a text that changed elsewhere (an undo) unless it is being edited. */
  private onState(): void {
    if (!this.drawn) this.draw(); else this.refreshText();
    if (!this.headed && this.name() !== null) { this.headed = true; (this.leaf as WorkspaceLeaf & { updateHeader?: () => void }).updateHeader?.(); }
  }

  /** The text the controller holds for this editor now (null while it is not read). */
  private current(): string | null {
    const s = this.host.controller().state;
    return s.prompts.kind === "ready" ? (s.prompts.value.find((x) => x.step === this.key)?.text ?? null) : null;
  }

  private refreshText(): void {
    const now = this.current();
    if (this.ta === null || now === null || now === this.shown) return;
    if (this.ta.value === this.shown) this.ta.value = now;      // not edited: show the new text
    this.shown = now;
  }

  private draw(): void {
    const root = el("div");
    root.className = "ew";
    const scroll = root.appendChild(el("div", "scroll"));
    const page = scroll.appendChild(el("div", "set"));
    this.contentEl.replaceChildren(root);
    this.drawn = false;
    this.ta = null;
    this.drawStep(page);
  }

  private line(page: HTMLElement, h1: string, text: string): void {
    page.append(el("h1", "", h1), el("div", "muted", text));
  }

  private drawStep(page: HTMLElement): void {
    const c = this.host.controller();
    const p = c.state.prompts;
    if (p.kind !== "ready") { this.line(page, PROMPT_COPY.title, p.kind === "error" ? p.message : PROMPT_COPY.loading); return; }
    const step = p.value.find((x) => x.step === this.key);
    if (step === undefined) { this.line(page, PROMPT_COPY.title, PROMPT_COPY.gone); return; }
    this.drawn = true;
    const ta = el("textarea", "ped");
    ta.id = "prompt-edit";
    ta.value = step.text;
    this.ta = ta;
    this.shown = step.text;
    localizedAttribute(ta, "aria-label", () => (PROMPT_COPY.promptLabel(step.name)));
    const row = el("div", "row");
    row.style.marginTop = "12px";
    const save = btn(() => (PROMPT_COPY.save), "primary", () => { void this.guard(save, reset, () => c.savePrompt(this.key, ta.value)); });
    const reset = btn(() => (PROMPT_COPY.reset), "", () => {
      void this.guard(save, reset, async () => { const t = await c.resetPrompt(this.key); if (t !== null) ta.value = t; return t !== null; });
    });
    row.append(save, reset);
    page.append(el("h1", "", step.name));
    if (step.format_note) { const n = el("div", "muted", step.format_note); n.dataset.role = "format-note"; page.append(n); }
    page.append(ta, row);
  }

  /** One write at a time: the buttons are off while it is out (the controller's calls never reject; they notice their failure). */
  private async guard(a: HTMLButtonElement, b: HTMLButtonElement | null, run: () => Promise<unknown>): Promise<void> {
    if (this.working) return;
    this.working = true;
    a.disabled = true;
    if (b) b.disabled = true;
    try { await run(); } finally {
      this.working = false;
      a.disabled = false;
      if (b) b.disabled = false;
    }
  }
}
