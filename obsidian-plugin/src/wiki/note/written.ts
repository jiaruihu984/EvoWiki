/**
 * The written copy (Q8, revised): before the plugin creates or rewrites a page's note, it keeps the generated body it
 * is about to write under .evowiki/written/<pk>/<hash>.json (pk = the first 32 hex characters of sha256(page id): a
 * page id may hold "/" or ":"; hash = the hex of the note's evowiki_hash). The note names its copy by the hash it
 * stores, so the copy is exact whatever the server did later, and a file is never overwritten (content-addressed:
 * an existing file already holds the same body). A hand edit is read against it (hand-edit.ts).
 *
 * Local only: nothing here leaves the vault. A copy that does not parse, or whose body does not hash to its name, is
 * ignored (fail closed). A failed write is logged and never changes the sync result.
 */
import { createHash } from "node:crypto";
import { normalizeBody, parseWikiFile, wikiHash } from "./format";
import { type Written, type WrittenSection, writtenFallback, writtenFrom } from "./edit-diff";
import { type HandEditPlan, planHandEdit } from "./hand-edit";
import type { WikiNoteInput } from "./types";

/** The vault side the written copy needs: E3a's create-only writes plus a read of a hidden (unindexed) file. */
export interface WrittenStore {
  occupied(path: string): Promise<boolean>;
  ensureFolder(path: string): Promise<void>;
  create(path: string, text: string): Promise<void>;
  /** A file's text under .evowiki/ (not indexed by Obsidian), or null when there is none. */
  readHidden(path: string): Promise<string | null>;
}

export const WRITTEN_ROOT = ".evowiki/written";
const HASH = /^sha256:([0-9a-f]{64})$/;

const pageKey = (pageId: string): string => createHash("sha256").update(pageId, "utf8").digest("hex").slice(0, 32);

/** The copy's folder and path for a page and a note hash (`sha256:<hex>`); null for a hash of another shape. */
export function writtenPath(pageId: string, hash: string): { dir: string; path: string } | null {
  const m = HASH.exec(hash);
  if (m === null) return null;
  const dir = `${WRITTEN_ROOT}/${pageKey(pageId)}`;
  return { dir, path: `${dir}/${m[1]!}.json` };
}

/** Keeps the copy of what is about to be written for `input` (no-op when it is already there). Never throws. */
export async function keepWritten(store: WrittenStore, input: WikiNoteInput): Promise<void> {
  try {
    const w = writtenFrom(input);
    const at = writtenPath(w.pageId, w.hash);
    if (at === null || await store.occupied(at.path)) return;
    await store.ensureFolder(at.dir);
    await store.create(at.path, JSON.stringify({ v: 1, page_id: w.pageId, hash: w.hash, body: w.body, layout: w.layout }));
  } catch (e) {
    console.warn("EvoWiki: could not keep the written copy of a wiki note", e);
  }
}

const isInt = (x: unknown): x is number => typeof x === "number" && Number.isInteger(x) && x >= 0;
const isRange = (x: unknown): x is { from: number; to: number } =>
  typeof x === "object" && x !== null && isInt((x as { from?: unknown }).from) && isInt((x as { to?: unknown }).to)
  && (x as { from: number }).from <= (x as { to: number }).to;

/** The copy for this page and hash, checked (shape, page, hash, the body hashing to it, ordered ranges); else null. */
export async function loadWritten(store: Pick<WrittenStore, "readHidden">, pageId: string, hash: string): Promise<Written | null> {
  const at = writtenPath(pageId, hash);
  if (at === null) return null;
  let raw: unknown;
  try {
    const text = await store.readHidden(at.path);
    if (text === null) return null;
    raw = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof raw !== "object" || raw === null) return null;
  const o = raw as Record<string, unknown>;
  if (o.v !== 1 || o.page_id !== pageId || o.hash !== hash || typeof o.body !== "string") return null;
  if (wikiHash(pageId, o.body) !== hash) return null;
  const lines = normalizeBody(o.body).split("\n").length;
  const l = o.layout as { sections?: unknown } | null;
  if (typeof l !== "object" || l === null || !Array.isArray(l.sections)) return null;
  const sections: Written["layout"]["sections"] = [];
  let end = 0;
  for (const x of l.sections as unknown[]) {
    if (!isRange(x)) return null;
    const { id, label, artifactId, src } = x as { id?: unknown; label?: unknown; artifactId?: unknown; src?: WrittenSection["src"] };
    if (typeof id !== "string" || typeof label !== "string" || x.from < end || x.from > lines) return null;
    // `src` is absent in a copy from before sources were kept: hand-edit.ts fails closed on such a section.
    sections.push({ id, label, from: x.from, to: x.to, ...(typeof artifactId === "string" ? { artifactId } : {}), src: src as WrittenSection["src"] });
    end = x.to;
  }
  return { pageId, hash, body: o.body, layout: { sections } };
}

/**
 * What the user changed in the note's block, read against its written copy, or, when there is none or it predates the
 * sources it needs, against the current page's render if that is exactly what was written.
 */
export async function handEditFor(store: Pick<WrittenStore, "readHidden">, text: string, input: WikiNoteInput): Promise<HandEditPlan> {
  const p = parseWikiFile(text);
  if (p.kind !== "one" || p.version !== 2 || p.pageId !== input.pageId || !p.edited) return { kind: "none" };
  const kept = await loadWritten(store, input.pageId, p.hash);
  const w = kept !== null && kept.layout.sections.every((x) => x.src !== undefined) ? kept : writtenFallback(input, p.hash) ?? kept;
  return w === null ? { kind: "unsafe", why: "source", label: "" } : planHandEdit(text, w, input.sources ?? []);
}
