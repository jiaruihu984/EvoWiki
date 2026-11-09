import { describe, expect, it } from "vitest";
import { rowPending, rowProgress } from "./page-progress";
import { fillLayer, paintFill } from "./writing-fill";

const row = (state: Parameters<typeof rowProgress>[0]["state"], section = 0, of: number | null = null) =>
  ({ state, progress: state === "writing" ? { section, of, pass: null } : null });

describe("a page card's fill is its row's server state (page-progress.ts)", () => {
  it("planned / failed 0%; writing section / of (0% before the outline); written / read / mastered 100%", () => {
    expect([row("planned"), row("failed"), row("writing", 0, null), row("writing", 0, 3), row("writing", 1, 3), row("writing", 2, 3),
      row("written"), row("read"), row("mastered")].map(rowProgress)).toEqual([0, 0, 0, 0, 33, 67, 100, 100, 100]);
  });
  it("the path is read again while a row is written, or its page was just made and not started", () => {
    expect(rowPending({ state: "writing", pageId: "pg" })).toBe(true);
    expect(rowPending({ state: "planned", pageId: "pg" })).toBe(true);
    expect(rowPending({ state: "planned", pageId: null })).toBe(false);
    expect(rowPending({ state: "written", pageId: "pg" })).toBe(false);
  });
});

describe("the writing card (the approved writing-card demo)", () => {
  it("writing: p% plus the wave's 4 px (4 px at 0); written: the full card, plain; planned: none", () => {
    const f = fillLayer("writing", 0);
    expect([f.className, f.dataset.state, f.style.width, f.getAttribute("aria-hidden")]).toEqual(["ew-fill", "writing", "calc(0% + 4px)", "true"]);
    paintFill(f, "writing", 40);
    expect(f.style.width).toBe("calc(40% + 4px)");
    paintFill(f, "written", 100);
    expect([f.dataset.state, f.style.width]).toEqual(["done", "100%"]);
    paintFill(f, "planned", 0);
    expect([f.dataset.state, f.style.width]).toEqual(["idle", "0px"]);
  });
});

describe("a page streamed here fills from its own stream (streamProgress)", () => {
  it("0% before the outline, done / of while it writes, 100% written, 0% failed / stopped", async () => {
    const { streamProgress } = await import("./page-progress");
    const p = (done: number, of: number, ended = false, ok = false) => ({ pageId: "pg", done, of, ended, ok });
    expect([p(0, 0), p(0, 3), p(1, 3), p(2, 3), p(3, 3, true, true), p(1, 3, true, false)].map(streamProgress)).toEqual([0, 0, 33, 67, 100, 0]);
  });
});
