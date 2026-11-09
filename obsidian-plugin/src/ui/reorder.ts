/**
 * Drag-to-reorder for the lists the plugin draws from state (the 助手 draft card's rows, the 学点新的 学习路径 rows, the
 * 知识图谱 path rows), by SortableJS (MIT) in its pointer mode (`forceFallback`): no HTML5 drag-and-drop, so a file or
 * text dragged in from outside never reaches these lists (the panels' own drop zones get it), a row's text is never
 * dropped into a note, and touch works. While a row is dragged it stays faded at the place it would land, with the
 * demo's line above it (`.ew-sort-ghost`), and a copy follows the pointer (`.ew-sort-drag`).
 *
 * The state is the source of truth: when a drag ends at another place, Sortable's own move of the row is undone and the
 * caller's `move(from, to)` changes the state, which draws the list again. A list drawn again (or taken out) mid-drag
 * reports nothing. Alt + ↑ / ↓ on a focused row moves it by one (`byKey`: the caller puts the focus back on it).
 */
import Sortable from "sortablejs";

export interface ReorderOptions {
  /** A row moved from `from` to `to` (the index it ends at). */
  move: (from: number, to: number, byKey: boolean) => void;
  /** No dragging and no keys (a row is being renamed, say). */
  disabled?: boolean;
}

/** The rows: the list's own `li` children. */
const ROW = ":scope > li";

/** The rows Sortable moves and the index it reports count only these. */
const rowsOf = (list: HTMLElement): HTMLElement[] => [...list.querySelectorAll<HTMLElement>(ROW)].filter((x) => !x.classList.contains("ew-sort-drag"));

/** Puts `row` back at index `at` among the list's rows (Sortable already moved it; the redraw from state moves it). */
function putBack(list: HTMLElement, row: HTMLElement, at: number): void {
  const rest = rowsOf(list).filter((x) => x !== row);
  list.insertBefore(row, rest[at] ?? null);
}

/** What onEnd needs from Sortable's event (the rest of SortableEvent is not read). */
export interface EndEvent { item: HTMLElement; oldDraggableIndex?: number; newDraggableIndex?: number }

/** Makes `list`'s rows reorderable. Returns the function that stops it (destroys the Sortable instance). */
export function reorderable(list: HTMLElement, o: ReorderOptions): () => void {
  let live = true;
  const reduce = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const sortable = Sortable.create(list, {
    draggable: "li",
    disabled: o.disabled === true,
    forceFallback: true,
    fallbackTolerance: 4,                     // a click (×, ⋯, 进入) is not a drag
    filter: "button, input, textarea, select, a",
    preventOnFilter: false,                   // those controls keep their own clicks and focus
    animation: reduce ? 0 : 120,
    ghostClass: "ew-sort-ghost",
    chosenClass: "",
    dragClass: "ew-sort-drag",
    fallbackClass: "ew-sort-drag",
    onStart: (e) => { alignCopy(e.item); },
    onEnd: (e) => { endDrag(list, e, () => live, o); },
  });
  const onKey = (e: KeyboardEvent): void => {
    if (!e.altKey || o.disabled === true || !(e.target instanceof HTMLElement) || e.target.parentElement !== list) return;
    const k = rowsOf(list).indexOf(e.target);
    const j = e.key === "ArrowUp" ? k - 1 : e.key === "ArrowDown" ? k + 1 : -1;
    if (k < 0 || j < 0 || j >= rowsOf(list).length) return;
    e.preventDefault();
    o.move(k, j, true);
  };
  // the rows keep draggable=true (the demo's grab cursor); the browser's own drag of them is never started
  const noNative = (e: DragEvent): void => { if (e.target instanceof Element && e.target.closest("li") !== null) e.preventDefault(); };
  list.addEventListener("keydown", onKey);
  list.addEventListener("dragstart", noNative);
  return () => {
    if (!live) return;
    live = false;
    list.removeEventListener("keydown", onKey);
    list.removeEventListener("dragstart", noNative);
    sortable.destroy();
  };
}

/**
 * The copy under the pointer is `position: fixed` inside the list; in Obsidian a pane is a containing block for fixed
 * boxes (it sat a sidebar's width to the right in the real-Obsidian check), so it is moved back onto the row it copies.
 */
function alignCopy(item: HTMLElement): void {
  const g = Sortable.ghost;
  if (!g) return;
  const a = item.getBoundingClientRect(), b = g.getBoundingClientRect();
  g.style.marginLeft = `${a.left - b.left}px`;
  g.style.marginTop = `${a.top - b.top}px`;
}

/** Sortable's onEnd: undo its DOM move and report the move (nothing when the row ended where it was or the list is gone). */
export function endDrag(list: HTMLElement, e: EndEvent, alive: () => boolean, o: Pick<ReorderOptions, "move">): void {
  const from = e.oldDraggableIndex, to = e.newDraggableIndex;
  if (from === undefined || to === undefined || from === to) return;
  putBack(list, e.item, from);
  if (!alive() || !list.isConnected) return;
  o.move(from, to, false);
}
