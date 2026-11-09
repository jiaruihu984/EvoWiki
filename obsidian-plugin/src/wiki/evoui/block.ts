/**
 * The `evoui` code block (an EvoUI interactive in a note): what the fence holds, and whether a block may be drawn.
 *
 *     ```evoui                      ```evoui
 *     {"type":"evoui", …}           asset: <asset id>
 *     ```                           ```
 *
 * The left form is what EvoWiki writes into a note (deep/evoui `block_fence`: the block JSON inline); the right one
 * points at an asset (`GET /wiki/assets/{id}`, meta.kind "evoui", cached as `<notes>/Wiki/assets/<id>.json`).
 * parseEvouiSource is the single place that turns a fence's text into a block (or an asset id); the final fence format is
 * D1's, so a change there lands here only. This file runs in the main window and only parses JSON: the block is validated
 * and evaluated in the sandboxed frame (frame/validate.ts, frame-host.ts).
 */
import { validAssetId } from "../embed/embed-block";

export const EVOUI_FENCE = "evoui";
/** spec.md / validate.py LIMITS.block, counted in characters of the block's JSON (the fence text is a bit longer: indentation). */
export const MAX_BLOCK_CHARS = 40_000;
/** The most fence text read at all (indented JSON of a block at its limit fits). */
export const MAX_SOURCE_CHARS = 120_000;

export type EvouiSource =
  | { kind: "block"; block: unknown }
  | { kind: "asset"; id: string }
  | { kind: "invalid" };

/** JSON inline (starts with `{`), or one `asset: <id>` line; anything else is invalid, never guessed. */
export function parseEvouiSource(source: string): EvouiSource {
  const text = source.trim();
  if (text.length === 0 || text.length > MAX_SOURCE_CHARS) return { kind: "invalid" };
  if (text.startsWith("{")) {
    try { return { kind: "block", block: JSON.parse(text) as unknown }; } catch { return { kind: "invalid" }; }
  }
  const lines = text.split(/\r\n?|\n/).map((l) => l.trim()).filter((l) => l !== "");
  const m = lines.length === 1 ? /^asset\s*:\s*(\S+)$/.exec(lines[0]!) : null;
  return m && validAssetId(m[1]!) ? { kind: "asset", id: m[1]! } : { kind: "invalid" };
}

/** The block's own fallback text when it has one (clipped to the spec's limit), else null. */
export function fallbackOf(block: unknown): string | null {
  if (block === null || typeof block !== "object") return null;
  const f = (block as { fallback?: unknown }).fallback;
  return typeof f === "string" && f.trim() !== "" ? f.trim().slice(0, 1500) : null;
}
