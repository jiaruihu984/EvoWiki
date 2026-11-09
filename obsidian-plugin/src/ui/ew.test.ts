import { describe, expect, it, vi } from "vitest";
import { btn, el, ewClass } from "./ew";

describe("ew helpers (app.js el / btn with ew- prefixed classes)", () => {
  it("ewClass prefixes every token and drops empty ones", () => {
    expect(ewClass("btn primary")).toBe("ew-btn ew-primary");
    expect(ewClass("  lk ")).toBe("ew-lk");
    expect(ewClass("")).toBe("");
  });
  it("el sets className only for a non-empty cls and textContent only for a non-null txt", () => {
    const a = el("div", "card feature", "标题");
    expect(a.tagName).toBe("DIV");
    expect(a.className).toBe("ew-card ew-feature");
    expect(a.textContent).toBe("标题");
    const b = el("span", "", "x");
    expect(b.hasAttribute("class")).toBe(false);
    const c = el("p");
    expect(c.hasAttribute("class")).toBe(false);
    expect(c.childNodes.length).toBe(0);
    expect(el("b", null, "").childNodes.length).toBe(0); // "" is not null: textContent = "" (as app.js)
  });
  it("text is a text node, never markup", () => {
    const e = el("div", "muted", "<b>x</b>");
    expect(e.children.length).toBe(0);
    expect(e.textContent).toBe("<b>x</b>");
  });
  it('btn("记下","primary") is button.ew-btn.ew-primary type=button with the click handler', () => {
    const fn = vi.fn();
    const b = btn("记下", "primary", fn);
    expect(b.tagName).toBe("BUTTON");
    expect(b.className).toBe("ew-btn ew-primary");
    expect(b.type).toBe("button");
    expect(b.textContent).toBe("记下");
    b.click();
    expect(fn).toHaveBeenCalledTimes(1);
    const plain = btn("取消");
    expect(plain.className).toBe("ew-btn");
    expect(plain.onclick).toBeNull();
  });
});
