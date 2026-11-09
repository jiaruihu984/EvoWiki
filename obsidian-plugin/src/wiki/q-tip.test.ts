/**
 * The shared "?" tooltip: placed by @floating-ui/dom where the demo's CSS puts it, kept inside the window (follow-up
 * 2026-10-22: it was cut off at the sidebar's left edge); one at a time; Esc and a click outside close it.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { at, rect, settle, sized, viewport } from "../ui/floating-test-kit";
import { attachTip, qTip, tipPlacement } from "./q-tip";

afterEach(() => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); document.body.replaceChildren(); vi.restoreAllMocks(); });

function mount(anchorAt: DOMRect, parentClass = ""): { w: HTMLElement; t: HTMLElement; b: HTMLButtonElement } {
  viewport(2160, 1350);                                     // the window of the live check
  const host = document.createElement("div");
  host.className = parentClass;
  document.body.append(host);
  const w = qTip(["原因", "细节"], "为什么改");
  host.append(w);
  const t = w.querySelector<HTMLElement>(".ew-tipbox")!;
  at(w, anchorAt); sized(t, 240, 60);
  return { w, t, b: w.querySelector("button")! };
}

describe("qTip placing", () => {
  it("hover: fixed under the ?, its right edge 6px past it (the demo's right: -6px; top: 100% + 6px)", async () => {
    const { w, t } = mount(rect(1000, 380, 20, 20));
    w.dispatchEvent(new MouseEvent("mouseenter"));
    await settle();
    expect(t.style.position).toBe("fixed");
    expect([t.style.left, t.style.top]).toEqual([`${1020 + 6 - 240}px`, `${400 + 6}px`]);
    expect(w.querySelector("button")!.getAttribute("aria-describedby")).toBe(t.id);
  });

  it("near the left edge it shifts back inside; at the bottom it opens above", async () => {
    const left = mount(rect(20, 380, 20, 20));
    left.w.dispatchEvent(new MouseEvent("mouseenter"));
    await settle();
    const x = parseFloat(left.t.style.left);
    expect(x).toBeGreaterThanOrEqual(8);
    expect(x + 240).toBeLessThanOrEqual(2160 - 8);
    left.w.dispatchEvent(new MouseEvent("mouseleave"));
    const low = mount(rect(1000, 1320, 20, 20));
    low.b.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    await settle();
    expect(low.t.style.top).toBe(`${1320 - 6 - 60}px`);
  });

  it("the demo's per-place rules: .ew-mmenu opens above, .ew-arow / .ew-revbar / the exam head open to the right", () => {
    expect(tipPlacement(mount(rect(0, 0, 20, 20)).w)).toBe("bottom-end");
    expect(tipPlacement(mount(rect(0, 0, 20, 20), "ew-mmenu").w)).toBe("top-end");
    for (const c of ["ew-arow", "ew-revbar"]) expect(tipPlacement(mount(rect(0, 0, 20, 20), c).w)).toBe("bottom-start");
    const exam = document.createElement("div"); exam.className = "ew-examb";
    const head = document.createElement("div"); head.className = "ew-thead"; exam.append(head); document.body.append(exam);
    const w = qTip("x", "y"); head.append(w);
    expect(tipPlacement(w)).toBe("bottom-start");
  });
});

describe("qTip open / close", () => {
  it("a click opens it; Esc closes it and hides it even while it is still focused, until it is asked for again", () => {
    const { w, t, b } = mount(rect(1000, 380, 20, 20));
    b.focus();
    b.click();
    expect(w.classList.contains("ew-open")).toBe(true);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(w.classList.contains("ew-open")).toBe(false);
    expect(t.style.display).toBe("none");
    w.dispatchEvent(new MouseEvent("mouseenter"));
    expect(t.style.display).toBe("");
  });

  it("a click outside closes a click-opened tip; a click inside it does not", () => {
    const { w, t, b } = mount(rect(1000, 380, 20, 20));
    b.click();
    t.click();
    expect(w.classList.contains("ew-open")).toBe(true);
    document.body.click();
    expect(w.classList.contains("ew-open")).toBe(false);
  });

  it("one at a time: opening a second closes (and hides) the first", () => {
    const one = mount(rect(1000, 380, 20, 20)), two = mount(rect(1200, 380, 20, 20));
    one.b.click();
    two.b.click();
    expect(one.w.classList.contains("ew-open")).toBe(false);
    expect(two.w.classList.contains("ew-open")).toBe(true);
    two.b.click();                                          // its button again: closed
    expect(two.w.classList.contains("ew-open")).toBe(false);
    // one shown by the keyboard focus, then another hovered: the focused one hides (it would show through :focus-within)
    one.b.focus();
    two.w.dispatchEvent(new MouseEvent("mouseenter"));
    expect(one.t.style.display).toBe("none");
    expect(two.t.style.display).toBe("");
  });

  it("onOpen runs on hover, focus and click", () => {
    const seen = vi.fn();
    const w = qTip("x", "y", seen); document.body.append(w);
    w.dispatchEvent(new MouseEvent("mouseenter"));
    w.querySelector("button")!.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    w.querySelector("button")!.click();
    expect(seen).toHaveBeenCalledTimes(3);
  });
});

describe("attachTip (the answer row's icon buttons: assistant/rating-row.ts)", () => {
  it("a .ew-qtip in .ew-arow is placed by floating-ui on hover: under the icon, its left edge 6px before it", async () => {
    viewport(2160, 1350);
    const row = document.createElement("div"); row.className = "ew-arow"; document.body.append(row);
    const w = document.createElement("span"); w.className = "ew-qtip";
    const b = document.createElement("button"); const t = document.createElement("span"); t.className = "ew-tipbox";
    w.append(b, t); row.append(w);
    attachTip(w, t);
    at(w, rect(1000, 380, 26, 26)); sized(t, 60, 30);
    w.dispatchEvent(new MouseEvent("mouseenter"));
    await settle();
    expect([t.style.position, t.style.left, t.style.top]).toEqual(["fixed", `${1000 - 6}px`, `${406 + 6}px`]);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(t.style.display).toBe("none");
  });
});
