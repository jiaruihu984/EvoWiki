// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { primaryOf } from "./end-block/model";
import { reviewFirstRow } from "./review-first";

const HINT = { node_id: "c10", name: "连通分量", page_id: "p3", page_title: "连通分量" };

describe("reviewFirstRow", () => {
  it("says the point and offers 先复习 / 跳过, each answering once", () => {
    const answer = vi.fn();
    const row = reviewFirstRow(HINT, answer);
    expect(row.textContent).toContain("建议先复习连通分量");
    const [go, skip] = [...row.querySelectorAll("button")];
    expect([go!.textContent, skip!.textContent]).toEqual(["先复习", "跳过"]);
    go!.click();
    skip!.click();
    expect(answer.mock.calls).toEqual([["review"], ["skip"]]);
  });
  it("leaves the step's own button as it is", () => {
    const step = { kind: "read", review_first: HINT } as never;
    expect(primaryOf(step).kind).toBe("next");
  });
});
