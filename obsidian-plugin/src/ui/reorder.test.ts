import Sortable from "sortablejs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { endDrag, reorderable } from "./reorder";
import { sortDrag } from "./reorder-test-kit";

afterEach(() => { document.body.replaceChildren(); });

function list(n = 4): { ol: HTMLOListElement; rows: HTMLLIElement[] } {
  const ol = document.createElement("ol");
  const rows = Array.from({ length: n }, (_, i) => {
    const li = document.createElement("li"); li.textContent = "r" + i; li.tabIndex = 0; li.draggable = true;
    const x = document.createElement("button"); x.textContent = "×"; li.append(x);
    ol.append(li); return li;
  });
  document.body.append(ol);
  return { ol, rows };
}
const text = (ol: HTMLElement): string[] => [...ol.children].map((x) => x.firstChild!.textContent!);
const key = (t: HTMLElement, k: string, alt = true): KeyboardEvent => {
  const e = new KeyboardEvent("keydown", { key: k, altKey: alt, bubbles: true, cancelable: true }); t.dispatchEvent(e); return e;
};

describe("reorderable (SortableJS)", () => {
  it("a Sortable in pointer mode on the list, with the demo's classes; stop destroys it", () => {
    const { ol } = list();
    const stop = reorderable(ol, { move: vi.fn() });
    const s = Sortable.get(ol)!;
    expect([s.option("forceFallback"), s.option("draggable"), s.option("ghostClass"), s.option("fallbackClass"), s.option("chosenClass")])
      .toEqual([true, "li", "ew-sort-ghost", "ew-sort-drag", ""]);
    expect(s.option("filter")).toContain("button");
    stop();
    expect(Sortable.get(ol)).toBeFalsy();
    stop();                                                         // twice: nothing
  });

  it("a drag that ends elsewhere: Sortable's DOM move is undone and move(from, to) is called once (the state redraws)", () => {
    const { ol } = list();
    const move = vi.fn();
    reorderable(ol, { move });
    sortDrag(ol, 0, 2);
    expect(move).toHaveBeenCalledExactlyOnceWith(0, 2, false);
    expect(text(ol)).toEqual(["r0", "r1", "r2", "r3"]);           // the DOM is the state's until it is drawn again
    sortDrag(ol, 3, 1);
    expect(move).toHaveBeenLastCalledWith(3, 1, false);
    expect(text(ol)).toEqual(["r0", "r1", "r2", "r3"]);
  });

  it("nothing for a drag that ends where it began, after stop, or when the list was drawn again mid-drag", () => {
    const { ol, rows } = list();
    const move = vi.fn();
    reorderable(ol, { move });
    endDrag(ol, { item: rows[1]!, oldDraggableIndex: 1, newDraggableIndex: 1 }, () => true, { move });
    const other = list();
    const stop = reorderable(other.ol, { move });
    const onEnd = Sortable.get(other.ol)!.option("onEnd") as (e: unknown) => void;
    stop();
    onEnd({ item: other.rows[0], oldDraggableIndex: 0, newDraggableIndex: 2 });
    const gone = list();
    reorderable(gone.ol, { move });
    gone.ol.remove();
    sortDrag(gone.ol, 0, 3);
    expect(move).not.toHaveBeenCalled();
  });

  it("Alt + ↑ / ↓ on a focused row moves it by one (byKey); the ends, other keys, no Alt, a button inside, disabled: nothing", () => {
    const { ol, rows } = list();
    const move = vi.fn();
    reorderable(ol, { move });
    expect(key(rows[1]!, "ArrowDown").defaultPrevented).toBe(true);
    expect(move).toHaveBeenLastCalledWith(1, 2, true);
    key(rows[1]!, "ArrowUp");
    expect(move).toHaveBeenLastCalledWith(1, 0, true);
    key(rows[0]!, "ArrowUp"); key(rows[3]!, "ArrowDown"); key(rows[2]!, "ArrowLeft"); key(rows[2]!, "ArrowDown", false);
    key(rows[2]!.querySelector("button")!, "ArrowDown");
    expect(move).toHaveBeenCalledTimes(2);
    const off = list();
    reorderable(off.ol, { move, disabled: true });
    key(off.rows[0]!, "ArrowDown");
    expect(Sortable.get(off.ol)!.option("disabled")).toBe(true);
    expect(move).toHaveBeenCalledTimes(2);
  });

  it("only its own rows: the browser's drag of a row never starts, a file dragged over the list is left to the panel", () => {
    const { ol, rows } = list();
    reorderable(ol, { move: vi.fn() });
    const start = new Event("dragstart", { bubbles: true, cancelable: true });
    rows[0]!.dispatchEvent(start);
    expect(start.defaultPrevented).toBe(true);
    for (const t of ["dragenter", "dragover", "drop"]) {
      const e = new Event(t, { bubbles: true, cancelable: true });
      let reached = false;
      document.body.addEventListener(t, () => { reached = true; }, { once: true });
      rows[1]!.dispatchEvent(e);
      expect([t, e.defaultPrevented, reached]).toEqual([t, false, true]);
    }
    expect(ol.querySelectorAll(".ew-sort-ghost, .ew-sort-drag, .ew-over")).toHaveLength(0);
  });
});
