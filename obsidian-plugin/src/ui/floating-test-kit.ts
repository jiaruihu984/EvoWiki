/** jsdom has no layout: what @floating-ui/dom measures (the window, an element's box and size) is given here instead. */
import { vi } from "vitest";

export const rect = (left: number, top: number, width: number, height: number): DOMRect =>
  ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) });

/** The window (the viewport the popups are kept inside). */
export function viewport(width: number, height: number): void {
  const html = document.documentElement;
  vi.spyOn(html, "clientWidth", "get").mockReturnValue(width);
  vi.spyOn(html, "clientHeight", "get").mockReturnValue(height);
  vi.spyOn(window, "innerWidth", "get").mockReturnValue(width);
  vi.spyOn(window, "innerHeight", "get").mockReturnValue(height);
}

/** An anchor where it is on screen. */
export function at(node: HTMLElement, r: DOMRect): void {
  vi.spyOn(node, "getBoundingClientRect").mockReturnValue(r);
  vi.spyOn(node, "offsetWidth", "get").mockReturnValue(r.width);
  vi.spyOn(node, "offsetHeight", "get").mockReturnValue(r.height);
}

/** A popup of this size; its box is where floating-ui last put it (style.left / top). */
export function sized(node: HTMLElement, width: number, height: number): void {
  vi.spyOn(node, "offsetWidth", "get").mockReturnValue(width);
  vi.spyOn(node, "offsetHeight", "get").mockReturnValue(height);
  vi.spyOn(node, "getBoundingClientRect").mockImplementation(() =>
    rect(parseFloat(node.style.left) || 0, parseFloat(node.style.top) || 0, width, height));
}

/** Lets computePosition (a promise chain) finish. */
export const settle = (): Promise<void> => new Promise((r) => { setTimeout(r, 0); });
