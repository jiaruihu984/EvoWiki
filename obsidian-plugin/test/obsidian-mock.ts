// Minimal stand-in for the `obsidian` module in Vitest. Only what src/ imports at module load.
import { StateField } from "@codemirror/state";
import { vi } from "vitest";

export class Plugin {
  /** Ribbon icons added so far (icon, title). */
  ribbon: { icon: string; title: string; cb: () => void }[] = [];
  addRibbonIcon(icon: string, title: string, cb: () => void): HTMLElement { this.ribbon.push({ icon, title, cb }); return document.createElement("div"); }
  private readonly cleanups: (() => void)[] = [];
  register(cb: () => void): void { this.cleanups.push(cb); }
  /** Intervals registered so far; unload() clears them like the real Plugin. */
  registerInterval(id: number): number { this.cleanups.push(() => window.clearInterval(id)); return id; }
  registerDomEvent(el: Window | HTMLElement | Document, type: string, cb: (e: Event) => void): void {
    el.addEventListener(type, cb);
    this.cleanups.push(() => el.removeEventListener(type, cb));
  }
  /** Runs the registered cleanups, then onunload (the real Component.unload order is not needed here). */
  unload(): void {
    for (const c of this.cleanups.splice(0)) c();
    (this as unknown as { onunload?: () => void }).onunload?.();
  }
}
export class Component {
  /** Cleanups registered on this component; unload() runs them (MarkdownRenderChild in check-block wiring). */
  private readonly cleanups: (() => void)[] = [];
  register(cb: () => void): void { this.cleanups.push(cb); }
  load(): void { /* nothing to load in tests */ }
  unload(): void { for (const cb of this.cleanups.splice(0)) cb(); }
}
export class MarkdownRenderChild extends Component { constructor(public containerEl: HTMLElement) { super(); } }
export class TFile { path = ""; basename = ""; extension = "md"; }
export class MarkdownView { file: TFile | null = null; }
/** Obsidian's menu: records the items added and where it was shown (section-menu-view.ts). */
/** One entry of a Menu (the tests read these); `submenu` is set when the item opened one. */
export interface MockMenuItem { title: string; disabled: boolean; click: (() => void) | null; submenu: Menu | null }
/** `submenus: false` makes the next Menus lack setSubmenu (older Obsidian), like the real typings. */
export const menuSupport = { submenus: true };
export class Menu {
  readonly items: MockMenuItem[] = [];
  /** The item counts before each separator (so tests see the grouping). */
  readonly separatorsAt: number[] = [];
  shownAt: { x: number; y: number } | null = null;
  private hide: (() => void) | null = null;
  addItem(cb: (i: { setTitle(t: string): unknown; setDisabled(d: boolean): unknown; onClick(f: () => void): unknown;
    setSubmenu?(): Menu }) => void): this {
    const it: MockMenuItem = { title: "", disabled: false, click: null, submenu: null };
    const api: { setTitle(t: string): typeof api; setDisabled(d: boolean): typeof api; onClick(f: () => void): typeof api; setSubmenu?(): Menu } = {
      setTitle: (t) => { it.title = t; return api; }, setDisabled: (d) => { it.disabled = d; return api; },
      onClick: (f) => { it.click = f; return api; } };
    if (menuSupport.submenus) api.setSubmenu = () => { it.submenu = new Menu(); return it.submenu; };
    cb(api);
    this.items.push(it);
    return this;
  }
  addSeparator(): this { this.separatorsAt.push(this.items.length); return this; }
  onHide(f: () => void): void { this.hide = f; }
  showAtPosition(pos: { x: number; y: number }): this { this.shownAt = pos; Menu.last = this; return this; }
  close(): void { this.hide?.(); }
  static last: Menu | null = null;
}
/** A stand-in for an icon: an element recording which icon was asked for. */
export function setIcon(parent: HTMLElement, iconId: string): void {
  const el = document.createElement("svg");
  el.className = "svg-icon";
  el.setAttribute("data-icon", iconId);
  parent.replaceChildren(el);
}
/** Obsidian's Live Preview flag as a CodeMirror state field (section-menu-view.ts): on wherever a test includes it. */
export const editorLivePreviewField = StateField.define<boolean>({ create: () => true, update: (v) => v });
export const editorInfoField = StateField.define<{ file: { path: string } | null }>({ create: () => ({ file: null }), update: (v) => v });
/** Every Notice message shown, in order (tests clear it). */
export const notices: string[] = [];
export class Notice {
  static last: Notice | null = null;
  hidden = false;
  /** A fragment (text + a button) is kept as is; `message` is its text. */
  constructor(public content: string | DocumentFragment, public duration?: number) {
    Notice.last = this;
    notices.push(typeof content === "string" ? content : (content.textContent ?? ""));
  }
  get message(): string { return typeof this.content === "string" ? this.content : (this.content.textContent ?? ""); }
  hide(): void { this.hidden = true; }
}
export class ItemView {
  contentEl = document.createElement("div");
  constructor(public leaf: unknown) {}
  setState(_state: unknown, _result: unknown): Promise<void> { return Promise.resolve(); }
}
/**
 * Obsidian's Modal DOM as the real constructor builds it: containerEl.modal-container > .modal-bg + modalEl.modal >
 * .modal-close-button + titleEl.modal-title + contentEl.modal-content. open() / close() stay no-ops (tests drive onOpen).
 */
export class Modal {
  containerEl = document.createElement("div");
  modalEl = document.createElement("div");
  titleEl = document.createElement("div");
  contentEl = document.createElement("div");
  constructor(public app: unknown) {
    this.containerEl.className = "modal-container";
    const bg = document.createElement("div");
    bg.className = "modal-bg";
    this.modalEl.className = "modal";
    const x = document.createElement("div");
    x.className = "modal-close-button";
    this.titleEl.className = "modal-title";
    this.contentEl.className = "modal-content";
    this.modalEl.append(x, this.titleEl, this.contentEl);
    this.containerEl.append(bg, this.modalEl);
  }
  open(): void {}
  close(): void {}
}
export class SuggestModal<T> extends Modal {
  placeholder = "";
  setPlaceholder(p: string): void { this.placeholder = p; }
  // Subclasses implement these; declared so TypeScript accepts `override`.
  getSuggestions(_q: string): T[] { return []; }
  renderSuggestion(_v: T, _el: HTMLElement): void {}
  onChooseSuggestion(_v: T, _evt: MouseEvent | KeyboardEvent): void {}
}
export class FuzzySuggestModal<T> extends SuggestModal<T> {
  getItems(): T[] { return []; }
  getItemText(_i: T): string { return ""; }
  onChooseItem(_i: T, _evt: MouseEvent | KeyboardEvent): void {}
}
export class PluginSettingTab { containerEl = document.createElement("div"); constructor(public app: unknown, public plugin: unknown) {} hide(): void {} }
/**
 * Obsidian's Setting row: a .setting-item with name, description and control parts (like the real one), so tests can read
 * the text and drive the controls. Text → <input>, dropdown → <select>, toggle → <input type=checkbox>; each component
 * fires its onChange on the element's "change" event.
 */
export class Setting {
  readonly settingEl = document.createElement("div");
  readonly nameEl = document.createElement("div");
  readonly descEl = document.createElement("div");
  readonly controlEl = document.createElement("div");
  constructor(el: HTMLElement) {
    this.settingEl.className = "setting-item";
    this.nameEl.className = "setting-item-name";
    this.descEl.className = "setting-item-description";
    this.controlEl.className = "setting-item-control";
    this.settingEl.append(this.nameEl, this.descEl, this.controlEl);
    el.append(this.settingEl);
  }
  setName(n: string | DocumentFragment): this { this.nameEl.replaceChildren(n); return this; }
  setDesc(d: string | DocumentFragment): this { this.descEl.replaceChildren(d); return this; }
  setHeading(): this { this.settingEl.classList.add("setting-item-heading"); return this; }
  setClass(c: string): this { this.settingEl.classList.add(c); return this; }
  addText(cb?: (t: TextStub) => void): this {
    const inputEl = document.createElement("input");
    inputEl.type = "text";
    this.controlEl.append(inputEl);
    const t: TextStub = {
      inputEl,
      setValue: (v) => { inputEl.value = v; return t; },
      setPlaceholder: (v) => { inputEl.placeholder = v; return t; },
      setDisabled: (d) => { inputEl.disabled = d; return t; },
      onChange: (f) => { inputEl.addEventListener("change", () => { void f(inputEl.value); }); return t; },
    };
    cb?.(t);
    return this;
  }
  addDropdown(cb?: (d: DropdownStub) => void): this {
    const selectEl = document.createElement("select");
    this.controlEl.append(selectEl);
    const d: DropdownStub = {
      selectEl,
      addOption: (value, display) => { const o = document.createElement("option"); o.value = value; o.textContent = display; selectEl.append(o); return d; },
      setValue: (v) => { selectEl.value = v; return d; },
      setDisabled: (x) => { selectEl.disabled = x; return d; },
      onChange: (f) => { selectEl.addEventListener("change", () => { void f(selectEl.value); }); return d; },
    };
    cb?.(d);
    return this;
  }
  addToggle(cb?: (t: ToggleStub) => void): this {
    const toggleEl = document.createElement("input");
    toggleEl.type = "checkbox";
    this.controlEl.append(toggleEl);
    const t: ToggleStub = {
      toggleEl,
      setValue: (v) => { toggleEl.checked = v; return t; },
      setDisabled: (x) => { toggleEl.disabled = x; return t; },
      onChange: (f) => { toggleEl.addEventListener("change", () => { void f(toggleEl.checked); }); return t; },
    };
    cb?.(t);
    return this;
  }
  addButton(cb?: (b: ButtonStub) => void): this {
    const buttonEl = document.createElement("button");
    this.controlEl.append(buttonEl);
    const b: ButtonStub = {
      buttonEl,
      setButtonText: (v) => { buttonEl.textContent = v; return b; },
      setDisabled: (x) => { buttonEl.disabled = x; return b; },
      onClick: (f) => { buttonEl.addEventListener("click", () => { void f(); }); return b; },
    };
    cb?.(b);
    return this;
  }
}
interface TextStub { inputEl: HTMLInputElement; setValue(v: string): TextStub; setPlaceholder(v: string): TextStub;
  setDisabled(d: boolean): TextStub; onChange(f: (v: string) => unknown): TextStub }
interface DropdownStub { selectEl: HTMLSelectElement; addOption(value: string, display: string): DropdownStub; setValue(v: string): DropdownStub;
  setDisabled(d: boolean): DropdownStub; onChange(f: (v: string) => unknown): DropdownStub }
interface ButtonStub { buttonEl: HTMLButtonElement; setButtonText(v: string): ButtonStub; setDisabled(d: boolean): ButtonStub;
  onClick(f: () => unknown): ButtonStub }
interface ToggleStub { toggleEl: HTMLInputElement; setValue(v: boolean): ToggleStub; setDisabled(d: boolean): ToggleStub;
  onChange(f: (v: boolean) => unknown): ToggleStub }
// Obsidian's MathJax (src/wiki/evoui/math.ts): a typeset formula is an <mjx-container> with its source as text
export const loadMathJax = vi.fn(() => Promise.resolve());
export const renderMath = vi.fn((source: string, display: boolean): HTMLElement => {
  const el = document.createElement("mjx-container");
  el.setAttribute("display", String(display));
  el.textContent = `[math] ${source}`;
  if (source.split("{").length !== source.split("}").length) el.append(document.createElement("mjx-merror"));   // MathJax's answer to bad TeX
  return el;
});
export const finishRenderMath = vi.fn(() => Promise.resolve());
export const MarkdownRenderer = { render: vi.fn(() => Promise.resolve(undefined)) };
export const requestUrl = vi.fn();
/** Obsidian 1.8+ getLanguage(): the UI language (tests: English). */
export const getLanguage = (): string => "en";
/** Obsidian's path cleanup, enough for tests: forward slashes, no doubled / leading / trailing slash. */
export const normalizePath = (p: string): string => p.replace(/\\/g, "/").replace(/\/+/g, "/").replace(/^\/|\/$/g, "");
/** moment(): only format() with the YYYY / MM / DD tokens (local time; tests may fake the clock). */
export const moment = (): { format(f: string): string } => {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return { format: (f) => f.replace(/YYYY/g, String(d.getFullYear())).replace(/MM/g, pad(d.getMonth() + 1)).replace(/DD/g, pad(d.getDate())) };
};
