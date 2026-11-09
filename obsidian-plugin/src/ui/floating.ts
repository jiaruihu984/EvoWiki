/**
 * Popups placed next to what opened them, by @floating-ui/dom (MIT): the circled-? tooltip (wiki/q-tip.ts) and the ⋯
 * menus (the note's section menu, the 学点新的 and 知识图谱 row menus). The popup keeps its DOM place and its classes (the
 * demo's CSS still draws it); only where it shows is computed: `position: fixed` at the anchor, flipped to the other side
 * when there is no room, shifted back inside the window / the clipping pane, capped to the width there is, and kept there
 * while the note scrolls or the pane resizes (autoUpdate) until it is stopped; hidden while its anchor is scrolled out of
 * sight.
 *
 * `menuKeys` is the ARIA menu pattern for those menus: ArrowUp / ArrowDown (wrapping), Home, End move the focus between
 * the enabled items; Esc closes and puts the focus back on the button; Tab does too and lets the Tab move on from it.
 */
import { autoUpdate, computePosition, flip, hide, offset, type Placement, shift, size } from "@floating-ui/dom";

export interface FloatOptions {
  /** Where the popup opens (it flips to the other side when there is no room). */
  placement: Placement;
  /** Gap between the anchor and the popup. */
  gap: number;
  /**
   * Along the anchor: how far the popup's aligned edge sticks out past the anchor's (the demo's `right: -6px` is 6), or,
   * given the anchor's width, how far it is moved toward the anchor's other edge (app.js sectionMenu `r.right - 190`).
   */
  overhang?: number | ((anchorWidth: number) => number);
}

/** Room kept between a popup and the edge it would cross. */
export const EDGE_PAD = 8;

/**
 * Places `floating` next to `anchor` and keeps it there until the returned function is called. When `floating` is not in
 * the document yet (a list still being rebuilt), placing starts once it is, on the next microtask.
 */
export function floatAt(anchor: Element, floating: HTMLElement, o: FloatOptions): () => void {
  let stopped = false;
  let stopAuto: (() => void) | null = null;
  const st = floating.style;
  const update = (): void => {
    if (stopped || !floating.isConnected) return;
    // the CSS cap (the tooltip's max-width: 240px) still applies: the width there is only lowers it
    st.maxWidth = "";
    const view = floating.ownerDocument.defaultView;
    const cssMax = view ? parseFloat(view.getComputedStyle(floating).maxWidth) : NaN;
    void computePosition(anchor, floating, {
      strategy: "fixed",
      placement: o.placement,
      middleware: [
        offset(({ rects }) => ({
          mainAxis: o.gap,
          alignmentAxis: typeof o.overhang === "function" ? o.overhang(rects.reference.width) : -(o.overhang ?? 0),
        })),
        flip({ padding: EDGE_PAD }),
        shift({ padding: EDGE_PAD }),
        size({
          padding: EDGE_PAD,
          apply({ availableWidth }) {
            const w = Number.isFinite(cssMax) ? Math.min(cssMax, availableWidth) : availableWidth;
            if (w > 0) st.maxWidth = `${w}px`;
          },
        }),
        hide(),
      ],
    }).then(({ x, y, placement, middlewareData }) => {
      if (stopped) return;
      st.left = `${x}px`; st.top = `${y}px`;
      // the anchor scrolled out of its pane (under the sidebar's tabs, say): the popup is not left floating over them
      st.visibility = middlewareData.hide?.referenceHidden === true ? "hidden" : "";
      floating.dataset.placement = placement;
    });
  };
  const start = (): void => {
    if (stopped || !floating.isConnected) return;
    // inline, so the demo's own placing (position: absolute; right / top / bottom) gives way
    st.position = "fixed"; st.right = "auto"; st.bottom = "auto"; st.transform = "";
    stopAuto = autoUpdate(anchor, floating, update);
  };
  if (floating.isConnected) start(); else queueMicrotask(start);
  return () => { stopped = true; stopAuto?.(); stopAuto = null; };
}

/** The menu's items the focus can be on (disabled ones are skipped). */
const items = (menu: HTMLElement): HTMLElement[] => [...menu.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];

/** Focuses the menu's first enabled item (the menu was just opened). */
export function focusFirst(menu: HTMLElement): void { items(menu)[0]?.focus({ preventScroll: true }); }

/** The menu's keys (ARIA menu pattern). `close` closes the menu; with `refocus` it should put the focus back on its button. */
export function menuKeys(menu: HTMLElement, close: (refocus: boolean) => void): void {
  menu.addEventListener("keydown", (e) => {
    const list = items(menu);
    const at = list.indexOf(menu.ownerDocument.activeElement as HTMLElement);
    let next = -1;
    switch (e.key) {
      case "ArrowDown": next = at < 0 ? 0 : (at + 1) % list.length; break;
      case "ArrowUp": next = at < 0 ? list.length - 1 : (at - 1 + list.length) % list.length; break;
      case "Home": next = 0; break;
      case "End": next = list.length - 1; break;
      case "Escape": e.preventDefault(); e.stopPropagation(); close(true); return;
      case "Tab": close(true); return;               // back on the button: the Tab moves on from there
      default: return;
    }
    e.preventDefault(); e.stopPropagation();
    list[next]?.focus({ preventScroll: true });
  });
}

/**
 * A ⋯ menu opened under its button (`at`: bottom-end, right edges lined up, unless given), flipped above when there is no
 * room; its keys, and (when `focus`) the focus on its first item once it is in the document. Returns the function that
 * stops the placing.
 */
export function openMenuAt(button: HTMLElement, menu: HTMLElement, close: (refocus: boolean) => void, focus: boolean,
  at: FloatOptions = { placement: "bottom-end", gap: 3 }): () => void {
  menuKeys(menu, close);
  const stop = floatAt(button, menu, at);
  if (focus) queueMicrotask(() => { if (menu.isConnected) focusFirst(menu); });
  return stop;
}
