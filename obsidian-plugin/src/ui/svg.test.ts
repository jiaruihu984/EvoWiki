import { expect, it } from "vitest";
import { s } from "./svg";

it("builds namespaced SVG elements with attributes and text children, never markup", () => {
  const el = s("g", { class: "n", "data-id": "pca" }, s("title", {}, "<b>PCA</b>"));
  expect(el.namespaceURI).toBe("http://www.w3.org/2000/svg");
  expect(el.getAttribute("class")).toBe("n");
  expect(el.querySelector("b")).toBeNull();
  expect(el.textContent).toBe("<b>PCA</b>");
});
