/**
 * A materials draft in the chat (app.js revActCard): one line that points at the page; the decision is made in the page.
 * 草稿在页面里 · 改了 N 节 › → 已更新 N 节 · 撤销 (+ 打开这一页 on another page) / 改了 N 节的草稿 · 已取消 / 已撤销 / 有新的草稿.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cardRig, PAGE } from "../../test/card-rig";
import { OfflineError } from "../api/errors";
import type { ActionOut } from "../wiki/bot/bot-schema";
import { DraftDecisions } from "../wiki/revise/decisions";
import type { ActionEvent } from "./types";

const EV: ActionEvent = { type: "action", goal_title: null, replaces: "", page_id: null, action_id: "rv1", kind: "page_revise", title: "按资料改「主成分分析」", status: "proposed", confirm: true, can_undo: false, items: [] };
const row = (over: Partial<ActionOut> = {}, state: "writing" | "ready" | "failed" = "ready", pageId = PAGE.kind === "page" ? PAGE.pageId : "p"): ActionOut => ({
  action_id: "rv1", kind: "page_revise", scope: "page_body", level: "ask", status: "proposed", source: "assistant", summary: "按资料改「主成分分析」",
  why: "", content: {}, page_id: pageId, detail: "", created_at: "2026-10-21T08:00:00Z", decided_at: null, expires_at: null, can_undo: false,
  diff: [], confirm: true, cost_usd: null, made: [], section_id: null,
  revise: { state, page_id: pageId, run_id: "run1", detail: "", sources: [{ source_id: "s1", title: "线性代数讲义.pdf", deleted: false }],
    sections: state === "ready" ? [
      { section_id: "a", heading: "A", old_heading: "A", status: "changed", old_body: "x", new_body: "y" },
      { section_id: "b", heading: "B", old_heading: "B", status: "changed", old_body: "x", new_body: "y" },
    ] : [], unchanged: 3, rebuild: false, cite_issues: 0, stale: false, practice: null },
  result: {}, ...over,
});
const flush = async (): Promise<void> => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
const line = (rig: ReturnType<typeof cardRig>): string => rig.q(".ew-revmini")!.textContent ?? "";

afterEach(() => { document.body.replaceChildren(); });

describe("the materials draft card in the chat", () => {
  it("while written: 生成中 with the spinner; ready: the one-line button 草稿在页面里 · 改了 N 节 › that opens the page", async () => {
    const showRevise = vi.fn();
    const rig = cardRig();
    (rig.r as unknown as { actions: { showRevise?: (id: string) => void } }).actions.showRevise = showRevise;
    await rig.ask([EV]);
    expect(rig.q(".ew-revmini .ew-spin")).not.toBeNull();
    expect(line(rig)).toBe("生成中取消");
    // no 确认替换 in the chat; a draft still being written can be called off
    expect(rig.all(".ew-revmini button").map((x) => x.textContent)).toEqual(["取消"]);
    rig.ctl.syncLedger([row()]);
    const b = rig.q<HTMLButtonElement>("button.ew-propcard.ew-revmini.ew-go")!;
    expect(b.textContent).toBe("草稿在页面里 · 改了 2 节");
    expect(b.querySelector(".ew-ri svg")).not.toBeNull();
    expect(b.querySelector(".ew-rg svg")).not.toBeNull();
    b.click();
    expect(showRevise).toHaveBeenCalledWith("page_pca");
    rig.dispose();
  });

  it("while written: 取消 rejects the row (a run that never ends must not hold the page) → 已取消", async () => {
    const rig = cardRig();
    await rig.ask([EV]);
    rig.ctl.syncLedger([row({}, "writing")]);
    rig.api.actionRows.set("rv1", row({ status: "rejected", decided_at: "2026-10-21T08:05:00Z" }, "writing"));
    rig.q<HTMLButtonElement>(".ew-revmini button[data-op=reject]")!.click();
    await flush();
    expect(rig.api.actionCalls.filter((c) => c.op === "reject")).toEqual([{ op: "reject", id: "rv1" }]);
    expect(line(rig)).toBe("改了 0 节的草稿已取消");
    rig.dispose();
  });

  it("confirmed (in the page): 已更新 N 节 + 撤销 (打开这一页 only on another page); 撤销 → undoAction, the note follows", async () => {
    const rig = cardRig();
    const edited = vi.fn();
    (rig.ctl as unknown as { deps: { pageEdited?: unknown } }).deps.pageEdited = edited;
    await rig.ask([EV]);
    rig.ctl.syncLedger([row({ status: "executed", can_undo: true, decided_at: "2026-10-21T08:05:00Z" })]);
    expect(line(rig)).toBe("已更新 2 节撤销");
    expect(rig.all(".ew-revmini button").map((x) => x.textContent)).toEqual(["撤销"]);
    // on another page: 打开这一页 too
    rig.ctl.show({ kind: "page", pageId: "other", title: "别的页" });
    rig.ctl.show(PAGE);
    rig.api.actionRows.set("rv1", row({ status: "executed", can_undo: true }));
    rig.all<HTMLButtonElement>(".ew-revmini button").find((x) => x.textContent === "撤销")!.click();
    await flush();
    expect(rig.api.actionCalls.filter((c) => c.op === "undo")).toEqual([{ op: "undo", id: "rv1" }]);
    expect(edited).toHaveBeenCalledWith("page_pca", []);
    expect(line(rig)).toBe("改了 2 节的草稿已撤销");
    rig.dispose();
  });

  it("打开这一页 shows when the assistant is on another page", async () => {
    const rig = cardRig();
    await rig.ask([EV]);
    rig.ctl.syncLedger([row({ status: "executed", can_undo: true, decided_at: "2026-10-21T08:05:00Z" }, "ready", "page_other")]);
    expect(rig.all(".ew-revmini button").map((x) => x.textContent)).toEqual(["打开这一页", "撤销"]);
    rig.all<HTMLButtonElement>(".ew-revmini button")[0]!.click();
    expect(rig.spies.showPage).toHaveBeenCalledWith("page_other");
    rig.dispose();
  });

  it("cancelled: 改了 N 节的草稿 · 已取消; a newer draft of the page: 有新的草稿 (no buttons)", async () => {
    const rig = cardRig();
    await rig.ask([EV]);
    rig.ctl.syncLedger([row({ status: "rejected", decided_at: "2026-10-21T08:05:00Z" })]);
    expect(line(rig)).toBe("改了 2 节的草稿已取消");
    expect(rig.q(".ew-revmini.ew-done")).not.toBeNull();
    expect(rig.all(".ew-revmini button")).toHaveLength(0);
    const newer = row({ action_id: "rv2", created_at: "2026-10-21T09:00:00Z" });
    rig.ctl.syncLedger([row({ status: "rejected", decided_at: "2026-10-21T08:05:00Z" }), newer]);
    expect(line(rig)).toBe("改了 2 节的草稿有新的草稿");
    rig.dispose();
  });
});

describe("the practice draft card in the chat (app.js actCard a.prac)", () => {
  const PEV: ActionEvent = { type: "action", goal_title: null, replaces: "", page_id: null, action_id: "pr1", kind: "page_practice", title: "给「主成分分析」加交互", status: "proposed", confirm: true, can_undo: false, items: [] };
  const prow = (over: Partial<ActionOut> = {}): ActionOut => row({ action_id: "pr1", kind: "page_practice", summary: "给「主成分分析」加交互",
    revise: { ...row().revise!, run_id: null, sources: [], sections: [row().revise!.sections[0]!], practice: "interactive" }, ...over });

  it("keeps 确认替换 / 取消 and, once the ledger shows the draft in the page, 在页面里看 opens it; 确认替换 → the note follows", async () => {
    const showRevise = vi.fn();
    const rig = cardRig();
    (rig.r as unknown as { actions: { showRevise?: (id: string) => void } }).actions.showRevise = showRevise;
    const edited = vi.fn();
    (rig.ctl as unknown as { deps: { pageEdited?: unknown } }).deps.pageEdited = edited;
    await rig.ask([PEV]);
    expect(rig.q(".ew-propcard b")!.textContent).toBe("建议修改「主成分分析」");
    expect(rig.all(".ew-propcard button").map((x) => x.textContent)).toEqual(["确认替换", "取消"]);
    rig.ctl.syncLedger([prow()]);
    expect(rig.all(".ew-propcard button").map((x) => x.textContent)).toEqual(["在页面里看", "确认替换", "取消"]);
    // at the end of the 加交互 line, not in the button row (which keeps 确认替换 / 取消 / 请重试 on one line)
    expect(rig.q(".ew-propcard .ew-subrow")!.textContent).toBe("加交互在页面里看");
    expect(rig.q(".ew-propcard .ew-row [data-op=see]")).toBeNull();
    rig.q<HTMLButtonElement>(".ew-propcard button[data-op=see]")!.click();
    expect(showRevise).toHaveBeenCalledWith("page_pca");
    rig.api.actionRows.set("pr1", prow({ status: "executed", can_undo: true, revise: null }));
    rig.all<HTMLButtonElement>(".ew-propcard button").find((x) => x.textContent === "确认替换")!.click();
    await flush();
    expect(rig.api.actionCalls.filter((c) => c.op === "approve")).toEqual([{ op: "approve", id: "pr1" }]);
    expect(edited).toHaveBeenCalledWith("page_pca", []);
    rig.dispose();
  });

  // one decision in two places (the page's bar and this card): wiki/revise/decisions.ts
  const btns = (rig: ReturnType<typeof cardRig>): string[] =>
    rig.all<HTMLButtonElement>(".ew-propcard button").map((x) => (x.textContent ?? "") + (x.disabled ? "(off)" : ""));

  it("confirmed here while the server is down: no sentence, only a quiet 请重试 after the buttons; the bar is told; 请重试 sends it again", async () => {
    const decisions = new DraftDecisions();
    const rig = cardRig({ decisions });
    await rig.ask([PEV]);
    rig.ctl.syncLedger([prow()]);
    rig.api.actionReplies.push({ throws: new OfflineError() });
    rig.all<HTMLButtonElement>(".ew-propcard button").find((x) => x.textContent === "确认替换")!.click();
    await flush();
    expect(btns(rig)).toEqual(["在页面里看", "确认替换", "取消", "请重试"]);
    expect(rig.q(".ew-propcard")!.textContent).not.toContain("没发出去");
    expect(decisions.get("pr1").failed).toBe("approve");
    rig.api.actionRows.set("pr1", prow({ status: "executed", can_undo: true, revise: null }));
    rig.q<HTMLButtonElement>(".ew-propcard .ew-retry")!.click();
    await flush();
    expect(rig.api.actionCalls.filter((c) => c.op === "approve")).toHaveLength(2);
    expect(rig.all(".ew-propcard button").map((x) => x.textContent)).not.toContain("确认替换");
    expect(decisions.get("pr1").settled?.status).toBe("executed");
    rig.dispose();
  });

  it("decided on the page's bar: the card waits with it, then turns done at once (no stale buttons, no second request)", async () => {
    const decisions = new DraftDecisions();
    const rig = cardRig({ decisions });
    await rig.ask([PEV]);
    rig.ctl.syncLedger([prow()]);
    expect(decisions.start("pr1", "approve")).toBe(true);
    expect(btns(rig)).toEqual(["在页面里看", "确认替换(off)", "取消(off)"]);
    decisions.settle("pr1", prow({ status: "executed", can_undo: true, revise: null }));
    expect(rig.all(".ew-propcard button").map((x) => x.textContent)).not.toContain("确认替换");
    expect(rig.q(".ew-propcard")!.textContent).toContain("撤销");
    expect(rig.api.actionCalls.filter((c) => c.op === "approve" || c.op === "reject")).toEqual([]);
    rig.dispose();
  });

  it("the bar's decision did not go through: the card shows 请重试 too, and clicking it sends that decision from here", async () => {
    const decisions = new DraftDecisions();
    const rig = cardRig({ decisions });
    await rig.ask([PEV]);
    rig.ctl.syncLedger([prow()]);
    decisions.start("pr1", "reject");
    decisions.fail("pr1", "reject");
    expect(btns(rig)).toEqual(["在页面里看", "确认替换", "取消", "请重试"]);
    rig.q<HTMLButtonElement>(".ew-propcard .ew-retry")!.click();
    await flush();
    expect(rig.api.actionCalls.filter((c) => c.op === "reject")).toEqual([{ op: "reject", id: "pr1" }]);
    expect(rig.q(".ew-propcard")!.textContent).toContain("未做更改");
    rig.dispose();
  });

  it("its page changed where it goes (stale): no 确认替换, no sentence, 取消 + a quiet 重试 that writes it again; then it waits as before", async () => {
    const decisions = new DraftDecisions();
    const rig = cardRig({ decisions });
    await rig.ask([PEV]);
    rig.ctl.syncLedger([prow({ revise: { ...prow().revise!, stale: true } })]);
    expect(btns(rig)).toEqual(["在页面里看", "取消", "重试"]);
    expect(rig.q(".ew-propcard")!.textContent).not.toContain("改过");
    rig.api.actionRows.set("pr1", prow({ revise: { ...prow().revise!, stale: true } }));
    rig.q<HTMLButtonElement>(".ew-propcard [data-op=redraft]")!.click();
    await flush();
    expect(rig.api.actionCalls.filter((c) => c.op === "redraft")).toEqual([{ op: "redraft", id: "pr1" }]);
    expect(btns(rig)).toEqual(["在页面里看", "确认替换", "取消"]);
    expect(decisions.get("pr1")).toEqual({ busy: null, failed: null, settled: null });
    rig.dispose();
  });

  it("its 确认 came after the page moved (failed, stale): only a quiet 重试, which brings it back waiting", async () => {
    const rig = cardRig({ decisions: new DraftDecisions() });
    await rig.ask([PEV]);
    rig.ctl.syncLedger([prow({ status: "failed", detail: "页面在提议之后改过了，这条修改已作废，不会覆盖新版本" })]);
    expect(btns(rig)).toEqual(["重试"]);
    expect(rig.q(".ew-propcard")!.textContent).not.toContain("作废");
    rig.api.actionRows.set("pr1", prow({ status: "failed" }));
    rig.q<HTMLButtonElement>(".ew-propcard [data-op=redraft]")!.click();
    await flush();
    expect(rig.all(".ew-propcard button").map((x) => x.textContent)).toContain("确认替换");
    rig.dispose();
  });
});
