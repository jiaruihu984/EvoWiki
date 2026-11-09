import { tr } from "../../i18n/index";
/** The 知识图谱 page's text (graph.html, verbatim where it has the string; plain Chinese, never server text). */
export const GRAPH_COPY = {
  get canvasLabel() { return tr("wiki.graph-page.copy.GRAPH_COPY.canvasLabel.1"); },
  zoomIn: "＋", get zoomInLabel() { return tr("wiki.graph-page.copy.GRAPH_COPY.zoomInLabel.2"); },
  zoomOut: "－", get zoomOutLabel() { return tr("wiki.graph-page.copy.GRAPH_COPY.zoomOutLabel.3"); },
  get legendKnown() { return tr("wiki.graph-page.copy.GRAPH_COPY.legendKnown.4"); }, get legendShaky() { return tr("wiki.graph-page.copy.GRAPH_COPY.legendShaky.5"); }, get legendGap() { return tr("wiki.graph-page.copy.GRAPH_COPY.legendGap.6"); },
  get goalsTitle() { return tr("wiki.graph-page.copy.GRAPH_COPY.goalsTitle.7"); },
  get pathTitle() { return tr("wiki.graph-page.copy.GRAPH_COPY.pathTitle.8"); },
  /** B5: a goal parked for the Bot (no 继续 card, no weekly review) and the way back. */
  get parked() { return tr("wiki.graph-page.copy.GRAPH_COPY.parked.9"); }, get parkedTip() { return tr("wiki.graph-page.copy.GRAPH_COPY.parkedTip.10"); }, get resume() { return tr("wiki.graph-page.copy.GRAPH_COPY.resume.11"); }, get resumeFailed() { return tr("wiki.graph-page.copy.GRAPH_COPY.resumeFailed.12"); },
  /** The node card's 前置 list heading (graph.html nodeCard `.prh`). */
  get prereqHead() { return tr("wiki.graph-page.copy.GRAPH_COPY.prereqHead.13"); },
  tipName: (name: string, word: string) => name + (word ? "　" + word : ""),
  tipPrereq: (name: string, phrase: string) => (phrase ? `${name} → ${phrase}` : name),
  tipRelated: (names: string[]) => tr("wiki.graph-page.copy.GRAPH_COPY.tipRelated.14") + names.join("、"),
  get want() { return tr("wiki.graph-page.copy.GRAPH_COPY.want.15"); },
  get mine() { return tr("wiki.graph-page.copy.GRAPH_COPY.mine.16"); },
  /** 真题: a row whose node is examined often (PathItemOut.exam ≥ 3) gets the tag and a ? 「有 N 道往年题考到这里」. */
  get examTag() { return tr("wiki.graph-page.copy.GRAPH_COPY.examTag.17"); }, examTip: (n: number) => tr("wiki.graph-page.copy.GRAPH_COPY.examTip.18", { v0: n }),
  get skippedLine() { return tr("wiki.graph-page.copy.GRAPH_COPY.skippedLine.19"); },
  grip: "⋮⋮",
  moreLabel: (title: string) => tr("wiki.graph-page.copy.GRAPH_COPY.moreLabel.20", { v0: title }),
  get menuRename() { return tr("wiki.graph-page.copy.GRAPH_COPY.menuRename.21"); }, get menuSplit() { return tr("wiki.graph-page.copy.GRAPH_COPY.menuSplit.22"); }, get menuDelete() { return tr("wiki.graph-page.copy.GRAPH_COPY.menuDelete.23"); },
  renamed: (title: string) => tr("wiki.graph-page.copy.GRAPH_COPY.renamed.24", { v0: title }),
  get renamePlaceholder() { return tr("wiki.graph-page.copy.GRAPH_COPY.renamePlaceholder.25"); }, get save() { return tr("wiki.graph-page.copy.GRAPH_COPY.save.26"); }, get cancel() { return tr("wiki.graph-page.copy.GRAPH_COPY.cancel.27"); },
  get undo() { return tr("wiki.graph-page.copy.GRAPH_COPY.undo.28"); },
  // a path row's live status (graph.html wText; the planning seconds are counted by the page itself, spec §4)
  get rowQueued() { return tr("wiki.graph-page.copy.GRAPH_COPY.rowQueued.29"); },
  rowPlanning: (sec: number) => (sec >= 1 ? tr("wiki.graph-page.copy.GRAPH_COPY.rowPlanning.30", { v0: sec }) : tr("wiki.graph-page.copy.GRAPH_COPY.rowPlanning.31")),
  get rowFailed() { return tr("wiki.graph-page.copy.GRAPH_COPY.rowFailed.32"); },
  get rowRetry() { return tr("wiki.graph-page.copy.GRAPH_COPY.rowRetry.33"); },
  get rewrite() { return tr("wiki.graph-page.copy.GRAPH_COPY.rewrite.34"); },
  get rowSplitting() { return tr("wiki.graph-page.copy.GRAPH_COPY.rowSplitting.35"); },
  // notices (graph.html)
  removed: (title: string) => tr("wiki.graph-page.copy.GRAPH_COPY.removed.36", { v0: title }),
  rowSplit: (title: string, n: number) => tr("wiki.graph-page.copy.GRAPH_COPY.rowSplit.37", { v0: title, v1: n }),
  pageLabel: (i: number, title: string) => tr("wiki.graph-page.copy.GRAPH_COPY.pageLabel.38", { v0: i, v1: title }),
  /** Materials: the line under the panel (graph.html COVER) and a deleted material's name there. */
  get uncoveredLead() { return tr("wiki.graph-page.copy.GRAPH_COPY.uncoveredLead.39"); }, deletedTitle: (title: string) => tr("wiki.graph-page.copy.GRAPH_COPY.deletedTitle.40", { v0: title }),
  get addPlaceholder() { return tr("wiki.graph-page.copy.GRAPH_COPY.addPlaceholder.41"); },
  get add() { return tr("wiki.graph-page.copy.GRAPH_COPY.add.42"); },
  // not in graph.html (the demo never fails): plain sentences, never the server's text
  get actionFailed() { return tr("wiki.graph-page.copy.GRAPH_COPY.actionFailed.43"); },
  get listLabel() { return tr("wiki.graph-page.copy.GRAPH_COPY.listLabel.44"); },
  listItem: (name: string, word: string) => (word ? `${name}，${word}` : name),
} as const;
