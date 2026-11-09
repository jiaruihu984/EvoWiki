/**
 * The Obsidian side of the reading-assistant panel: which note is active, when it changes, and Markdown rendering.
 * Reads only (the active file's cached text and frontmatter); nothing here sends anything. No app API is touched at
 * construction: listeners exist only between onActiveChange and its returned unsubscribe.
 */
import { type App, type Component, MarkdownRenderer, type TFile } from "obsidian";
import { readConceptId } from "../concept/mapping";
import { isInternalPath } from "../notes/note-format";
import { isSupportedName } from "../wiki/sources/sources-copy";
import { openEvoWikiSettings } from "../wiki/wiring";
import type { ActiveNote } from "./session-store";
import type { Source } from "./types";
import { renderMarkdownSafely } from "./safe-markdown";
import { resolveActiveNote } from "./target";

export interface AssistantHost {
  activeNote(): Promise<ActiveNote>;
  onActiveChange(cb: () => void): () => void;
  /** owner = the view (MarkdownRenderer needs a Component); the view binds it into RenderHooks.renderMarkdown(el, md). */
  renderMarkdown(el: HTMLElement, md: string, owner: Component): Promise<void>;
  conceptName(id: string): string;
  /** A source chip in an answer: a wiki page (page_id) opens in the wiki, an http(s) url in the browser. */
  openSource?(src: Source): void;
  openSettings?(): void;
  /** 「帮我复习」's card in the chat: 今天's review round as it stands now (main.ts). */
  reviewCard?(): HTMLElement;
  reviewLive?(): boolean;
  /** A materials draft card: the page in review mode, at its first mark. */
  openRevise?(pageId: string): void;
  /**
   * A file dragged in from Obsidian's file explorer: the drop's text (an obsidian:// link or a vault path), read through
   * the vault (reads only). A File when it reads; "unreadable" for an obsidian:// link that does not (or not a kind the
   * backend reads); null when the text is no vault reference at all (nothing happens).
   */
  readDropped?(text: string): Promise<File | "unreadable" | null>;
}

/** The vault path a drop's text names: an obsidian://open?...&file=<path> link, else the text's first line. */
export function droppedVaultPath(text: string): { path: string; link: boolean } | null {
  const line = text.split(/\r?\n/).map((l) => l.trim()).find((l) => l !== "");
  if (line === undefined) return null;
  if (/^obsidian:\/\//i.test(line)) {
    try {
      const path = new URL(line).searchParams.get("file");
      return { path: path ?? "", link: true };
    } catch { return { path: "", link: true }; }
  }
  return /^(https?|file):/i.test(line) ? null : { path: line, link: false };
}

export function createObsidianHost(app: App, conceptName: (id: string) => string, openPage?: (pageId: string) => void,
  openRevise?: (pageId: string) => void): AssistantHost {
  return {
    ...(openRevise ? { openRevise } : {}),
    async activeNote() {
      const f = app.workspace.getActiveFile();
      const text = f && f.extension === "md" && !isInternalPath(f.path) ? await app.vault.cachedRead(f) : null;
      const conceptId = f ? readConceptId(app.metadataCache.getFileCache(f)?.frontmatter) : null;
      return resolveActiveNote(f, conceptId, text);
    },
    onActiveChange(cb) {
      const opened = app.workspace.on("file-open", () => cb());
      const leaf = app.workspace.on("active-leaf-change", () => cb());
      const changed = app.metadataCache.on("changed", (file: TFile) => {
        if (file.path === app.workspace.getActiveFile()?.path) cb();
      });
      return () => { app.workspace.offref(opened); app.workspace.offref(leaf); app.metadataCache.offref(changed); };
    },
    // Remote media (images, frames, audio/video) never load: see safe-markdown.ts.
    renderMarkdown(el, md, owner) { return renderMarkdownSafely((m, into) => MarkdownRenderer.render(app, m, into, "", owner), el, md); },
    conceptName,
    async readDropped(text) {
      const ref = droppedVaultPath(text);
      if (ref === null) return null;
      const f = ref.path === "" ? null
        : (app.vault.getAbstractFileByPath(ref.path) ?? app.metadataCache.getFirstLinkpathDest(ref.path, ""));
      if (f === null || !("extension" in f)) return ref.link ? "unreadable" : null;
      if (!isSupportedName(f.name)) return "unreadable";
      try { return new File([await app.vault.readBinary(f as TFile)], f.name); } catch { return "unreadable"; }
    },
    openSettings() { openEvoWikiSettings(app); },
    openSource(src) {
      const pageId = src.page_id ?? "";
      const url = src.url ?? "";
      if (pageId !== "" && openPage) openPage(pageId);
      else if (/^https?:\/\//i.test(url)) window.open(url, "_blank", "noopener");
    },
  };
}
