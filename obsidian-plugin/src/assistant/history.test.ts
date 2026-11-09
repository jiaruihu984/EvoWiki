import { describe, expect, it } from "vitest";
import { formatWhen, parseWhen } from "../ui/dates";
import { groupOf, rowSub, titleOf } from "./history";

// Local-time clock: Wed 2026-10-14 12:00 local.
const NOW = new Date(2026, 9, 14, 12, 0);
const at = (y: number, mo: number, d: number, h = 9, mi = 5): string => new Date(y, mo - 1, d, h, mi).toISOString();

describe("titleOf", () => {
  it("strips one trailing ？ ? 。 and cuts to 24 characters", () => {
    expect(titleOf("特征值为什么叫「特征」？")).toBe("特征值为什么叫「特征」");
    expect(titleOf("what is PCA?")).toBe("what is PCA");
    expect(titleOf("好的。。")).toBe("好的。");
    expect(titleOf("一二三四五六七八九十一二三四五六七八九十一二三四五六七八")).toHaveLength(24);
    expect([...titleOf("😀".repeat(30))]).toHaveLength(24);
  });
  it("is the first line: 学点新的's goal message (goal, then a 背景 line) is titled by the goal", () => {
    expect(titleOf("我要学黑壳虾\n背景：学过高中生物，想知道黑壳虾是什么、怎么养。")).toBe("我要学黑壳虾");
    expect(titleOf("  讲讲这个？\n")).toBe("讲讲这个");
  });
});

describe("formatWhen / groupOf (local calendar days)", () => {
  it("today, yesterday, earlier this year, other year", () => {
    expect(formatWhen(at(2026, 10, 14, 9, 5), NOW, "list")).toBe("今天 09:05");
    expect(formatWhen(at(2026, 10, 13, 21, 40), NOW, "list")).toBe("昨天 21:40");
    expect(formatWhen(at(2026, 10, 11), NOW, "list")).toBe("10月11日");
    expect(formatWhen(at(2025, 3, 2), NOW, "list")).toBe("2025年3月2日");
    expect([at(2026, 10, 14), at(2026, 10, 13), at(2026, 10, 12), at(2025, 1, 1)].map((i) => groupOf(i, NOW)))
      .toEqual(["today", "yesterday", "earlier", "earlier"]);
  });
  it("just after midnight is still yesterday for an evening before, by calendar day not 24 hours", () => {
    const now = new Date(2026, 9, 14, 0, 30);
    expect(groupOf(at(2026, 10, 13, 23, 50), now)).toBe("yesterday");
    expect(groupOf(at(2026, 10, 14, 0, 10), now)).toBe("today");
  });
  it("a zone-less stamp is UTC; garbage gives an empty label and 更早", () => {
    expect(parseWhen("2026-10-14T01:00:00")!.toISOString()).toBe("2026-10-14T01:00:00.000Z");
    expect(formatWhen("nope", NOW, "list")).toBe("");
    expect(groupOf("nope", NOW)).toBe("earlier");
  });
  it("rowSub is `page · time`, or only the time without a page", () => {
    const s = { session_id: "s", title: "t", page_id: null, page_title: "pca（2/4）", note_concept_id: null,
      created_at: at(2026, 10, 14), last_activity_at: at(2026, 10, 14, 15, 20), message_count: 2 };
    expect(rowSub(s, NOW)).toBe("pca（2/4） · 今天 15:20");
    expect(rowSub({ ...s, page_title: "" }, NOW)).toBe("今天 15:20");
  });
});
