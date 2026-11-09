import { describe, expect, it, vi } from "vitest";
import type { ActionOut, SuggestionOut } from "./bot/bot-schema";
import { setup } from "./controller-test-kit";
import { PERSONAL_COPY } from "./personal-copy";
import { shownRules } from "./personal-rules";
import { createBar } from "./revise/overlay";
import { pageWaitActions, waitBars } from "./page-waits";
import type { TeachSpecOut } from "./schema";

const specOf = (c: ReturnType<typeof setup>["c"]): TeachSpecOut => {
  const p = c.state.prefs;
  if (p.kind !== "ready") throw new Error(p.kind);
  return p.value;
};
const writes = (s: ReturnType<typeof setup>): unknown[][] =>
  s.api.calls.filter((x) => /SpecItem$/.test(x.method)).map((x) => [x.method, ...x.args.filter((a) => a !== undefined)]);

describe("个性化: the preferences (controller/personal.ts)", () => {
  it("loadEngine reads the whole spec into prefs (rules and the pending one)", async () => {
    const s = setup();
    const id = s.api.__pendingRule("讲数学时先给能算的小例子", "你在 3 次对话里都要了能算出结果的例子");
    await s.c.loadEngine();
    const spec = specOf(s.c);
    expect(spec.items.find((i) => i.item_id === id)?.authority).toBe("pending");
    expect(spec.evidence[id]).toBe("你在 3 次对话里都要了能算出结果的例子");
  });

  it("设为我的偏好 with the text unchanged → take_over; edited → PATCH {text}; the rule is the user's and listed", async () => {
    const s = setup();
    const a = s.api.__pendingRule("讲数学时先给能算的小例子");
    const b = s.api.__pendingRule("多用图");
    await s.c.loadEngine();
    expect(await s.c.prefAdopt(a, "  讲数学时先给能算的小例子 ")).toBe(true);
    expect(await s.c.prefAdopt(b, "每个概念配一张图")).toBe(true);
    expect(writes(s)).toEqual([["takeOverSpecItem", a], ["editSpecItem", b, { text: "每个概念配一张图" }]]);
    const mine = shownRules(specOf(s.c)).filter((r) => r.own === "user").map((r) => r.item.text);
    expect(mine).toEqual(["讲数学时先给能算的小例子", "每个概念配一张图"]);
    expect(s.notices.slice(-2)).toEqual([PERSONAL_COPY.adopted, PERSONAL_COPY.adopted]);
  });

  it("不采用 → DELETE: remembered as rejected (没采用和撤回的); an empty text is refused locally", async () => {
    const s = setup();
    const a = s.api.__pendingRule("多用图");
    await s.c.loadEngine();
    expect(await s.c.prefAdopt(a, "   ")).toBe(false);
    expect(s.notices).toContain(PERSONAL_COPY.emptyPref);
    await s.c.prefReject(a);
    expect(writes(s)).toEqual([["deleteSpecItem", a]]);
    expect(specOf(s.c).decisions.map((d) => [d.rule_id, d.text, d.action])).toEqual([[a, "多用图", "rejected"]]);
    expect(s.notices[s.notices.length - 1]).toBe(PERSONAL_COPY.rejected);
  });

  it("管理偏好: 保存 (unchanged only closes), 删除 (revoked), 恢复 (added back as the user's, the decision gone)", async () => {
    const s = setup();
    await s.c.loadEngine();
    await s.c.prefRestore("先给结论");
    const items = specOf(s.c).items;
    const id = items[items.length - 1]!.item_id;
    s.c.personalView({ manage: true, edit: id });
    expect(await s.c.prefSave(id, "先给结论")).toBe(true);
    expect(s.c.state.personal.edit).toBeNull();
    s.c.personalView({ edit: id });
    s.c.prefDraft(`e:${id}`, "先给结论，再讲为什么");
    await s.c.prefSave(id, "先给结论，再讲为什么");
    expect(s.c.state.personal.edit).toBeNull();
    expect(s.c.state.personal.drafts[`e:${id}`]).toBeUndefined();
    await s.c.prefRemove(id);
    expect(specOf(s.c).decisions.map((d) => [d.text, d.action])).toEqual([["先给结论，再讲为什么", "revoked"]]);
    await s.c.prefRestore("先给结论，再讲为什么");
    expect(specOf(s.c).decisions).toEqual([]);
    expect(writes(s).map((w) => w[0])).toEqual(["addSpecItem", "editSpecItem", "deleteSpecItem", "addSpecItem"]);
    expect(s.notices.slice(-3)).toEqual([PERSONAL_COPY.saved, PERSONAL_COPY.deleted, PERSONAL_COPY.restored]);
  });

  it("a typed text is kept without a redraw; leaving 管理偏好 ends an edit", () => {
    const s = setup();
    const seen = vi.fn();
    s.c.subscribe(seen);
    s.c.prefDraft("p:x", "abc");
    expect(seen).not.toHaveBeenCalled();
    expect(s.c.state.personal.drafts["p:x"]).toBe("abc");
    s.c.personalView({ manage: true, edit: "x" });
    s.c.personalView({ manage: false });
    expect(s.c.state.personal).toMatchObject({ manage: false, edit: null });
  });

  it("offline: nothing is sent, a notice says why", async () => {
    const s = setup();
    await s.c.loadEngine();
    s.conn.emit("offline");
    await s.c.prefRestore("x");
    expect(writes(s)).toEqual([]);
    expect(s.notices.length).toBe(1);
  });
});

describe("查看 a change, 去确认, a rule's result", () => {
  it("botShowAction opens 个性化 with 活动记录 expanded and reveals the row", () => {
    const s = setup();
    s.c.botShowAction("act1");
    expect(s.c.state.tab).toBe("engine");
    expect(s.c.state.personal).toMatchObject({ manage: false, log: true });
    expect(s.revealed).toEqual(["act:act1"]);
  });

  it("a row older than yesterday opens 更早 too", async () => {
    const s = setup();
    await s.c.init();
    const rsi = s.c.state.rsi;
    if (rsi.kind !== "ready") throw new Error(rsi.kind);
    const old = rsi.value.find((c) => c.status === "personal_accepted")!;
    s.c.showChange(`rsi:${old.patch_id}`);
    expect(s.c.state.personal.older).toBe(true);
  });

  it("openManagePrefs: 个性化 · 管理偏好", () => {
    const s = setup();
    s.c.openManagePrefs();
    expect([s.c.state.tab, s.c.state.personal.manage]).toEqual(["engine", true]);
  });

  it("资料更新 · 去确认 opens the first waiting draft's page (not 个性化)", async () => {
    const s = setup();
    const card = { suggestion_id: "sg_rv", kind: "source_added", title: "t", why: "w", placement: "today", page_id: null, priority: 50, status: "pending",
      created_at: "2026-10-10T00:00:00Z", expires_at: "2099-01-01T00:00:00Z", trigger_id: null,
      action: { type: "revise_from_source", source_id: "src1", page_ids: ["p1"] } } as unknown as SuggestionOut;
    vi.spyOn(s.api, "botSuggestions").mockResolvedValue([card]);
    await s.c.refreshToday(true);
    await s.c.botStart("sg_rv");
    const [id] = s.c.state.bot.revise.sg_rv!.actionIds;
    const revise = { state: "ready" as const, page_id: "p1", run_id: "r1", detail: "", sources: [], sections: [], unchanged: 0, rebuild: false,
      cite_issues: 0, stale: false, practice: null };
    vi.spyOn(s.api, "botActions").mockResolvedValue([ledger({ action_id: id!, kind: "page_revise", scope: "page_body", revise })]);
    await s.c.refreshToday(true);
    const open = vi.spyOn(s.c, "openRevise").mockResolvedValue();
    s.c.botGoConfirm("sg_rv");
    expect(open).toHaveBeenCalledWith("p1");
    expect(s.c.state.tab).not.toBe("engine");
    s.c.dispose();
  });
});

// ---- a ledger action waiting for a yes / no: on its page, else on 知识图谱 (page-waits.ts) ----
const ledger = (o: Partial<ActionOut> & Pick<ActionOut, "action_id">): ActionOut => ({
  kind: "page_merge", scope: "delete_merge", level: "ask", status: "proposed", source: "assistant", summary: "把「方差（旧）」并进这一页", why: "两页内容重复",
  content: {}, page_id: "p1", detail: "", created_at: "2026-10-10T08:00:00Z", decided_at: null, expires_at: null, can_undo: false, diff: [], confirm: false,
  cost_usd: null, made: [], revise: null, result: {}, section_id: null, ...o });

describe("a wait on its page", () => {
  it("the page's bar: summary, ? (why), 确认合并 / 取消 → botDecide; another page's wait and one with no page are not there", async () => {
    const s = setup();
    vi.spyOn(s.api, "botActions").mockResolvedValue([ledger({ action_id: "w1" }), ledger({ action_id: "w2", page_id: "p2" }),
      ledger({ action_id: "w3", page_id: null, kind: "node_split", summary: "把「统计」拆成两个点" })]);
    await s.c.refreshToday(true);
    const decide = vi.spyOn(s.c, "botDecide").mockResolvedValue();
    const hub = { current: s.c, subscribe: (fn: () => void) => s.c.subscribe(fn) };
    const bar = createBar(() => "p1", hub, () => null);
    const bars = [...bar.el.querySelectorAll<HTMLElement>(".ew-revbar")];
    expect(bars.map((b) => b.dataset.wait)).toEqual(["w1"]);
    const b = bars[0]!;
    expect(b.getAttribute("aria-label")).toBe("待确认的建议");
    expect(b.querySelector(".ew-rb-t")?.textContent).toBe("把「方差（旧）」并进这一页");
    expect(b.querySelector(".ew-qtip")).not.toBeNull();
    expect([...b.querySelectorAll(".ew-rb-acts > button")].map((x) => x.textContent)).toEqual(["确认合并", "取消"]);
    b.querySelector<HTMLButtonElement>("[data-action=bot-approve]")!.click();
    b.querySelector<HTMLButtonElement>("[data-action=bot-reject]")!.click();
    expect(decide.mock.calls).toEqual([["w1", "approve"], ["w1", "reject"]]);
    // 知识图谱: the wait with no page
    expect(waitBars(s.c.state, null, { botDecide: vi.fn() }).map((x) => x.dataset.wait)).toEqual(["w3"]);
    expect(pageWaitActions(s.c.state, "p2").map((x) => x.action_id)).toEqual(["w2"]);
    s.c.dispose();
  });

  it("decided, the bar goes (an approved row offers only 确认)", async () => {
    const s = setup();
    const spy = vi.spyOn(s.api, "botActions").mockResolvedValue([ledger({ action_id: "w1", status: "approved" })]);
    await s.c.refreshToday(true);
    const hub = { current: s.c, subscribe: (fn: () => void) => s.c.subscribe(fn) };
    const bar = createBar(() => "p1", hub, () => null);
    expect([...bar.el.querySelectorAll(".ew-rb-acts > button")].map((x) => x.textContent)).toEqual(["确认合并"]);
    spy.mockResolvedValue([ledger({ action_id: "w1", status: "executed" })]);
    await s.c.refreshToday(true);
    bar.update();
    expect(bar.el.querySelector(".ew-revbar")).toBeNull();
    s.c.dispose();
  });
});
