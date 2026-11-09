import { describe, expect, it } from "vitest";
import { headingAt } from "./selection-ask";

const h = (heading: string, line: number) => ({ heading, position: { start: { line } } });

describe("headingAt", () => {
  it("returns null with no headings or none at/above the line", () => {
    expect(headingAt(undefined, 5)).toBeNull();
    expect(headingAt([], 5)).toBeNull();
    expect(headingAt([h("Later", 6)], 5)).toBeNull();
  });

  it("includes a heading on the selection's own line (<=)", () => {
    expect(headingAt([h("Here", 5)], 5)).toBe("Here");
  });

  it("the last heading at or above the line wins", () => {
    const hs = [h("Intro", 0), h("Method", 3), h("Results", 8)];
    expect(headingAt(hs, 7)).toBe("Method");
    expect(headingAt(hs, 8)).toBe("Results");
    expect(headingAt(hs, 2)).toBe("Intro");
  });

  it("a blank nearest heading gives null (never \"\") and does not fall back to an earlier one", () => {
    expect(headingAt([h("Intro", 0), h("   ", 3)], 4)).toBeNull();
    expect(headingAt([h("", 1)], 1)).toBeNull();
  });
});
