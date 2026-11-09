import { afterEach, describe, expect, it } from "vitest";
import { TreeMarker } from "./tree-marker";

const row = (path: string): HTMLElement => {
  const r = document.createElement("div");
  r.className = "nav-file-title";
  r.dataset.path = path;
  document.body.append(r);
  return r;
};
afterEach(() => { document.body.replaceChildren(); });

describe("TreeMarker", () => {
  it("marks the row of a writing page with class + 「生成中」, and removes it when done", () => {
    const a = row("EvoWiki/Wiki/统计/均值.md"), b = row("EvoWiki/Wiki/统计/方差.md");
    const m = new TreeMarker();
    m.set("p1", "EvoWiki/Wiki/统计/均值.md");
    expect(m.marked()).toEqual(["EvoWiki/Wiki/统计/均值.md"]);
    expect(a.querySelector(".ew-writing-mark")?.textContent).toBe("生成中");
    expect(b.classList.contains("ew-writing")).toBe(false);
    m.clear("p1");
    expect(m.marked()).toEqual([]);
    expect(a.querySelector(".ew-writing-mark")).toBeNull();
    m.dispose();
  });
  it("a row drawn later (Obsidian rebuilds the tree) gets the mark; a rename moves it; one mark per row", async () => {
    const m = new TreeMarker();
    m.set("p1", "w/a.md");
    m.set("p1", "w/a.md");
    const late = row("w/a.md");
    await new Promise((r) => setTimeout(r, 60));
    expect(late.querySelectorAll(".ew-writing-mark")).toHaveLength(1);
    const moved = row("w/b.md");
    m.set("p1", "w/b.md");
    expect(late.querySelector(".ew-writing-mark")).toBeNull();
    expect(moved.querySelector(".ew-writing-mark")).not.toBeNull();
    m.dispose();
    expect(m.marked()).toEqual([]);
  });
  it("no row in the DOM (folder collapsed): nothing happens, nothing throws", () => {
    const m = new TreeMarker();
    m.set("p1", "w/a.md");
    expect(m.marked()).toEqual([]);
    m.dispose();
  });
});
