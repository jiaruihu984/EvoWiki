import { describe, expect, it } from "vitest";
import { ApiError } from "../api/errors";
import { firstPage, flush, methods, setup, view } from "./controller-test-kit";
import { FakeWikiApi } from "./fake";
import type { ErrorEvent } from "./schema";
import type { ResultNote } from "./state";

const BUDGET = "今天的花费到了上限 $1.00，这次没有完成。明天 0 点恢复，也可以在设置里调高。";
const budgetNote: ResultNote = { text: BUDGET, tone: "error" };
/** A server that wrongly flags the usage-limit event as retryable: still no retry button. */
const flaggedRetryable: ErrorEvent = { code: "budget_exceeded", retryable: true, detail: BUDGET };
const since = (api: FakeWikiApi, n: number): string[] => methods(api).slice(n);

describe("usage limit (429 / budget_exceeded): the budget text where the action shows its result, no retry, no resend", () => {

  it("生成这一页: the organize stream sends budget_exceeded → status text, no retry, no GET, no resend", async () => {
    const { api, sync, c } = setup();
    api.failNext("organize", { code: "llm_failed", retryable: true, detail: "" });
    await firstPage(c);
    expect(view(c).page.organized).toBe(false);
    api.__budget(true);
    const n = api.calls.length;
    await c.organize();
    await flush();
    expect(since(api, n)).toEqual(["organize"]);
    expect(view(c).status).toEqual(budgetNote);
    expect(view(c).page.organized).toBe(false);
    expect(sync.runs).toEqual([]);
    expect(c.state.busy).toBeNull();
  });

  it("生成这一页: budget_exceeded flagged retryable still gets no retry; a thrown 429 neither", async () => {
    const { api, c } = setup();
    api.failNext("organize", flaggedRetryable);
    await firstPage(c);
    expect(view(c).status).toEqual(budgetNote);
    api.failNext("organize", new ApiError(429, BUDGET, "budget_exceeded"));
    const n = api.calls.length;
    await c.organize();
    await flush();
    expect(since(api, n)).toEqual(["organize"]);
    expect(view(c).status).toEqual(budgetNote);
  });

  it("我懂: 429 → text under the section, no retry, no GET", async () => {
    const { api, c } = setup();
    await firstPage(c);
    api.failNext("feedbackKnow", new ApiError(429, BUDGET, "budget_exceeded"));
    const n = api.calls.length;
    await c.know("s3");
    await flush();
    expect(since(api, n)).toEqual(["feedbackKnow"]);
    expect(view(c).sectionNotes.s3).toEqual(budgetNote);
    expect(c.state.busy).toBeNull();
  });

});

describe("names maps from the server", () => {
  it("catalog: its names map lands in the name book", async () => {
    class CatalogNames extends FakeWikiApi {
      override catalog(): ReturnType<FakeWikiApi["catalog"]> {
        return super.catalog().then((out) => ({ ...out, names: { ...out.names, c7: "协方差" } }));
      }
    }
    const { c } = setup({ api: new CatalogNames() });
    await c.init();
    expect(c.names.concept("c7")).toBe("协方差");
  });
});
