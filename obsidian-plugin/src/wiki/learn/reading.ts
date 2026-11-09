/**
 * 读懂这一篇 (reading-mode spec §5): what 学点新的 and the chat's goal run show of a goal's reading (`GoalMapOut.reading`,
 * computed by the server at read time). Pure.
 *
 * - A unit's status is the server's (its page only): 读完 never means 已掌握; a read unit shows its points' K colour.
 * - 「先补：X」: only the uses K does not hold yet (`known` false), each opening its page, else the unit that teaches it, else
 *   its node on the map.
 * - 下一步: the first unit, in the material's order, not read and not skipped; when it uses points not learned yet the step
 *   is still to read it, with the 先补 hint beside it (a soft hint, never a block).
 */
import type { Band, ReadingOut, ReadingPointOut, ReadingUnitOut } from "../schema";
import { LEARN_COPY } from "./copy";

/** The uses of `u` K does not hold yet (its 「先补：X」 chips). */
export const unknownUses = (u: ReadingUnitOut): ReadingPointOut[] => u.uses.filter((p) => !p.known);

/** 下一步: the first unit not read and not skipped (null: every unit read or skipped). */
export function nextUnit(r: ReadingOut | null | undefined): ReadingUnitOut | null {
  return r?.units.find((u) => u.status !== "done" && u.status !== "skipped") ?? null;
}

/** 「读到第 3 段 · 共 8 段」 (skipped units are not counted). */
export function progressLine(r: ReadingOut): string {
  const { done, total } = r.progress;
  return LEARN_COPY.readProgress(done, total);
}

/** The K colour of a read unit: its points' shares of bands, averaged ({} = nothing to colour: not read, or no cells). */
export function unitMix(u: ReadingUnitOut): Partial<Record<Band, number>> {
  if (u.status !== "done") return {};
  const ms = u.teaches.map((t) => t.mix).filter((m) => Object.keys(m).length > 0);
  if (ms.length === 0) return {};
  const out: Partial<Record<Band, number>> = {};
  for (const m of ms) for (const [b, v] of Object.entries(m) as [Band, number][]) out[b] = (out[b] ?? 0) + v / ms.length;
  return out;
}

/**
 * Where a 先补 chip goes: the point's page; else, when another unit of this reading teaches it (never on the map, §2.3),
 * that unit (opened as its row opens it); else its node on this goal's map.
 */
export type UseTarget = { kind: "page"; pageId: string } | { kind: "unit"; unitId: string } | { kind: "node"; nodeId: string };
export function useTarget(p: ReadingPointOut, r?: ReadingOut | null): UseTarget {
  if (p.page_id !== null) return { kind: "page", pageId: p.page_id };
  const teller = r?.units.find((u) => u.teaches.some((t) => t.id === p.id));
  return teller ? { kind: "unit", unitId: teller.unit_id } : { kind: "node", nodeId: p.id };
}
