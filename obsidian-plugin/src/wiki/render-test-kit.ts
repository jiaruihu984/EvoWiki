/** Test-only helpers for the render-*.test.ts files (no production code imports this). */
import { vi } from "vitest";
import type { RenderCtx, WikiActions } from "./actions";
import { NameBook, type Names } from "./names";

export const fakeActions = () => ({
  setTab: vi.fn(), retryConnection: vi.fn(),
  setLearnTopic: vi.fn(), openLearnNew: vi.fn(), openWikiPage: vi.fn(), openPath: vi.fn(), pathNext: vi.fn(),
  pathDone: vi.fn(), revealCue: vi.fn(), gradeReview: vi.fn(), answerReview: vi.fn(), nextReview: vi.fn(), startReview: vi.fn(), pauseReview: vi.fn(), markBar: vi.fn(), refreshToday: vi.fn(), askAboutCard: vi.fn(),
  followup: vi.fn(), answerConfirm: vi.fn(), reteach: vi.fn(), confirmUnclear: vi.fn(), setReviewPerDay: vi.fn(),
  reloadReview: vi.fn(), reloadCatalog: vi.fn(), organize: vi.fn(), stop: vi.fn(),
  openCurrentNote: vi.fn(), know: vi.fn(), toggleFold: vi.fn(), togglePin: vi.fn(),
  answerCheck: vi.fn(), done: vi.fn(), expand: vi.fn(), retry: vi.fn(),
  jumpToSection: vi.fn(),
  setRsiSwitch: vi.fn(), reloadRsi: vi.fn(), undoPatch: vi.fn(), approvePatch: vi.fn(), discardPatch: vi.fn(),selectCell: vi.fn(), declareCell: vi.fn(),
  sourcesAdd: vi.fn(), loadEngine: vi.fn(), openSettings: vi.fn(), openPrompt: vi.fn(), openProfile: vi.fn(), moreReview: vi.fn(),
  continueLearning: vi.fn(), answerReviewFirst: vi.fn(), botStart: vi.fn(), botNotInterested: vi.fn(), botGoConfirm: vi.fn(), botDecide: vi.fn(), openRevise: vi.fn(), goToChange: vi.fn(), botToggleWhy: vi.fn(),
  botSaveSettings: vi.fn(), botPause: vi.fn(), botChip: vi.fn(), closeCelebrate: vi.fn(),
  sourceStartRename: vi.fn(), sourceSetRenameDraft: vi.fn(),
  sourceCommitRename: vi.fn(), sourceCancelRename: vi.fn(), sourceAskDelete: vi.fn(), sourceCancelDelete: vi.fn(), sourceConfirmDelete: vi.fn(),
  sourceOpen: vi.fn(), sourceRetryFollow: vi.fn(), sourceRetryImport: vi.fn(), sourcesReload: vi.fn(),
  personalView: vi.fn(), prefDraft: vi.fn(), prefAdopt: vi.fn(), prefReject: vi.fn(), prefSave: vi.fn(), prefRemove: vi.fn(), prefRestore: vi.fn(), resolveConflicts: vi.fn(), reloadConflicts: vi.fn(),
}) satisfies WikiActions;

export interface MdCall { el: HTMLElement; md: string }
/** renderMarkdown writes the Markdown as plain text and records every call. */
export function fakeCtx(names: Names = new NameBook()): RenderCtx & { md: MdCall[] } {
  const md: MdCall[] = [];
  return { md, names, renderMarkdown: (el: HTMLElement, text: string) => { md.push({ el, md: text }); el.textContent = text; } };
}

export const btn = (root: ParentNode, sel: string): HTMLButtonElement => {
  const b = root.querySelector(sel);
  if (!(b instanceof HTMLButtonElement)) throw new Error(`no button ${sel}`);
  return b;
};
export const btns = (root: ParentNode, sel: string): HTMLButtonElement[] => [...root.querySelectorAll<HTMLButtonElement>(sel)];
/** xs[i], failing the test when it is missing. */
export function at<T>(xs: readonly T[], i: number): T {
  const x = xs[i];
  if (x === undefined) throw new Error(`no item ${i}`);
  return x;
}
