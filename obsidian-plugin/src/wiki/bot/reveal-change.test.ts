import { afterEach, describe, expect, it, vi } from "vitest";
import { revealChange, showChangeOnScreen } from "./reveal-change";

afterEach(() => { document.body.replaceChildren(); });
const queue = (): { frame: (f: () => void) => void; step: () => void } => {
  const q: (() => void)[] = [];
  return { frame: (f) => { q.push(f); }, step: () => { const f = q.shift(); f?.(); } };
};
function chg(key: string): { row: HTMLElement; scroll: ReturnType<typeof vi.fn> } {
  const row = document.createElement("div");
  row.className = "ew-chg";
  row.dataset.chg = key;
  const scroll = vi.fn();
  row.scrollIntoView = scroll;
  return { row, scroll };
}
const mount = (...rows: HTMLElement[]): HTMLElement => {
  const root = document.createElement("div");
  root.append(...rows);
  document.body.append(root);
  return root;
};

describe("revealChange", () => {
  it("on the next frame scrolls the [data-chg] row to the centre and adds the ants frame inside it", () => {
    const x = chg("act:x"), y = chg("act:y");
    const q = queue();
    revealChange(mount(x.row, y.row), "act:x", false, q.frame);
    expect(x.row.querySelector("svg.ew-ants")).toBeNull();
    q.step();
    expect(x.scroll).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });
    expect(y.scroll).not.toHaveBeenCalled();
    expect(x.row.querySelector("svg.ew-ants")).not.toBeNull();
    expect(y.row.querySelector("svg.ew-ants")).toBeNull();
  });

  it("reduced motion scrolls without animation; a row not drawn yet is waited for; a missing one gives up quietly", () => {
    const root = mount();
    const q = queue();
    revealChange(root, "rsi:p", true, q.frame);
    q.step();
    const p = chg("rsi:p");
    root.append(p.row);
    q.step();
    expect(p.scroll).toHaveBeenCalledWith({ behavior: "auto", block: "center" });
    const gone = queue();
    revealChange(root, "none", false, gone.frame);
    for (let i = 0; i < 40; i++) gone.step();
    expect(root.querySelectorAll("svg.ew-ants")).toHaveLength(1);
  });
});

describe("showChangeOnScreen", () => {
  it("reveals the wiki leaf first, then scrolls and frames the row", async () => {
    const x = chg("act:x");
    const order: string[] = [];
    const leaf = { id: "leaf" };
    x.scroll.mockImplementation(() => { order.push("scroll"); });
    const root = mount(x.row);
    const reveal = vi.fn(() => { order.push("reveal"); return Promise.resolve(); });
    await showChangeOnScreen({ view: () => ({ leaf, root }), revealLeaf: reveal, reduce: () => false, frame: (f) => { f(); } }, "act:x");
    expect(reveal).toHaveBeenCalledWith(leaf);
    expect(order).toEqual(["reveal", "scroll"]);
    expect(x.row.querySelector("svg.ew-ants")).not.toBeNull();
  });

  it("no wiki view: nothing happens; a failed reveal still scrolls", async () => {
    const reveal = vi.fn();
    await showChangeOnScreen({ view: () => null, revealLeaf: reveal, reduce: () => false, frame: (f) => { f(); } }, "k");
    expect(reveal).not.toHaveBeenCalled();
    const x = chg("k");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    await showChangeOnScreen({ view: () => ({ leaf: 1, root: mount(x.row) }), revealLeaf: () => Promise.reject(new Error("no")), reduce: () => false, frame: (f) => { f(); } }, "k");
    expect(x.scroll).toHaveBeenCalled();
    warn.mockRestore();
  });
});
