import type { SyncResult, WikiNoteInput } from "./note";
import type { HandEditPlan } from "./note/hand-edit";

export type { SyncResult, WikiNoteInput, WikiNoteSection } from "./note";
export type { HandEditPlan } from "./note/hand-edit";

/** What the controller needs from the vault side; wiring.ts binds it to E3a's syncWikiNote and findWikiNote. */
export interface NoteSync {
  /**
   * Writes / updates the page's note. May throw: then nothing was written. `opts.partial`: written while a stream runs
   * (the page as written so far); a full sync of the landed page follows. The vault side writes it the same way.
   * `opts.accepted`: the exact note text whose hand edit the server took, so it may be written over.
   */
  run(input: WikiNoteInput, opts?: { partial?: boolean; accepted?: string; valid?: () => boolean }): Promise<SyncResult>;
  /** Path of the one note in the wiki folder that claims pageId, else null. Read-only, local. */
  find(pageId: string): Promise<string | null>;
  /** Page ids of the notes in the wiki folder (read-only, local): the pages whose files the reconcile pass looks at. */
  listPages?(): Promise<string[]>;
  /** Page ids of the notes whose frontmatter says `evowiki-status: writing` (read-only, local). */
  listWriting?(): Promise<string[]>;
  /**
   * Read-only, local: the one note claiming this page, with what the user changed in its block; null when no single
   * note claims it.
   */
  handEdit(input: WikiNoteInput): Promise<{ path: string; text: string; plan: HandEditPlan } | null>;
  /** Read-only, local: `path` is the one note claiming `pageId`, and the user changed its block. */
  edited(path: string, pageId: string): Promise<boolean>;
  /** Whether the user is in this note now: a hand edit goes up when he leaves it. */
  isOpen(path: string): boolean;
}
