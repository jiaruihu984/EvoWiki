import { describe, expect, it } from "vitest";
import type { SourceDetail, SourceStatus } from "../schema";
import { type Attached, canShift, canStart, shiftDepth, tocOf } from "./attach";

const a = (status: SourceStatus): Attached => ({ id: "s", title: "讲义.pdf", pages: null, status, progress: null });

describe("学点新的 · 上传附件 (pure logic)", () => {
  it("canStart: every material read (ready / partial); none attached is fine", () => {
    expect(canStart([])).toBe(true);
    expect(canStart([a("ready")])).toBe(true);
    expect(canStart([a("partial")])).toBe(true);
    expect(canStart([a("reading")])).toBe(false);
    expect(canStart([a("failed")])).toBe(false);
    expect(canStart([a("ready"), a("reading")])).toBe(false);
  });

  it("tocOf: the sections as 目录 rows (title, depth, PDF page)", () => {
    const d = { sections: [
      { id: "x1", parent_id: null, title: "第 1 章", depth: 0, page_from: 1, page_to: 2, tokens: 1, read_by: "text", summary: null },
      { id: "x2", parent_id: "x1", title: "1.1", depth: 1, page_from: null, page_to: null, tokens: 1, read_by: "text", summary: null },
    ] } as unknown as SourceDetail;
    expect(tocOf(d)).toEqual([{ id: "x1", title: "第 1 章", depth: 0, page: 1 }, { id: "x2", title: "1.1", depth: 1, page: null }]);
  });

  it("← → move a row one level, within 0..2", () => {
    const r = { id: "x", title: "t", depth: 0, page: 1 };
    expect(canShift(r, -1)).toBe(false);
    expect(canShift(r, 1)).toBe(true);
    expect(canShift({ ...r, depth: 2 }, 1)).toBe(false);
    expect(shiftDepth({ ...r, depth: 1 }, -1)).toBe(0);
    expect(shiftDepth({ ...r, depth: 2 }, 1)).toBe(2);
  });
});
