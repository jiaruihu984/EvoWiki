/**
 * 学点新的 · 上传附件: the attached material's state and its 目录 rows (pure logic; learn-view.ts draws them). The plugin
 * never guesses how the material is used: 开始 sends only the material's id with the goal to the 助手 and the server decides.
 */
import type { SourceDetail, SourceProgress, SourceStatus } from "../schema";

/** A material on the goal card. `title` is the file's name as picked (「讲义.pdf」), not the server's title. */
export interface Attached { id: string; title: string; pages: number | null; status: SourceStatus; progress: SourceProgress | null }

/** 开始 waits for every attached material to be read (ready / partial). */
export function canStart(a: readonly Attached[]): boolean {
  return a.every((x) => x.status === "ready" || x.status === "partial");
}

/** One row of the material's 目录: its title, level and the PDF page it starts on. */
export interface TocRow { id: string; title: string; depth: number; page: number | null }

/** The deepest level ← → can reach (source_schemas.py MAX_SECTION_DEPTH). */
export const MAX_TOC_DEPTH = 2;

export function tocOf(d: SourceDetail): TocRow[] {
  return d.sections.map((s) => ({ id: s.id, title: s.title, depth: s.depth, page: s.page_from }));
}

/** ← (−1) / → (+1) is usable while the new level stays within 0..MAX_TOC_DEPTH. */
export function canShift(r: TocRow, d: -1 | 1): boolean {
  const n = r.depth + d;
  return n >= 0 && n <= MAX_TOC_DEPTH;
}

/** The level after ← / →, kept within 0..MAX_TOC_DEPTH. */
export function shiftDepth(r: TocRow, d: -1 | 1): number {
  return Math.min(MAX_TOC_DEPTH, Math.max(0, r.depth + d));
}
