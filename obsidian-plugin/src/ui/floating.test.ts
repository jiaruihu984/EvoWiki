import { afterEach, describe, expect, it, vi } from "vitest";
import { EDGE_PAD, floatAt, menuKeys, openMenuAt } from "./floating";
import { at, rect, settle, sized, viewport } from "./floating-test-kit";

afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); });

function pair(anchorAt: DOMRect, w: number, h: number): { a: HTMLElement; f: HTMLElement } {
  viewport(1000, 800);
  const a = document.createElement("button"), f = document.createElement("div");
  document.body.append(a, f);
  at(a, anchorAt); sized(f, w, h);
  return { a, f };
}

describe("floatAt (@floating-ui/dom)", () => {
  it("fixed, under the anchor, right edges lined up plus the overhang", async () => {
    const { a, f } = pair(rect(500, 100, 20, 20), 200, 50);
    const stop = floatAt(a, f, { placement: "bottom-end", gap: 6, overhang: 6 });
    await settle();
    expect(f.style.position).toBe("fixed");
    expect([f.style.left, f.style.top]).toEqual([`${520 + 6 - 200}px`, `${120 + 6}px`]);
    stop();
  });

  it("flips above at the bottom of the window and shifts back inside the edge", async () => {
    const { a, f } = pair(rect(980, 760, 20, 20), 200, 50);
    const stop = floatAt(a, f, { placement: "bottom-start", gap: 4 });
    await settle();
    expect(f.style.top).toBe(`${760 - 4 - 50}px`);
    expect(f.style.left).toBe(`${1000 - EDGE_PAD - 200}px`);
    expect(f.dataset.placement).toMatch(/^top-/);
    stop();
  });

  it("the overhang as a function of the anchor's width (app.js sectionMenu: left = right − 190)", async () => {
    const { a, f } = pair(rect(480, 80, 20, 20), 202, 120);
    const stop = floatAt(a, f, { placement: "bottom-start", gap: 4, overhang: (w) => w - 190 });
    await settle();
    expect([f.style.left, f.style.top]).toEqual([`${500 - 190}px`, `${100 + 4}px`]);
    stop();
  });

  it("not in the document yet: placed once it is (the next microtask); nothing after stop", async () => {
    viewport(1000, 800);
    const a = document.createElement("button"), f = document.createElement("div");
    document.body.append(a); at(a, rect(100, 100, 20, 20)); sized(f, 100, 40);
    const stop = floatAt(a, f, { placement: "bottom-start", gap: 4 });
    document.body.append(f);
    await settle();
    expect(f.style.top).toBe("124px");
    stop();
    const g = document.createElement("div"); sized(g, 100, 40);
    floatAt(a, g, { placement: "bottom-start", gap: 4 })();
    document.body.append(g);
    await settle();
    expect(g.style.top).toBe("");
  });
});

describe("menuKeys / openMenuAt (ARIA menu pattern)", () => {
  function menu(): { m: HTMLElement; b: HTMLButtonElement[] } {
    const m = document.createElement("div");
    const b = ["一", "二", "三", "四"].map((t) => { const x = document.createElement("button"); x.textContent = t; m.append(x); return x; });
    b[2]!.disabled = true;
    document.body.append(m);
    return { m, b };
  }
  const key = (t: HTMLElement, k: string): KeyboardEvent => {
    const e = new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true });
    t.dispatchEvent(e); return e;
  };

  it("ArrowDown / ArrowUp wrap over the enabled items, Home / End go to the ends", () => {
    const { m, b } = menu();
    menuKeys(m, vi.fn());
    b[0]!.focus();
    key(b[0]!, "ArrowDown"); expect(document.activeElement).toBe(b[1]);
    key(b[1]!, "ArrowDown"); expect(document.activeElement).toBe(b[3]);        // the disabled one is skipped
    key(b[3]!, "ArrowDown"); expect(document.activeElement).toBe(b[0]);
    key(b[0]!, "ArrowUp"); expect(document.activeElement).toBe(b[3]);
    key(b[3]!, "Home"); expect(document.activeElement).toBe(b[0]);
    expect(key(b[0]!, "End").defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(b[3]);
  });

  it("Esc closes and asks for the focus back, without reaching the document; Tab closes too; other keys pass", () => {
    const { m, b } = menu();
    const close = vi.fn(), seen = vi.fn();
    document.addEventListener("keydown", seen);
    menuKeys(m, close);
    b[0]!.focus();
    key(b[0]!, "Escape");
    expect(close).toHaveBeenLastCalledWith(true);
    expect(seen).not.toHaveBeenCalled();
    expect(key(b[0]!, "Tab").defaultPrevented).toBe(false);
    expect(close).toHaveBeenCalledTimes(2);
    expect(key(b[0]!, "a").defaultPrevented).toBe(false);
    expect(close).toHaveBeenCalledTimes(2);
    document.removeEventListener("keydown", seen);
  });

  it("openMenuAt focuses the first enabled item once the menu is in the document", async () => {
    viewport(1000, 800);
    const btn = document.createElement("button"); document.body.append(btn); at(btn, rect(500, 100, 20, 20));
    const { m, b } = menu(); b[0]!.disabled = true; sized(m, 150, 90);
    const stop = openMenuAt(btn, m, vi.fn(), true);
    await settle();
    expect(document.activeElement).toBe(b[1]);
    expect([m.style.left, m.style.top]).toEqual([`${520 - 150}px`, `${120 + 3}px`]);
    stop();
  });
});
