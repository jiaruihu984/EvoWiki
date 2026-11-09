/**
 * Chat attachments (materials phase 2): the port main.ts injects (upload / follow, over the /wiki/sources client) and the
 * pure state of a draft's attachments and of a sent turn's. The UI never shows how the model read an attachment (user
 * decision: no process text); a card has the file and its state. What to do with a file comes from the chat: the
 * assistant infers it and acts (new_goal / mark_source_use), so a card has no buttons.
 */
import type { WikiApi } from "../wiki/api";
import type { SourceEvent, SourceOut, SourceStatus } from "../wiki/schema";
import { MAX_MESSAGE_CHARS, type AttachmentOut } from "./types";

/** What the progress stream of an uploaded attachment says, narrowed to what the composer needs. */
export type AttachProgress =
  | { kind: "progress"; done: number; total: number }
  /** The source can be used (reading may go on in the background, never shown). */
  | { kind: "usable"; status: SourceStatus; unreadable?: number[] }
  | { kind: "done"; status: SourceStatus; unreadable?: number[] }
  | { kind: "error"; detail: string };

export interface AttachmentPort {
  /** POST /wiki/sources with use "ask" and the session id. A same-content upload answers 200 with the existing source. */
  upload(file: { name: string; data: ArrayBuffer }, sessionId: string): Promise<SourceOut>;
  /** Follows the source's progress until done / error, or until `signal` aborts. */
  follow(id: string, on: (p: AttachProgress) => void, signal: AbortSignal): Promise<void>;
}

/** The port over the /wiki/sources client (main.ts builds it; tests use a fake port). */
export function sourcesPort(api: Pick<WikiApi, "uploadSource" | "followSource">): AttachmentPort {
  return {
    upload: (file, sessionId) => api.uploadSource({ file: { name: file.name, data: file.data }, use: "ask", session_id: sessionId }),
    follow: (id, on, signal) => api.followSource(id, (e: SourceEvent) => { on(progressOf(e)); }, { signal }),
  };
}

function progressOf(e: SourceEvent): AttachProgress {
  switch (e.event) {
    case "progress": return { kind: "progress", done: e.data.done, total: e.data.total };
    case "usable": return { kind: "usable", status: e.data.status, unreadable: e.data.unreadable_pages };
    case "done": return { kind: "done", status: e.data.status, unreadable: e.data.unreadable_pages };
    case "error": return { kind: "error", detail: e.data.detail };
  }
}

export type DraftStatus = "uploading" | "reading" | "ready" | "partial" | "failed";
/**
 * One attachment in the composer. `thumb`: an object URL of an image file (the composer revokes it on removal).
 * `progress`: for the bar only (the card says 正在解析, never the server's counts or message).
 */
export interface DraftAttachment {
  localId: string; name: string; kind: string; bytes: number; pages: number | null;
  sourceId?: string; status: DraftStatus;
  progress?: { done: number; total: number }; error?: string; thumb?: string;
  /** Pages that could not be read (a partial source), for 「部分页读不出 · 第 a–b 页」. */
  unreadable?: number[];
}

/** The kind before the server answers: images by MIME type, else the file extension (the server's kind replaces it). */
export function guessKind(name: string, type: string): string {
  if (type.startsWith("image/")) return "image";
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot + 1).toLowerCase();
}

export function newDraft(localId: string, file: { name: string; type: string; size: number }, thumb?: string): DraftAttachment {
  return { localId, name: file.name, kind: guessKind(file.name, file.type), bytes: file.size, pages: null, status: "uploading",
    ...(thumb !== undefined ? { thumb } : {}) };
}

/** The upload's answer (201 new, or 200 the same content again: treated alike). */
export function applyUploaded(d: DraftAttachment, out: SourceOut): void {
  d.sourceId = out.id; d.name = out.title; d.kind = out.kind; d.pages = out.pages; d.status = out.status;
  if (out.status === "failed") d.error = out.error ?? "";
  if (out.unreadable_pages?.length) d.unreadable = out.unreadable_pages;
  if (out.status === "reading" && out.progress) {
    d.progress = { done: out.progress.done, total: out.progress.total };
  }
}

/** One progress item. Once usable (ready / partial) the card stops showing progress; later items change nothing. */
export function applyProgress(d: DraftAttachment, p: AttachProgress): void {
  if (d.status !== "reading") return;
  switch (p.kind) {
    case "progress": d.progress = { done: p.done, total: p.total }; return;
    case "usable": case "done":
      d.status = p.status === "reading" ? "reading" : p.status;
      if (p.unreadable?.length) d.unreadable = p.unreadable;
      if (d.status !== "reading") delete d.progress;
      return;
    case "error": failDraft(d, p.detail); return;
  }
}

export function failDraft(d: DraftAttachment, error: string): void {
  d.status = "failed"; d.error = error; delete d.progress;
}

/** Still uploading or being read. A failed one says so on its card and does not go with the message. */
export const blocksSend = (d: DraftAttachment): boolean => d.status === "uploading" || d.status === "reading";

/**
 * The send rule (the composer's 问 button and the controller's guard): not while busy, restoring, over length or with an
 * attachment that blocks; then any of text, a quote or an attachment.
 */
export function canSend(c: { busy: boolean; draft: string; quote: unknown; attachments: DraftAttachment[]; restore: string }): boolean {
  if (c.busy || c.restore === "loading" || [...c.draft].length > MAX_MESSAGE_CHARS) return false;
  if (c.attachments.some(blocksSend)) return false;
  return c.draft.trim() !== "" || c.quote !== null || c.attachments.some((d) => d.status !== "failed");
}

/** An attachment on a sent (or restored) user message. */
export interface TurnAttachment {
  sourceId: string; title: string; kind: string; status: string; deleted: boolean; uses: string[];
  bytes: number; pages: number | null; expiresAt: string | null; thumb?: string;
  /** Pages nothing could be read from (「部分页读不出 · 第 a–b 页」). */
  unreadable: number[];
  /** Characters of its text (「约 N 万字」); null when unknown. */
  chars: number | null;
  /** The server said an import run of it had not ended (AttachmentOut.importing) when the card was last updated. */
  importing: boolean;
  /** The full import a use from the chat started (正在解析 + bar, no counts); null when nothing runs. */
  progress: { done: number; total: number } | null;
  /** Why that import failed ("" if none). */
  error: string;
}

/** The source has a use beyond ask (kept: no 「N 天后清掉」; the use itself is never shown). */
export const learning = (uses: readonly string[]): boolean => uses.some((u) => u !== "ask");

export function fromOut(a: AttachmentOut, thumb?: string): TurnAttachment {
  const uses = a.uses ?? [];
  return {
    sourceId: a.source_id, title: a.title, kind: a.kind, status: a.status, deleted: a.deleted, uses,
    bytes: a.bytes ?? 0, pages: a.pages ?? null, expiresAt: a.expires_at ?? null, ...(thumb !== undefined ? { thumb } : {}),
    unreadable: a.unreadable_pages ?? [], chars: a.chars ?? null,
    importing: a.importing ?? false, progress: null, error: "",
  };
}

/** A sent card back in the composer (the server refused the message for an attachment): usable as it was when sent. */
export function backToDraft(localId: string, a: TurnAttachment): DraftAttachment {
  return { localId, name: a.title, kind: a.kind, bytes: a.bytes, pages: a.pages, sourceId: a.sourceId,
    status: a.status === "partial" ? "partial" : "ready", ...(a.thumb !== undefined ? { thumb: a.thumb } : {}) };
}

/** The card a sent draft attachment shows until the turn's done brings the server's record. */
export function fromDraft(d: DraftAttachment): TurnAttachment {
  return fromOut({ source_id: d.sourceId ?? "", title: d.name, kind: d.kind, status: d.status, deleted: false, uses: ["ask"],
    bytes: d.bytes, pages: d.pages, expires_at: null, unreadable_pages: d.unreadable ?? [], chars: null, read: null, importing: false }, d.thumb);
}

/** done's attachments replace the sent cards' data; the thumbnails and a running import's bar / error stay. */
export function mergeOut(prev: TurnAttachment[], outs: AttachmentOut[]): TurnAttachment[] {
  return outs.map((a) => {
    const old = prev.find((p) => p.sourceId === a.source_id);
    const next = fromOut(a, old?.thumb);
    if (old) { next.progress = old.progress; next.error = old.error; }
    return next;
  });
}

/** Days until `expiresAt` (rounded up), for 「N 天后清掉」; null when there is no date or it is more than `within` days away. */
export function expiresInDays(expiresAt: string | null, now: number, within = 7): number | null {
  if (expiresAt === null) return null;
  const t = Date.parse(expiresAt);
  if (Number.isNaN(t)) return null;
  const days = Math.max(0, Math.ceil((t - now) / 86_400_000));
  return days <= within ? days : null;
}
