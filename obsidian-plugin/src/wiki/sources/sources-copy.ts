import { tr } from "../../i18n/index";
/**
 * 参数 → 资料: every word of the section (Chinese; app.js `sourcesSection` of the approved full demo) and the folder the
 * originals go to. New text goes here, never as a literal in the controller or the renderer.
 */
import type { SourceKind, SourceProgress } from "../schema";

export const SOURCES_COPY = {
  /** The vault folder (under the notes folder) a file from the computer is saved to, once. */
  folder: "资料",
  get title() { return tr("wiki.sources.sources-copy.SOURCES_COPY.title.2"); },
  get cancel() { return tr("wiki.sources.sources-copy.SOURCES_COPY.cancel.3"); },
  get add() { return tr("wiki.sources.sources-copy.SOURCES_COPY.add.4"); },
  get empty() { return tr("wiki.sources.sources-copy.SOURCES_COPY.empty.5"); },
  get loading() { return tr("wiki.sources.sources-copy.SOURCES_COPY.loading.6"); },
  get open() { return tr("wiki.sources.sources-copy.SOURCES_COPY.open.7"); }, get rename() { return tr("wiki.sources.sources-copy.SOURCES_COPY.rename.8"); }, get remove() { return tr("wiki.sources.sources-copy.SOURCES_COPY.remove.9"); }, get save() { return tr("wiki.sources.sources-copy.SOURCES_COPY.save.10"); },
  get renameLabel() { return tr("wiki.sources.sources-copy.SOURCES_COPY.renameLabel.11"); },
  get confirmDelete() { return tr("wiki.sources.sources-copy.SOURCES_COPY.confirmDelete.12"); },
  get confirmYes() { return tr("wiki.sources.sources-copy.SOURCES_COPY.confirmYes.13"); },
  get retry() { return tr("wiki.sources.sources-copy.SOURCES_COPY.retry.14"); },
  get retryFailed() { return tr("wiki.sources.sources-copy.SOURCES_COPY.retryFailed.15"); },
  /** 用在：<goal titles> (a use whose goal title is not known is left out). */
  get usedIn() { return tr("wiki.sources.sources-copy.SOURCES_COPY.usedIn.16"); },
  get openDemo() { return tr("wiki.sources.sources-copy.SOURCES_COPY.openDemo.17"); },
  get citedGone() { return tr("wiki.sources.sources-copy.SOURCES_COPY.citedGone.18"); },
  /** Status line of a source still being read, until its first progress item. */
  get readingStart() { return tr("wiki.sources.sources-copy.SOURCES_COPY.readingStart.19"); },
  /** 「读好了 · N 节」, or just 「读好了」 when the section count is unknown. */
  ready: (chapters: number | null) => (chapters === null ? tr("wiki.sources.sources-copy.SOURCES_COPY.ready.20") : tr("wiki.sources.sources-copy.SOURCES_COPY.ready.21", { v0: chapters })),
  get failedFallback() { return tr("wiki.sources.sources-copy.SOURCES_COPY.failedFallback.22"); },
  /** A partial source: the pages that could not be read (ranges joined with 、). */
  unreadable: (ranges: string) => tr("wiki.sources.sources-copy.SOURCES_COPY.unreadable.23", { v0: ranges }),
  get partialFallback() { return tr("wiki.sources.sources-copy.SOURCES_COPY.partialFallback.24"); },
  pages: (n: number) => tr("wiki.sources.sources-copy.SOURCES_COPY.pages.25", { v0: n }),
  get streamLost() { return tr("wiki.sources.sources-copy.SOURCES_COPY.streamLost.26"); },
  get readFailedShort() { return tr("wiki.sources.sources-copy.SOURCES_COPY.readFailedShort.27"); },
  // notices
  get renamed() { return tr("wiki.sources.sources-copy.SOURCES_COPY.renamed.28"); },
  deleted: (title: string) => tr("wiki.sources.sources-copy.SOURCES_COPY.deleted.29", { v0: title }),
  openFailed: (title: string) => tr("wiki.sources.sources-copy.SOURCES_COPY.openFailed.30", { v0: title }),
  get blockedDemo() { return tr("wiki.sources.sources-copy.SOURCES_COPY.blockedDemo.31"); },
  saveCopyFailed: (name: string) => tr("wiki.sources.sources-copy.SOURCES_COPY.saveCopyFailed.32", { v0: name }),
  readFailed: (name: string) => tr("wiki.sources.sources-copy.SOURCES_COPY.readFailed.33", { v0: name }),
  unsupported: (name: string) => tr("wiki.sources.sources-copy.SOURCES_COPY.unsupported.34", { v0: name }),
} as const;

/** The kind label on a card (app.js `srcKind`). */
export const KIND_LABEL: Record<SourceKind, string> = {
  pdf: "PDF", pptx: "PPTX", docx: "DOCX", xlsx: "XLSX", md: "MD", text: "TXT", get image() { return tr("wiki.sources.sources-copy.KIND_LABEL.image.35"); }, get url() { return tr("wiki.sources.sources-copy.KIND_LABEL.url.36"); },
};

/** File extensions the backend reads (sources/parse.py SUPPORTED) and the kind each one is. */
export const EXT_KIND: Record<string, SourceKind> = {
  pdf: "pdf", pptx: "pptx", docx: "docx", xlsx: "xlsx", md: "md", markdown: "md", txt: "text",
  png: "image", jpg: "image", jpeg: "image", webp: "image",
};
/** The extension a downloaded original gets when its title has none. */
export const KIND_EXT: Record<SourceKind, string> = {
  pdf: "pdf", pptx: "pptx", docx: "docx", xlsx: "xlsx", md: "md", text: "txt", image: "png", url: "txt",
};

export const extOf = (name: string): string => {
  const m = /\.([A-Za-z0-9]+)$/.exec(name);
  return m ? m[1]!.toLowerCase() : "";
};
/** True for a file name the backend can read. */
export const isSupportedName = (name: string): boolean => EXT_KIND[extOf(name)] !== undefined;

/** 「第 5–14 页」: the sorted unreadable pages as ranges (a single page: 「第 5 页」). */
export function pageRanges(pages: readonly number[]): string {
  const ps = [...new Set(pages)].sort((a, b) => a - b);
  const parts: string[] = [];
  for (let i = 0; i < ps.length;) {
    let j = i;
    while (j + 1 < ps.length && ps[j + 1] === ps[j]! + 1) j++;
    parts.push(j === i ? `${ps[i]}` : `${ps[i]}–${ps[j]}`);
    i = j + 1;
  }
  return parts.length === 0 ? "" : tr("wiki.sources.sources-copy..37", { v0: parts.join("、") });
}

export const sourceProgressText = (progress: SourceProgress): string => {
  const label = tr(`source.progress.${progress.stage}`);
  return progress.total > 0 ? `${label} · ${progress.done}/${progress.total}` : label;
};
