import type { PageSourceOut } from "../schema";

export interface WikiNoteSection {
  id: string; heading: string;
  role: "organizer" | "line" | "recap" | "explain" | "compare" | "check"; cue: string; body: string; folded: boolean;
  /** SectionOut.note_body: a hand edit, shown exactly as the user typed it. */
  noteBody?: string | null;
  /** SectionOut.artifact_id: the text version written into the note (kept in the written copy for note-edit feedback). */
  artifactId?: string | null;
  /** SectionOut.fingerprint: kept in the written copy; a hand edit sends it back so the server refuses it if the section changed since. */
  fingerprint?: string;
}
export interface WikiNoteInput {
  pageId: string; title: string; goal: string;
  /** 「自你上次来…」, composed by E3b. */
  diffText: string | null;
  /** 「这页有 N 处内容可能不够…」, composed by E3b. */
  collapsed: boolean; sections: WikiNoteSection[];
  /** The materials the page was written from: what its 〔名 p.N〕 marks point at (PageOut.sources). None: marks stay text. */
  sources?: PageSourceOut[];
  /** Where the file lives (Task 13): PageOut.folder ("" = the Wiki root). Absent = the root. */
  folder?: string;
  /** The graph parent's name, added to the file name when the title is taken in the folder: 「定义（食品添加剂）」. */
  parentName?: string | null;
  /** The page is in the trash: its file goes to Obsidian's trash (an edited note is left alone). */
  trashed?: boolean;
  /** Only put the file where the page belongs (rename / move / trash); its text is not touched (the reconcile pass). */
  placeOnly?: boolean;
  /** Streaming: frontmatter `evowiki-status: writing` until the full sync. */
  writing?: boolean;
}
