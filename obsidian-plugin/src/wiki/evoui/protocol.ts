/**
 * The only channel between the main window and an EvoUI frame. Everything a frame sends is untrusted: `readFromFrame` is
 * the single place that turns a message into something the main window acts on, and it knows no way to reach the vault,
 * the network or a command. Sizes are the renderer's own limits with room to spare.
 */
import type { BlockEvent, BlockProgressData } from "./block-event";

export interface FrameTheme {
  /** Resolved values of the CSS variables the block's styles read. */
  vars: Record<string, string>;
  /** The plugin's own rules for the renderer's class names (frame-theme.ts). */
  css: string;
  dark: boolean;
  /** The block sits in a wiki note: the `.evowiki-page` rules apply. */
  page: boolean;
  font: { family: string; size: string; lineHeight: string; color: string };
}

export type ToFrame =
  | { t: "stop" }
  | { t: "init"; locale: "zh" | "en"; block: unknown; blockId: string | null; progress: BlockProgressData | null; theme: FrameTheme }
  | { t: "locale"; locale: "zh" | "en" }
  | { t: "theme"; theme: FrameTheme }
  | { t: "math"; id: number; html: string | null; css: string | null }
  | { t: "image"; id: number; bytes: ArrayBuffer | null }
  | { t: "ping"; n: number };

export type FromFrame =
  | { t: "stopped"; progress: BlockProgressData | null }
  | { t: "up"; boot: string }
  | { t: "pong"; n: number }
  | { t: "rendered" }
  | { t: "refused"; why: string }
  | { t: "height"; h: number }
  | { t: "event"; ev: BlockEvent }
  | { t: "progress"; progress: BlockProgressData }
  | { t: "math"; id: number; tex: string; display: boolean }
  | { t: "image"; id: number; asset: string };

/** What a block may ask of the page (the block is untrusted, so the cap is on this side). The tallest real block measured is about 1050 px; a longer one scrolls inside its frame. */
export const MAX_FRAME_HEIGHT = 6_000;
export const MAX_TEX_CHARS = 5_000;
const MAX_EVENT_CHARS = 8_000;
const MAX_PROGRESS_CHARS = 200_000;
const MAX_WHY_CHARS = 500;
const EVENT_TYPES = new Set(["item", "summary", "route", "request"]);

const isObject = (x: unknown): x is Record<string, unknown> => typeof x === "object" && x !== null && !Array.isArray(x);
const fits = (x: unknown, max: number): boolean => { try { return JSON.stringify(x).length <= max; } catch { return false; } };

/** The message, when it is exactly one of the shapes above; anything else is null and is dropped. */
export function readFromFrame(data: unknown): FromFrame | null {
  if (!isObject(data)) return null;
  switch (data.t) {
    case "up": return typeof data.boot === "string" && data.boot.length > 0 && data.boot.length <= 128 ? { t: "up", boot: data.boot } : null;
    case "rendered": return { t: "rendered" };
    case "pong": return typeof data.n === "number" ? { t: "pong", n: data.n } : null;
    case "refused": return { t: "refused", why: typeof data.why === "string" ? data.why.slice(0, MAX_WHY_CHARS) : "" };
    case "height": {
      const h = data.h;
      return typeof h === "number" && Number.isFinite(h) && h >= 0 ? { t: "height", h: Math.min(Math.ceil(h), MAX_FRAME_HEIGHT) } : null;
    }
    case "event": {
      const ev = data.ev;
      if (!isObject(ev) || typeof ev.type !== "string" || !EVENT_TYPES.has(ev.type) || typeof ev.node !== "string"
        || typeof ev.revision !== "number" || !fits(ev, MAX_EVENT_CHARS)) return null;
      return { t: "event", ev: ev as unknown as BlockEvent };
    }
    case "progress":
    case "stopped": {
      const p = data.progress;
      if (data.t === "stopped" && p === null) return { t: "stopped", progress: null };
      if (!isObject(p) || typeof p.revision !== "number" || !fits(p, MAX_PROGRESS_CHARS)) return null;
      return { t: data.t, progress: p as BlockProgressData };
    }
    case "math":
      return typeof data.id === "number" && typeof data.tex === "string" && data.tex.length <= MAX_TEX_CHARS && typeof data.display === "boolean"
        ? { t: "math", id: data.id, tex: data.tex, display: data.display } : null;
    case "image":
      return typeof data.id === "number" && typeof data.asset === "string" && data.asset.length <= 100
        ? { t: "image", id: data.id, asset: data.asset } : null;
    default: return null;
  }
}
