/**
 * Test kit: a 读懂这一篇 goal (reading-mode spec §5) behind the GoalApi contract, for the 学点新的 / goal-run tests. The map
 * is the nested kit's (its blocks A 水质 / B 喂养 are the prerequisites, 先补) with a reading of four units:
 * 1 read (its point known), 2 never looked at (opening it shows it uses A — on the map, no page — and softmax, which has a
 * page), 3 skipped, 4 looked at (it uses a point already known). openUnit records `open:<unit>:<start>`; `start` gives the
 * unit its page (在读). No model, no timers.
 */
import type { GoalMapOut, ReadingOut, ReadingPointOut, ReadingUnitOpenOut, ReadingUnitOut } from "../schema";
import { NestedGoalApi } from "./nested-goal-kit";

export const READING_ID = "rd-1";

const point = (id: string, name: string, o: Partial<ReadingPointOut> = {}): ReadingPointOut => ({
  id, name, atomic: true, known: false, mix: {}, mastery: null, page_id: null, on_map: false, ...o });

const unit = (order: number, title: string, o: Partial<ReadingUnitOut> = {}): ReadingUnitOut => ({
  unit_id: `ru_${order}`, order, title, about: "", analyzed: false, skipped: false, page_id: null, status: "not_started", location: "",
  teaches: [], uses: [], focus: [], uncertain: false, ...o });

/** What opening unit 2 finds it uses: block A of the map (no page yet) and softmax (taught on a page already). */
export const UNIT2_USES = [point("A", "水质", { on_map: true }), point("sm", "softmax", { page_id: "pg-softmax" })];

export class ReadingGoalApi extends NestedGoalApi {
  units: ReadingUnitOut[] = [
    unit(1, "要解决什么问题", { about: "序列模型太慢", location: "§1 p.1", analyzed: true, page_id: "pg-r1", status: "done",
      teaches: [point("t1", "并行", { known: true, mix: { known: 1 }, mastery: 0.9, page_id: "pg-r1" })] }),
    unit(2, "自注意力", { about: "每个词看所有词", location: "§2 p.2" }),
    unit(3, "实验", { location: "§3 p.4", skipped: true, status: "skipped" }),
    unit(4, "结论", { location: "§4 p.5", analyzed: true, uses: [point("k1", "向量", { known: true, mix: { known: 1 } })] }),
  ];

  reading(): ReadingOut {
    const todo = this.units.filter((u) => u.status !== "skipped");
    return { reading_id: READING_ID, source_id: "src-1", title: "注意力就是一切", path_id: null, units: structuredClone(this.units),
      progress: { done: todo.filter((u) => u.status === "done").length, total: todo.length } };
  }

  override map(): GoalMapOut { return { ...super.map(), reading: this.reading() }; }

  openUnit(readingId: string, unitId: string, start: boolean): Promise<ReadingUnitOpenOut> {
    this.calls.push(`open:${unitId}:${start}`);
    const i = this.units.findIndex((u) => u.unit_id === unitId);
    if (readingId !== READING_ID || i < 0) return Promise.reject(new Error("no such unit"));
    let u = this.units[i]!;
    if (!u.analyzed) u = { ...u, analyzed: true, uses: unitId === "ru_2" ? structuredClone(UNIT2_USES) : u.uses };
    if (start && u.page_id === null) u = { ...u, page_id: `pg-${unitId}`, status: "reading" };
    this.units[i] = u;
    return Promise.resolve({ reading_id: READING_ID, map_id: super.map().map_id, unit: structuredClone(u), page_id: u.page_id,
      path_id: u.page_id !== null ? `path-${READING_ID}` : null, llm_calls: 0 });
  }
}
