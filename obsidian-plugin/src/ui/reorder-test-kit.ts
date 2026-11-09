/** jsdom cannot drag: a drag is played the way SortableJS ends one (its own DOM move, then onEnd with the indices). */
import Sortable from "sortablejs";

/** Drags the row at `from` of `list` so that it ends at index `to`, as SortableJS would. */
export function sortDrag(list: HTMLElement, from: number, to: number): void {
  const s = Sortable.get(list);
  if (!s) throw new Error("not a reorderable list");
  const rows = [...list.children] as HTMLElement[];
  const item = rows[from]!;
  item.remove();
  list.insertBefore(item, list.children[to] ?? null);           // what Sortable does on the screen while dragging
  (s.option("onEnd") as (e: { item: HTMLElement; oldDraggableIndex: number; newDraggableIndex: number }) => void)(
    { item, oldDraggableIndex: from, newDraggableIndex: to });
}
