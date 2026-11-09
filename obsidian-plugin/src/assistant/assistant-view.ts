/**
 * The old standalone EvoWiki 陪读助手 panel. Still registered (a saved workspace may restore it), but nothing opens it
 * any more: the command and 问陪读助手 open the wiki sidebar's 助手 tab. It mounts the same AssistantPanel on the same
 * controller (Ruling R8.1), so both show one conversation. Closing it only detaches: a running answer keeps streaming.
 */
import { ItemView, type WorkspaceLeaf } from "obsidian";
import { AssistantPanel } from "./assistant-panel";
import type { AssistantController } from "./controller";
import { ASSISTANT_COPY } from "./copy";
import type { AssistantHost } from "./host";

export const ASSISTANT_VIEW_TYPE = "evowiki-assistant";

export class AssistantView extends ItemView {
  private panel: AssistantPanel | null = null;

  constructor(leaf: WorkspaceLeaf, private readonly controller: AssistantController, private readonly host: AssistantHost) {
    super(leaf);
  }

  getViewType(): "evowiki-assistant" { return ASSISTANT_VIEW_TYPE; }
  getDisplayText(): string { return ASSISTANT_COPY.viewTitle; }
  override getIcon(): "messages-square" { return "messages-square"; }

  override onOpen(): Promise<void> {
    const panel = new AssistantPanel(this.controller, this.host, this);
    this.panel = panel;
    // The demo's CSS applies inside an `.ew` wrapper (styles.css EXACT block).
    const wrap = this.contentEl.ownerDocument.createElement("div");
    wrap.className = "ew ew-asst-host";
    wrap.append(panel.root);
    this.contentEl.replaceChildren(wrap);
    panel.open();
    return Promise.resolve();
  }

  override onClose(): Promise<void> {
    this.panel?.close();
    this.panel = null;
    return Promise.resolve();
  }
}
