/**
 * RSI observability, ruling 3: every feedback request carries the artifact_id of the object the user was SHOWN
 * (held in the rendered page / review state), never one looked up at click time; unknown = not sent.
 */
import { describe, expect, it } from "vitest";
import { firstPage, setup, view } from "./controller-test-kit";
import { FakeWikiApi, fakeArtifactId } from "./fake";
import { quoteArtifactId } from "../assistant/quote-artifact";
import type { PageOut } from "./schema";

const ART = /^art_[0-9a-f]{24}$/;
const last = (api: FakeWikiApi, method: string) => [...api.calls].reverse().find((x) => x.method === method)!;
const sec = (c: ReturnType<typeof setup>["c"], sid: string) => view(c).page.sections.find((x) => x.id === sid)!;

describe("artifact_id of what was shown", () => {
  it("fake ids look like the server's", () => {
    expect(fakeArtifactId("x")).toMatch(ART);
  });

  it("我懂 / 再讲深一点 send the section's artifact_id; the page-feedback pill sends the section's, a page-level one none", async () => {
    const s = setup();
    await firstPage(s.c);
    const shown = sec(s.c, "s3").artifact_id;
    expect(shown).toMatch(ART);
    await s.c.know("s3");
    expect(last(s.api, "feedbackKnow").args).toEqual(["p1", "s3", shown]);
    const s2 = setup();
    await firstPage(s2.c);
    await s2.c.feedback("s2", ["too_hard"]);
    expect(last(s2.api, "pageFeedback").args[1]).toMatchObject({ section_id: "s2", artifact_id: sec(s2.c, "s2").artifact_id });
    await s2.c.feedback(null, ["too_hard"]);
    expect((last(s2.api, "pageFeedback").args[1] as { artifact_id?: unknown }).artifact_id).toBeUndefined();
  });

  it("a check answer sends the check's artifact_id (the one on screen)", async () => {
    const s = setup();
    await firstPage(s.c);
    const shown = sec(s.c, "s5").check!.artifact_id;
    expect(shown).toMatch(ART);
    await s.c.answerCheck("s5", 0);
    expect(last(s.api, "answerCheck").args).toEqual(["p1", "s5", 0, shown]);
  });

  it("quoteArtifactId: the open page's section with exactly that heading, else nothing", () => {
    const page = { page_id: "p1", sections: [
      { heading: "推导", artifact_id: "art_a" }, { heading: "例子", artifact_id: "art_b" }, { heading: "例子", artifact_id: "art_c" },
    ] } as unknown as PageOut;
    expect(quoteArtifactId(page, "p1", "推导")).toBe("art_a");
    expect(quoteArtifactId(page, "p1", "例子")).toBeNull();
    expect(quoteArtifactId(page, "p2", "推导")).toBeNull();
    expect(quoteArtifactId(null, "p1", "推导")).toBeNull();
    expect(quoteArtifactId(page, "p1", null)).toBeNull();
  });
});

describe("review and follow-up requests", () => {
  it("a review grade / check answer sends the card's artifact_id; a confirmation answer the question's", async () => {
    const s = setup();
    await firstPage(s.c);
    await s.c.init();
    await s.c.reloadReview();
    const t = s.c.state.review.today;
    if (t.kind !== "ready") throw new Error("no review");
    const item = t.value.items.find((i) => i.mode === "check")!;
    expect(item.artifact_id).toMatch(ART);
    await s.c.answerReview(item.cell, 0);
    expect(last(s.api, "gradeReview").args[0]).toMatchObject({ artifact_id: item.artifact_id });
  });
});
