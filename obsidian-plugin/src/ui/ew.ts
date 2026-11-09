/**
 * The demo's two DOM helpers (full-demo/src/app.js lines 4–5), with every class token prefixed `ew-` so the EXACT CSS
 * block in styles.css applies (`.btn.primary` in the demo is `.ew-btn.ew-primary` here). Text is always a text node.
 */

import { localizedText } from "../i18n";
export type Label = string | (() => string);
/** "btn primary" → "ew-btn ew-primary" (every whitespace-separated token prefixed, empty tokens dropped). */
export function ewClass(cls: string): string {
  return cls.split(/\s+/).filter((c) => c !== "").map((c) => "ew-" + c).join(" ");
}

/** app.js `el(tag, cls, txt)`: className only when `cls` is non-empty, textContent only when `txt` is not null/undefined. */
export function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string | null, txt?: Label | null): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = ewClass(cls);
  if (typeof txt === "function") localizedText(e, txt);
  else if (txt != null) e.textContent = txt;
  return e;
}

/** app.js `btn(txt, cls, fn)`: a type=button `.ew-btn` (+ `cls`), `fn` as its onclick. */
export function btn(txt: Label | null, cls?: string | null, fn?: ((e: MouseEvent) => unknown) | null): HTMLButtonElement {
  const b = el("button", "btn" + (cls ? " " + cls : ""), txt);
  b.type = "button";
  if (fn) b.onclick = fn;
  return b;
}
