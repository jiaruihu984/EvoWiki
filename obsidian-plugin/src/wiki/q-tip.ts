/**
 * The shared "?" tooltip (app.js `qTip(text, label)`, copy guide §2): a round circled-? button whose text shows on hover, keyboard
 * focus and click (never a visible line). `.ew-qtip` > `button.ew-qbtn` + `span.ew-tipbox[role=tooltip]`; the button is
 * `aria-describedby` the box. First line normal, the others dimmer (`.ew-tipd`). While it shows, the box is placed by
 * @floating-ui/dom (ui/floating.ts): where the demo's CSS puts it (under the ?, its right edge 6px past it; the per-place
 * rules in `TIP_PLACES`), flipped / shifted / narrowed so it is never cut off at an edge, and kept there while the pane
 * scrolls. One tip shows at a time; a click-opened tip closes on Esc and on a click outside it, Esc also hides a tip shown
 * by hover or focus until the pointer or the focus comes back.
 */
import type { Placement } from "@floating-ui/dom";
import { el } from "../ui/ew";
import { floatAt } from "../ui/floating";
import { s as svg } from "../ui/svg";
import { bindLabel } from "../i18n";
import type { Label } from "../ui/ew";

/** app.js ICON_Q (the question mark in a circle); `.ew-qbtn svg` gives it size and stroke. */
export const questionIcon = (): SVGElement => svg("svg", { viewBox: "0 0 24 24" },
  svg("circle", { cx: 12, cy: 12, r: 9 }), svg("path", { d: "M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6V14" }), svg("path", { d: "M12 17h.01" }));

/** The demo's per-place rules (styles.css EXACT: `.ew-mmenu` opens above, these open to the right), else under it, right-aligned. */
export const TIP_PLACES: readonly [string, Placement][] = [
  [".ew-mmenu", "top-end"],
  [".ew-arow, .ew-examb .ew-thead, .ew-revbar", "bottom-start"],
];
/** The demo's `top: calc(100% + 6px)` and `right: -6px` (or `left: -6px`). */
const TIP_GAP = 6, TIP_OVERHANG = 6;

export function tipPlacement(w: Element): Placement {
  for (const [sel, p] of TIP_PLACES) if (w.closest(sel) !== null) return p;
  return "bottom-end";
}

/** The tip being shown (placed) right now: one at a time. */
let shown: { w: HTMLElement; dismiss: () => void } | null = null;

let seq = 0;

/**
 * `text`: one line or several (empty lines are dropped). `label`: the button's aria-label. `onOpen` runs when the tip is
 * asked for (hover, focus or click), e.g. to read what it will say.
 */
export function qTip(text: string | readonly string[] | (() => string | readonly string[]), label: Label, onOpen?: () => void): HTMLSpanElement {
  const w = el("span", "qtip");
  const b = el("button", "qbtn");
  b.type = "button";
  b.append(questionIcon());
  // the name is a hidden span, not aria-label: Obsidian shows every aria-label as its own hover tooltip, on top of ours
  const name = el("span", "", label); name.className = "evowiki-sr-only"; b.append(name);
  const t = el("span", "tipbox");
  t.setAttribute("role", "tooltip");
  t.id = `ew-tip-${++seq}`;
  b.setAttribute("aria-describedby", t.id);
  const refresh = (): void => {
    const value = typeof text === "function" ? text() : text;
    const lines = ([] as string[]).concat(value as string | string[]).filter((x) => x !== "");
    t.replaceChildren(...lines.map((x, i) => el("span", i === 0 ? "" : "tipd", x)));
  };
  if (typeof text === "function") bindLabel(t, refresh); else refresh();

  const tip = attachTip(w, t);
  b.onclick = (e) => {
    e.stopPropagation();
    if (w.classList.toggle("ew-open")) tip.show(); else tip.settle();
    onOpen?.();
  };
  if (onOpen) { w.addEventListener("mouseenter", onOpen); w.addEventListener("focusin", onOpen); }
  w.append(b, t);
  return w;
}

/**
 * The tip behaviour on a `.ew-qtip` wrapper `w` and its `.ew-tipbox` `t` (the circled ? above, and the answer row's icon
 * buttons: assistant/rating-row.ts): shown while the pointer or the focus is on `w` (the demo's CSS shows it; it is placed
 * by floatAt meanwhile), one at a time, Esc hides it. `show` / `settle` are for a click that opens it (`.ew-open`).
 */
export function attachTip(w: HTMLElement, t: HTMLElement): { show(): void; settle(): void } {
  let hover = false, stop: (() => void) | null = null, off: (() => void) | null = null;
  const focused = (): boolean => w.contains(w.ownerDocument.activeElement);
  const hide = (): void => {
    stop?.(); stop = null; off?.(); off = null;
    if (shown?.w === w) shown = null;
  };
  /** Esc: closed, and hidden even while the pointer or the focus is still on it (until it comes back). */
  const dismiss = (): void => { w.classList.remove("ew-open"); t.style.display = "none"; hide(); };
  /** Nothing asks for it any more (not opened by a click, no pointer, no focus): stop placing it. */
  const settle = (): void => { if (!w.classList.contains("ew-open") && !hover && !focused()) hide(); };
  const show = (): void => {
    t.style.display = "";
    if (shown?.w === w) return;
    shown?.dismiss();                    // the other one closes, and hides even if it is still hovered or focused
    shown = { w, dismiss };
    stop = floatAt(w, t, { placement: tipPlacement(w), gap: TIP_GAP, overhang: TIP_OVERHANG });
    const doc = w.ownerDocument;
    const onKey = (e: KeyboardEvent): void => { if (e.key === "Escape") dismiss(); };
    const onDown = (e: Event): void => {
      if (w.contains(e.target as Node)) return;
      w.classList.remove("ew-open"); settle();
    };
    doc.addEventListener("keydown", onKey);
    doc.addEventListener("click", onDown, true);
    off = () => { doc.removeEventListener("keydown", onKey); doc.removeEventListener("click", onDown, true); };
  };

  w.addEventListener("mouseenter", () => { hover = true; show(); });
  w.addEventListener("mouseleave", () => { hover = false; settle(); });
  w.addEventListener("focusin", () => { show(); });
  w.addEventListener("focusout", (e) => { if (!w.contains(e.relatedTarget as Node | null)) queueMicrotask(settle); });
  return { show, settle };
}
