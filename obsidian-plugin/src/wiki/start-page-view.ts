/**
 * A page not written yet, opened from a link or a path row (app.js renderWritten, a queued page): a main-area tab with its
 * title, 目标 and 「开始生成」. Opening it writes nothing; the button starts the write (WikiController.startPage), whose
 * note then opens in this tab. `div.ew` > `div.ew-scroll` > `article.ew-note`: h1, `dl.ew-meta`, `div.ew-row` with the
 * button. The page (id, title, 目标) is kept in the leaf's state.
 */
import { ItemView, type ViewStateResult, type WorkspaceLeaf } from "obsidian";
import { btn, el } from "../ui/ew";
import { WIKI_COPY } from "./copy";

export const START_PAGE_VIEW_TYPE = "evowiki-start-page";

/** `pageId` "": a path row with no page yet (`pathId`, `key`): 开始生成 makes it. */
export interface StartPage { pageId: string; title: string; goal: string; pathId?: string; key?: string }

export interface StartPageHost {
  /** 开始生成: the write of this page starts (its note opens as it is written). */
  start(page: StartPage): void;
}

export class StartPageView extends ItemView {
  private page: StartPage = { pageId: "", title: "", goal: "" };
  /** Navigable: the note that 开始生成 opens replaces this tab (Obsidian's getLeaf(false) reuses it). */
  override navigation = true;

  constructor(leaf: WorkspaceLeaf, private readonly host: StartPageHost) { super(leaf); }

  getViewType(): string { return START_PAGE_VIEW_TYPE; }
  getDisplayText(): string { return this.page.title || WIKI_COPY.unnamedPage; }
  override getIcon(): string { return "file-plus"; }
  get pageId(): string { return this.page.pageId; }
  get rowKey(): string | undefined { return this.page.key; }

  override getState(): Record<string, unknown> { return { ...this.page }; }
  override async setState(state: unknown, result: ViewStateResult): Promise<void> {
    const s = (state ?? {}) as Partial<Record<keyof StartPage, unknown>>;
    const str = (x: unknown): string => (typeof x === "string" ? x : "");
    this.page = { pageId: str(s.pageId), title: str(s.title), goal: str(s.goal),
      ...(typeof s.pathId === "string" && typeof s.key === "string" ? { pathId: s.pathId, key: s.key } : {}) };
    await super.setState(state, result);
    // the tab's and the view header's title are the page's (both were drawn before the state arrived)
    (this.leaf as WorkspaceLeaf & { updateHeader?: () => void }).updateHeader?.();
    (this as unknown as { titleEl?: HTMLElement }).titleEl?.setText(this.getDisplayText());
    this.draw();
  }

  override onOpen(): Promise<void> { this.draw(); return Promise.resolve(); }
  override onClose(): Promise<void> { this.contentEl.replaceChildren(); return Promise.resolve(); }

  private draw(): void {
    const meta = el("dl", "meta");
    meta.append(el("dt", null, () => (WIKI_COPY.pageGoal)), el("dd", null, this.page.goal));
    const row = el("div", "row start");
    row.append(btn(() => (WIKI_COPY.startPage), "primary", () => { if (this.page.pageId !== "" || this.page.key !== undefined) this.host.start(this.page); }));
    const note = el("article", "note");
    note.append(el("h1", null, () => (this.page.title || WIKI_COPY.unnamedPage)), meta, row);
    const scroll = el("div", "scroll");
    scroll.append(note);
    const root = document.createElement("div");
    root.className = "ew";
    root.append(scroll);
    this.contentEl.replaceChildren(root);
  }
}
