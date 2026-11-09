import { describe, expect, it, vi } from "vitest";
import { methods, newGoal, setup, view } from "./controller-test-kit";

describe("学点新的 entry and 生成第一页 (T11)", () => {
  it("openLearnNew opens the 学点新的 tab with the topic (learnNew)", () => {
    const learnNew = vi.fn();
    const { c } = setup({ learnNew });
    c.openLearnNew("贝叶斯定理");
    expect(learnNew).toHaveBeenCalledWith("贝叶斯定理");
  });

  it("learnFirstPage: GET the path when it is not the one shown, then the 下一页 chain (POST next, organize, sync, open)", async () => {
    const { c, api, sync, opened } = setup();
    await newGoal(c, "学统计推断");
    const v = c.state.path.view;
    if (v.kind !== "ready") throw new Error("no path");
    const pathId = v.value.path_id;
    await c.openPath(null);
    api.calls.length = 0;
    await c.learnFirstPage(pathId);
    expect(methods(api).slice(0, 3)).toEqual(["getPath", "pathNext", "organize"]);
    expect(sync.runs).toHaveLength(1);
    expect(opened).toHaveLength(1);
    expect(view(c).page.organized).toBe(true);
  });

  it("learnWritePage while offline: rejects with the plain line, nothing sent", async () => {
    const { c, api, conn } = setup();
    conn.state = "offline";
    await expect(c.learnWritePage("p-x", "最大似然", "我要学机器学习")).rejects.toMatchObject({ name: "PageNotWritten" });
    expect(methods(api)).toEqual([]);
  });

  it("learnFirstPage whose organize fails (503) rejects: a failed write is never reported written (U1)", async () => {
    const { c, api } = setup();
    await newGoal(c, "学统计推断");
    const v = c.state.path.view;
    if (v.kind !== "ready") throw new Error("no path");
    vi.spyOn(api, "organize").mockRejectedValue(Object.assign(new Error("503"), { status: 503 }));
    await expect(c.learnFirstPage(v.value.path_id)).rejects.toMatchObject({ name: "PageNotWritten" });
  });
});
