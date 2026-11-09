import { describe, expect, it } from "vitest";
import { formatWhen, localDayKey } from "./dates";

describe("dates", () => {
  it("localDayKey is the local day, zero-padded", () => {
    expect(localDayKey(new Date(2026, 0, 5, 23, 59))).toBe("2026-01-05");
    expect(localDayKey(new Date(2026, 10, 25, 0, 0))).toBe("2026-11-25");
  });
  it("the stamp form (Bot runs) reads a zone-less server time as UTC, as the list form does", () => {
    const utc = "2026-10-14T01:00:00";
    const local = new Date(Date.UTC(2026, 9, 14, 1, 0));
    const now = new Date(local.getFullYear(), local.getMonth(), local.getDate(), 23, 0);
    expect(formatWhen(utc, now, "stamp")).toBe(`今天 ${local.getHours()}:${String(local.getMinutes()).padStart(2, "0")}`);
    expect(formatWhen(utc, now, "list")).toBe(`今天 ${String(local.getHours()).padStart(2, "0")}:00`);
    expect(formatWhen("nope", now, "stamp")).toBe("");
  });
});
