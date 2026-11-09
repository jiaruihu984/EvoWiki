/**
 * The one EvoUI renderer (D1, src/evowiki/deep/evoui/evoui.js): imported straight from the repository and never copied
 * or ported. Only the sandboxed frame loads it (frame/entry.ts); the plugin's main window never does. It is a browser script that registers `window.EvoUI` ({render, validate, evalExpr,
 * EvoUIError}); its expression parser has no eval / Function and sees only the block's own names and a fixed builtin
 * table. This module only types that global. Nothing in the plugin edits the D1 files.
 */
import "../../../../../src/evowiki/deep/evoui/evoui.js";
import type { BlockEvent, BlockProgressData } from "../block-event";

export interface EvoUIHandle { state: Record<string, unknown>; set(name: string, value: unknown): void; setLanguage(language: "zh" | "en"): void; destroy(): BlockProgressData }
export interface EvoUIEngine {
  /** Replaces `mount`'s children with the block; throws EvoUIError when the block does not validate. */
  /** `blockId` is copied into every event's `block`; `onEvent` gets each result event as it happens. */
  render(block: unknown, mount: HTMLElement, opts?: {
    language?: "zh" | "en";
    blockId?: string | null; onEvent?: (ev: BlockEvent) => void;
    /** A progress of an earlier session to restore (never fires events); `onProgress` gets the current one after changes (debounced). */
    progress?: BlockProgressData; onProgress?: (progress: BlockProgressData) => void;
    immediateProgress?: boolean;
    scheduleUpdate?: (paint: () => void) => void;
    /** The host's LaTeX renderer for `math` nodes (runtime.ts asks the main window's MathJax); without it a formula shows its source. */
    renderMath?: (tex: string, display: boolean) => Element;
    /** The bytes of one of an app's images (`evowiki.image`); without it the app's promise rejects. */
    image?: (image: { asset?: string }) => Promise<{ mime: string; bytes: ArrayBuffer } | null>;
  }): EvoUIHandle;
  /** The spec's rules, rule for rule as deep/evoui/validate.py; an empty list = valid. */
  validate(block: unknown): string[];
}

declare global {
  interface Window { EvoUI?: EvoUIEngine }
}

/** The renderer, or null when its script did not register (never expected; callers then show the fallback). */
export function evoEngine(): EvoUIEngine | null {
  return typeof window === "undefined" ? null : window.EvoUI ?? null;
}
