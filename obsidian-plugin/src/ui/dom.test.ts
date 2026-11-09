import { expect, it, vi } from "vitest";
import { h } from "./dom";

it("builds elements with classes, attributes, booleans, listeners and text children", () => {
  const onClick = vi.fn();
  const el = h("button", { class: "a b", "aria-label": "go", disabled: true, onclick: onClick }, "Go ", h("b", {}, "now"));
  expect(el.className).toBe("a b");
  expect(el.getAttribute("aria-label")).toBe("go");
  expect(el.disabled).toBe(true);
  expect(el.textContent).toBe("Go now");
  el.disabled = false; el.click();
  expect(onClick).toHaveBeenCalledOnce();
});

it("treats strings as text, never markup", () => {
  const el = h("div", {}, "<img src=x onerror=alert(1)>");
  expect(el.querySelector("img")).toBeNull();
  expect(el.textContent).toBe("<img src=x onerror=alert(1)>");
});

it("skips null, undefined and false children", () => {
  expect(h("p", {}, null, undefined, false, "x").childNodes.length).toBe(1);
});
