import { describe, expect, it } from "vitest";
import type { ReadingOut, ReadingPointOut, ReadingUnitOut } from "../schema";
import { useTarget } from "./reading";

const point = (id: string, o: Partial<ReadingPointOut> = {}): ReadingPointOut => ({
  id, name: id, atomic: true, known: false, mix: {}, mastery: null, page_id: null, on_map: false, ...o });

const unit = (order: number, o: Partial<ReadingUnitOut> = {}): ReadingUnitOut => ({
  unit_id: `ru_${order}`, order, title: `u${order}`, about: "", analyzed: true, skipped: false, page_id: null,
  status: "not_started", location: "", teaches: [], uses: [], focus: [], uncertain: false, ...o });

const reading = (units: ReadingUnitOut[]): ReadingOut => ({
  reading_id: "rd", source_id: "s", title: "T", path_id: null, units, progress: { done: 0, total: units.length } });

describe("useTarget (a 先补 chip)", () => {
  const r = reading([unit(1, { teaches: [point("att")] }), unit(2, { uses: [point("att"), point("A", { on_map: true })] })]);

  it("goes to the point's page when it has one", () => {
    expect(useTarget(point("att", { page_id: "pg-1" }), r)).toEqual({ kind: "page", pageId: "pg-1" });
  });

  it("goes to the unit that teaches it when it has no page (it is never on the map)", () => {
    expect(useTarget(point("att"), r)).toEqual({ kind: "unit", unitId: "ru_1" });
  });

  it("goes to its node on the map otherwise", () => {
    expect(useTarget(point("A", { on_map: true }), r)).toEqual({ kind: "node", nodeId: "A" });
    expect(useTarget(point("A"))).toEqual({ kind: "node", nodeId: "A" });
  });
});
