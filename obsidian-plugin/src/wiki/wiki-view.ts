/**
 * The EvoWiki sidebar ItemView (the demo's `aside.side`). main owns the controller (and disposes it); the view only
 * subscribes. The 助手 tab is the assistant's own panel (the shared AssistantController, Ruling R8.1), mounted once per
 * open view and kept across wiki rebuilds; it follows the assistant only while 助手 is shown. The red counts on 今天 / 参数
 * come from a DotTracker whose seen keys persist in plugin data. The small knowledge cloud of 今天 is made once per
 * open view and destroyed on close.
 */
import { ItemView, type WorkspaceLeaf } from "obsidian";
import type { AccountCheck } from "../account";
import type { RenderCtx, WikiActions } from "./actions";
import type { MiniCloud } from "./cloud/mini-cloud";
import type { WikiController } from "./controller";
import { WIKI_COPY } from "./copy";
import { DotTracker, type SeenStore } from "./dots";
import { WikiRenderer } from "./wiki-renderer";
import { onLanguageChange } from "../i18n";

export const WIKI_VIEW_TYPE = "evowiki-wiki";

/** What the 助手 tab mounts: a persistent node that follows the assistant between open() and close(). */
export interface MountedPanel { readonly root: HTMLElement; open(): void; close(): void }

export class WikiView extends ItemView {
  private unsub: (() => void) | null = null;
  private unsubProgress: (() => void) | null = null;
  private unsubAccount: (() => void) | null = null;
  private unsubLanguage: (() => void) | null = null;
  private panel: MountedPanel | null = null;
  private cloud: MiniCloud | null = null;
  private following = false;
  /** The open page 今天 last re-read for (null: none yet), and whether a re-read waits for the controller to go idle. */
  private seenPage: string | null = null;
  private staleToday = false;
  constructor(leaf: WorkspaceLeaf, private readonly controller: WikiController, private readonly actions: WikiActions,
              private readonly ctx: (view: WikiView) => RenderCtx,
              private readonly assistant: ((view: WikiView) => MountedPanel) | null = null,
              /** Told when this view closes, with its controller (main leaves demo mode when the demo's view closes). */
              private readonly onClosed: ((controller: WikiController) => void) | null = null,
              /** The small knowledge cloud of 今天: built once per open view and kept across redraws. */
              private readonly miniCloud: ((view: WikiView) => MiniCloud) | null = null,
              /** Where the red counts' seen keys persist (null: only for this view's life). */
              private readonly seen: SeenStore | null = null,
              private readonly account: Pick<AccountCheck, "state" | "subscribe"> | null = null) { super(leaf); }
  getViewType(): string { return WIKI_VIEW_TYPE; }
  getDisplayText(): string { return WIKI_COPY.viewTitle; }
  override getIcon(): string { return "book-open"; }
  override onOpen(): Promise<void> {
    this.panel = this.assistant?.(this) ?? null;
    this.cloud = this.miniCloud?.(this) ?? null;
    const dots = new DotTracker(this.seen);
    const r = new WikiRenderer(this.actions, () => this.ctx(this), this.panel?.root ?? null, this.cloud);
    this.contentEl.replaceChildren(r.root);
    const render = (): void => {
      const s = this.controller.state;
      r.update(s, dots.update(s, s.tab), this.account?.state ?? null);
      // The all-done check draws itself the first time only (app.js BOT.allDoneSeen).
      if (!s.bot.allDoneSeen && r.root.querySelector(".ew-alldone") !== null) this.controller.markAllDoneSeen();
      this.follow(s.tab === "assistant" && (this.account === null || this.account.state.kind === "ready"));
      this.refreshOnNewPage();
    };
    const open0 = this.controller.state.page;
    this.seenPage = open0.kind === "open" ? open0.page.page_id : null;
    // The page already open when the view opens gets its Bot chips too (a later page does through refreshOnNewPage).
    if (this.seenPage !== null) void this.controller.loadBotChips(this.seenPage);
    render();
    this.unsub = this.controller.subscribe(render);
    this.unsubLanguage = onLanguageChange(render);
    this.unsubAccount = this.account?.subscribe(render) ?? null;
    // A source's progress item: its card changes in place (no redraw of the panel).
    this.unsubProgress = this.controller.subscribeProgress(() => { r.progress(this.controller.state); });
    void this.controller.init();
    this.controller.viewOpened();
    return Promise.resolve();
  }
  override onClose(): Promise<void> {
    this.unsub?.(); this.unsub = null;
    this.unsubProgress?.(); this.unsubProgress = null;
    this.unsubAccount?.(); this.unsubAccount = null;
    this.unsubLanguage?.(); this.unsubLanguage = null;
    this.follow(false);
    this.panel = null;
    this.cloud?.destroy(); this.cloud = null;
    this.controller.viewClosed();
    this.onClosed?.(this.controller);
    return Promise.resolve();
  }

  /**
   * 待办事项 and 需要你看的 are read once when 今天 opens; a page that lands later (a new goal, 继续, another page opened) can
   * make cards due and ask for a decision, so the lists are re-read (forced) when the open page changes, once idle.
   */
  private refreshOnNewPage(): void {
    const s = this.controller.state;
    const id = s.page.kind === "open" ? s.page.page.page_id : null;
    if (id !== null && id !== this.seenPage) { this.seenPage = id; this.staleToday = true; void this.controller.loadBotChips(id); }
    // A review in progress skips the re-read (the cards must not change), so the wish stays until it ends.
    if (this.staleToday && s.busy === null && s.page.kind === "open" && !s.review.active) { this.staleToday = false; void this.controller.refreshToday(true); }
  }

  /** Opens the panel when 助手 is shown, closes it (detach only: answers keep streaming) when another tab is. */
  private follow(on: boolean): void {
    if (on === this.following || this.panel === null) return;
    this.following = on;
    if (on) this.panel.open(); else this.panel.close();
  }
}
