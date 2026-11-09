import { tr } from "../../i18n/index";
import { BAND_WORD } from "../format";
import type { Band } from "../schema";

/** User-visible text of the star maps (plain Chinese; never server text). */
export const STAR_COPY = {
  // 知识图谱 (the big map tab = app.js renderMap; strings verbatim from app.js)
  get title() { return tr("wiki.starmap.copy.STAR_COPY.title.1"); },
  get demoSuffix() { return tr("wiki.starmap.copy.STAR_COPY.demoSuffix.2"); },
  get ribbon() { return tr("wiki.starmap.copy.STAR_COPY.ribbon.3"); },
  get canvasLabel() { return tr("wiki.starmap.copy.STAR_COPY.canvasLabel.4"); },
  get legendKnown() { return tr("wiki.starmap.copy.STAR_COPY.legendKnown.5"); }, get legendShaky() { return tr("wiki.starmap.copy.STAR_COPY.legendShaky.6"); }, get legendGap() { return tr("wiki.starmap.copy.STAR_COPY.legendGap.7"); },
  /** app.js: the region tip word by the average SCORE of its concepts' bands (> 0.65 / > 0.3). */
  get regionKnown() { return tr("wiki.starmap.copy.STAR_COPY.regionKnown.8"); }, get regionShaky() { return tr("wiki.starmap.copy.STAR_COPY.regionShaky.9"); }, get regionGap() { return tr("wiki.starmap.copy.STAR_COPY.regionGap.10"); },
  get loading() { return tr("wiki.starmap.copy.STAR_COPY.loading.11"); },
  get retry() { return tr("wiki.starmap.copy.STAR_COPY.retry.12"); },
  get staleNote() { return tr("wiki.starmap.copy.STAR_COPY.staleNote.13"); },
  get empty() { return tr("wiki.starmap.copy.STAR_COPY.empty.14"); },
  get emptyAction() { return tr("wiki.starmap.copy.STAR_COPY.emptyAction.15"); },

  // 键盘 / 读屏的替代路径 (看不见的概念列表)
  get listLabel() { return tr("wiki.starmap.copy.STAR_COPY.listLabel.16"); },
  listItem: (name: string, band: Band) => `${name}，${BAND_WORD[band]}`,

  // 概念卡 (app.js renderMap card)
  cardLabel: (name: string) => tr("wiki.starmap.copy.STAR_COPY.cardLabel.17", { v0: name }),
  cardClose: "×",
  cardNow: (band: Band) => tr("wiki.starmap.copy.STAR_COPY.cardNow.18", { v0: BAND_WORD[band] }),
  get cardPrereqs() { return tr("wiki.starmap.copy.STAR_COPY.cardPrereqs.19"); }, get cardDependents() { return tr("wiki.starmap.copy.STAR_COPY.cardDependents.20"); }, get cardNone() { return tr("wiki.starmap.copy.STAR_COPY.cardNone.21"); },
  get cardOpenPage() { return tr("wiki.starmap.copy.STAR_COPY.cardOpenPage.22"); },
  get cardNoPage() { return tr("wiki.starmap.copy.STAR_COPY.cardNoPage.23"); },
  get cardKnow() { return tr("wiki.starmap.copy.STAR_COPY.cardKnow.24"); }, get cardDont() { return tr("wiki.starmap.copy.STAR_COPY.cardDont.25"); },
  declared: (name: string, v: "know" | "dont") => (v === "know" ? tr("wiki.starmap.copy.STAR_COPY.declared.26", { v0: name }) : tr("wiki.starmap.copy.STAR_COPY.declared.27", { v0: name })),

  // 今天里的小星图
  get miniTitle() { return tr("wiki.starmap.copy.STAR_COPY.miniTitle.28"); },
  get miniOpen() { return tr("wiki.starmap.copy.STAR_COPY.miniOpen.29"); },
  get miniAria() { return tr("wiki.starmap.copy.STAR_COPY.miniAria.30"); },
  get miniLoading() { return tr("wiki.starmap.copy.STAR_COPY.miniLoading.31"); },
  get miniError() { return tr("wiki.starmap.copy.STAR_COPY.miniError.32"); },
  get miniEmpty() { return tr("wiki.starmap.copy.STAR_COPY.miniEmpty.33"); },
} as const;
