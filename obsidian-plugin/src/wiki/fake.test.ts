import { describe, expect, it } from "vitest";
import { AbortedError, ApiError, OfflineError } from "../api/errors";
import { engineParamNote } from "./engine-copy";
import { FakeWikiApi } from "./fake";
import { DEMO_ASSET, ENGINE_PARAMS, ENGINE_PROMPTS, RSI_CHANGES } from "./fixtures";
import type { BotSettings } from "./bot/bot-schema";
import type { OrganizeEvent } from "./schema";

const GOAL = "  学会 z-score 标准化  ";

async function draftAndPage(api: FakeWikiApi, answers: number[] = [-1, 0, 1], background = ""): Promise<string> {
  const d = api.__goal(GOAL, background);
  return (await api.answerDraft(d.draft_id ?? "", { answers, skip: false })).page_id ?? "";
}
async function organize(api: FakeWikiApi, pid: string): Promise<OrganizeEvent[]> {
  const evs: OrganizeEvent[] = [];
  await api.organize(pid, (e) => evs.push(e));
  return evs;
}
const last = <T>(xs: T[]): T | undefined => xs[xs.length - 1];
const status = async (p: Promise<unknown>): Promise<number> => {
  try { await p; } catch (e) { if (e instanceof ApiError) return e.status; throw e; }
  return 0;
};

const BUDGET = "今天的花费到了上限 $1.00，这次没有完成。明天 0 点恢复，也可以在设置里调高。";

describe("abort and delay", () => {
  it("abort rejects with AbortedError and stops events; the page stays unorganized", async () => {
    const api = new FakeWikiApi();
    const pid = await draftAndPage(api);
    const ac = new AbortController();
    const evs: string[] = [];
    await expect(api.organize(pid, (e) => { evs.push(e.event); if (evs.length === 3) ac.abort(); }, { signal: ac.signal }))
      .rejects.toBeInstanceOf(AbortedError);
    expect(evs).toEqual(["outline", "writer", "section_delta"]);
    expect((await api.openPage(pid)).organized).toBe(false);
  });

  it("delayMs paces events and abort during a delay rejects", async () => {
    const api = new FakeWikiApi({ delayMs: 5 });
    const pid = await draftAndPage(api);
    const ac = new AbortController();
    const evs: string[] = [];
    const p = api.organize(pid, (e) => evs.push(e.event), { signal: ac.signal });
    setTimeout(() => ac.abort(), 12);
    await expect(p).rejects.toBeInstanceOf(AbortedError);
    expect(evs.length).toBeGreaterThan(0);
    expect(evs.length).toBeLessThan(10);
    const full: string[] = [];
    await api.organize(pid, (e) => full.push(e.event));
    expect(last(full)).toBe("done");
  });

  it("an already-aborted signal rejects before any event", async () => {
    const api = new FakeWikiApi();
    const pid = await draftAndPage(api);
    const ac = new AbortController();
    ac.abort();
    const evs: string[] = [];
    await expect(api.organize(pid, (e) => evs.push(e.event), { signal: ac.signal })).rejects.toBeInstanceOf(AbortedError);
    expect(evs).toEqual([]);
  });
});

describe("calls log and injected failures", () => {
  it("records every call in order with its arguments", async () => {
    const api = new FakeWikiApi();
    await api.catalog();
    await api.kGrid();
    await status(api.openPage("nope"));
    expect(api.calls).toEqual([
      { method: "catalog", args: [] }, { method: "kGrid", args: [] }, { method: "openPage", args: ["nope"] }]);
  });

  it("ApiError / OfflineError reject once (call still recorded), then normal", async () => {
    const api = new FakeWikiApi({ failNext: { catalog: new ApiError(503, "x") } });
    await expect(api.catalog()).rejects.toBeInstanceOf(ApiError);
    expect((await api.catalog()).learning).toEqual([]);
    api.failNext("kGrid", new OfflineError());
    await expect(api.kGrid()).rejects.toBeInstanceOf(OfflineError);
    await expect(api.kGrid()).resolves.toBeTruthy();
    expect(api.calls.map((c) => c.method)).toEqual(["catalog", "catalog", "kGrid", "kGrid"]);
  });

  it("an ErrorEvent makes a stream emit error and resolve, without changing state", async () => {
    const api = new FakeWikiApi();
    const pid = await draftAndPage(api);
    const err = { code: "llm_unavailable" as const, retryable: true, detail: "" };
    api.failNext("organize", err);
    expect(await organize(api, pid)).toEqual([{ event: "error", data: err }]);
    expect((await api.openPage(pid)).organized).toBe(false);
    await organize(api, pid);
    api.failNext("organize", new OfflineError());
    await expect(api.organize(pid, () => undefined)).rejects.toBeInstanceOf(OfflineError);
  });

  it("__budget: writes reject with 429 (nothing changes), streams send budget_exceeded, reads still work", async () => {
    const api = new FakeWikiApi();
    const pid = await draftAndPage(api);
    await organize(api, pid);
    const before = await api.openPage(pid);
    api.__budget(true);
    for (const p of [api.answerDraft("d1", { answers: null, skip: true }), api.feedbackKnow(pid, "s3"), api.answerCheck(pid, "s5", 0),
      api.done(pid), api.declare("c1.term", "know"), api.putSettings({ review_per_day: 3 }),
      api.addGuideline({ text: "多举例" }), api.fold(pid, "s3", true), api.pin(pid, "s1", true)]) {
      await expect(p).rejects.toEqual(new ApiError(429, BUDGET));
    }
    const budget = { event: "error", data: { code: "budget_exceeded", retryable: false, detail: BUDGET } };
    expect(await organize(api, pid)).toEqual([budget]);
    expect(await api.openPage(pid)).toEqual(before);
    await api.catalog();
    await api.kGrid();
    await api.getSettings();
    await api.guidelines();
    expect((await api.getSettings()).review_per_day).toBe(10);
    api.__budget(false);
    expect((await api.feedbackKnow(pid, "s3")).change.folded).toEqual(["s3"]);
  });

  it("an ErrorEvent for a JSON method is refused", () => {
    const api = new FakeWikiApi();
    expect(() => api.failNext("catalog", { code: "invalid", retryable: false, detail: "" })).toThrow();
    expect(() => new FakeWikiApi({ failNext: { kGrid: { code: "invalid", retryable: false, detail: "" } } })).toThrow();
  });
});

/** p1 from the first goal (answers -1, 0, 1), organized. */
async function organizedP1(opts: ConstructorParameters<typeof FakeWikiApi>[0] = {}): Promise<FakeWikiApi> {
  const api = new FakeWikiApi(opts);
  await organize(api, await draftAndPage(api));
  return api;
}

/** [status, detail] of a rejected call (null when it resolved). */
const failure = async (p: Promise<unknown>): Promise<[number, string] | null> => {
  try { await p; } catch (e) { if (e instanceof ApiError) return [e.status, e.detail]; throw e; }
  return null;
};


describe("RSI changes", () => {
  const NOW = new Date("2026-10-09T10:00:00Z");

  it("lists every patch newest first (candidate / gate_failed included for the plugin's filter)", async () => {
    const api = new FakeWikiApi();
    const rows = await api.rsiChanges();
    expect(rows).toEqual([...RSI_CHANGES]);
    expect(rows.map((c) => [c.track, c.layer, c.status])).toEqual([["B", 2, "personal_accepted"], ["B", 0, "rejected"],
      ["B", 0, "personal_accepted"], ["A", 0, "personal_accepted"], ["B", 0, "rolled_back"], ["A", 0, "candidate"], ["B", 0, "gate_failed"]]);
    expect(rows[1]?.before).not.toBe("");
    expect(rows[1]?.signals).toHaveLength(2);
  });

  it("a written feedback adds no change row (no RSI round runs for one feedback: RSI-B retired)", async () => {
    const api = await organizedP1({ now: () => NOW });
    const before = await api.rsiChanges();
    const v = (await api.teachSpec()).version;
    const out = await api.pageFeedback("p1", { chips: ["too_shallow"], text: "再深一点讲", section_id: null });
    expect(out.rsi).toBe("queued");
    expect(await api.rsiChanges()).toEqual(before);
    expect((await api.teachSpec()).version).toBe(v);
  });

  it("undo only personal_accepted (409); a layer-2 change only while it can be undone (409); unknown 404", async () => {
    const api = new FakeWikiApi();
    for (const id of ["pb4", "pb2", "pa1", "pb1"]) expect((await failure(api.undoPatch(id)))?.[0]).toBe(409);
    expect(await failure(api.undoPatch("x"))).toEqual([404, "no patch x"]);
    expect(await api.rsiChanges()).toEqual([...RSI_CHANGES]);
    api.__setChanges([{ ...RSI_CHANGES[0]!, can_undo: false }]);
    expect(await failure(api.undoPatch("pm1"))).toEqual([409, "method change pm1 is not the latest of its kind"]);
    api.__setChanges([...RSI_CHANGES]);
    expect(await api.undoPatch("pm1")).toMatchObject({ status: "rolled_back", can_undo: false });
    expect((await api.teachSpec()).version).toBe(1);
  });

  it("undo of a track-A change only changes its status; __setChanges replaces the list", async () => {
    const api = new FakeWikiApi();
    expect(await api.undoPatch("pa2")).toMatchObject({ status: "rolled_back", can_undo: false });
    expect((await api.teachSpec()).version).toBe(1);
    const one = { ...RSI_CHANGES[0]!, patch_id: "only" };
    api.__setChanges([one]);
    expect(await api.rsiChanges()).toEqual([one]);
  });
});

describe("engine (the 参数 tab)", () => {
  it("returns the demo's ENGINE rows verbatim, in order, as fresh copies", async () => {
    const api = new FakeWikiApi();
    const out = await api.engine();
    expect(out.prompts.map((p) => p.step)).toEqual(["拆目标和摸底", "列大纲", "写每一段", "出确认题", "理解你的反馈", "复习卡"]);
    expect(out.prompts).toEqual([...ENGINE_PROMPTS]);
    expect(out.prompts[2]).toMatchObject({ ver: "deep@v8", model: "Claude Sonnet" });
    // the demo's notes, except 「默认 · 等确认题结果再调」 (no structured form: those rows are default → 「默认」)
    expect(out.params.map((p) => [p.label, p.value, engineParamNote(p)])).toEqual([
      ["测验结果权重", "1.0", "默认"],
      ["知识检查权重", "1.0", "默认"],
      ["自我评估权重", "0.6", "默认"],
      ["学习反馈权重", "0.4", "默认"],
      ["提问行为权重", "0.3", "默认"],
      ["复习表现权重", "1.0", "默认"],
      ["「粗心失误」反馈可信度", "0.3", "默认"],
      ["「已掌握」反馈可信度", "0.3", "默认"],
      ["记忆保持系数", "× 1.0", "10月9日调过 · 因为你隔 3 天复习大多还记得"],
      ["已掌握时的答错概率", "0.1", "默认"],
      ["猜对概率系数", "× 1.0", "默认"],
      ["关联知识推断强度", "1.0", "默认"],
      ["初始掌握概率", "0.3", "默认"],
    ]);
    out.prompts[0]!.text = "changed";
    out.params.pop();
    expect(await api.engine()).toEqual({ prompts: [...ENGINE_PROMPTS], params: [...ENGINE_PARAMS] });
  });

  it("is a read: works under the usage limit, and fails like any call with failNext", async () => {
    const api = new FakeWikiApi();
    api.__budget(true);
    expect((await api.engine()).prompts).toHaveLength(6);
    api.__budget(false);
    api.failNext("engine", new ApiError(500, "boom"));
    await expect(api.engine()).rejects.toEqual(new ApiError(500, "boom"));
    expect((await api.engine()).params).toHaveLength(13);
  });
});


describe("the new calls under the usage limit", () => {
  it("the reads and the non-LLM writes work as usual", async () => {
    const api = await organizedP1();
    api.__budget(true);
    await api.rsiChanges();
    await api.assetMeta(DEMO_ASSET.asset_id);
    await api.assetHtml(DEMO_ASSET.asset_id);
    expect((await api.pageFeedback("p1", { chips: ["too_hard"], text: "", section_id: null })).understood).toBe(false);
    expect((await api.handEdit("p1", { sections: [], removed: [], summary: "x" })).edit_id).toBe("he0");
    expect((await api.undoPatch("pa2")).status).toBe("rolled_back");
    let spec = await api.addSpecItem({ text: "x", scope: "any", domain: null });
    spec = await api.editSpecItem("d01", { text: "y" });
    spec = await api.deleteSpecItem("d02");
    spec = await api.addExemplar({ kind: "wiki_page", ref: "p1" });
    spec = await api.deleteExemplar(spec.exemplars[0]?.exemplar_id ?? "");
    expect(spec.version).toBe(6);
  });
});


describe("fresh copies", () => {
  it("returned values are copies: mutating them does not change the next read", async () => {
    const api = await organizedP1();
    const undone = await api.undoPatch("pa2");
    undone.status = "rejected";
    undone.signals.push("x");
    const rows = await api.rsiChanges();
    const pa2 = rows.find((c) => c.patch_id === "pa2");
    expect(pa2).toMatchObject({ status: "rolled_back" });
    expect(pa2?.signals).toHaveLength(0);
    rows.find((c) => c.patch_id === "pm1")!.summary = "改了";
    rows.splice(0, rows.length);
    expect((await api.rsiChanges()).find((c) => c.patch_id === "pm1")).toMatchObject({ summary: RSI_CHANGES[0]!.summary });
    const spec = await api.teachSpec();
    spec.items[0]!.text = "改了";
    spec.items.pop();
    spec.evidence.x = "y";
    const again = await api.teachSpec();
    expect([again.items[0]?.text, again.items.length, again.evidence]).toEqual(["先用一两句人话说清「这是什么、解决什么问题」，再展开。", 5, {}]);
    const meta = await api.assetMeta(DEMO_ASSET.asset_id);
    meta.check.console_errors.push("x");
    meta.height = 1;
    expect(await api.assetMeta(DEMO_ASSET.asset_id)).toEqual(DEMO_ASSET);
    await api.pageFeedback("p1", { chips: ["too_hard"], text: "", section_id: null });
    api.__signals()[0]!.chips.push("unclear");
    expect(api.__signals()[0]?.chips).toEqual(["too_hard"]);
  });
});

describe("Bot API", () => {
  it("lists today's cards (pending, then shown) and only the asked page's chips", async () => {
    const api = new FakeWikiApi();
    const today = await api.botSuggestions("today");
    expect(today.map((s) => s.kind)).toEqual(["review_due", "wrong_answers"]);
    expect(today.every((s) => s.placement === "today")).toBe(true);
    expect(today[1]).toMatchObject({ title: "投影长度为什么要除以 |b|", action: { type: "open_page" } });
    expect((await api.botSuggestions("today")).every((s) => s.status === "shown")).toBe(true);
    const chips = await api.botSuggestions("page", "p2");
    expect(chips.length).toBeGreaterThan(0);
    expect(chips.every((s) => s.page_id === "p2" && s.kind === "chat" && s.action.type === "send_to_assistant")).toBe(true);
    expect(await api.botSuggestions("page", "nope")).toEqual([]);
    expect((await api.botSuggestions("page", "p1")).every((s) => s.page_id === "p1")).toBe(true);
  });

  it("not_interested writes an undoable preference action and a G-track change row; undo brings the card back", async () => {
    const api = new FakeWikiApi();
    expect(await api.botBadge()).toEqual({ today_new: 2, pending_actions: 1 });
    const [card] = await api.botSuggestions("today");
    expect((await api.botBadge()).today_new).toBe(2);   // listed (shown) but unreacted still counts
    const out = await api.botReact(card?.suggestion_id ?? "", "not_interested");
    expect(out.status).toBe("not_interested");
    const id = out.preference_action_id ?? "";
    expect(id).toMatch(/^act_/);
    const act = (await api.botActions("executed")).find((a) => a.action_id === id);
    expect(act).toMatchObject({ kind: "preference", scope: "preference", source: "bot", can_undo: true, status: "executed",
      why: "你点了不感兴趣" });
    expect(act?.summary).toBe(`以后少提醒「${card?.title}」`);
    const row = (await api.rsiChanges()).find((c) => c.patch_id === id);
    expect(row).toMatchObject({ track: "G", status: "personal_accepted", can_undo: true });
    expect(await status(api.botReact(card?.suggestion_id ?? "", "accepted"))).toBe(409);
    expect(row?.target).toBe("知识库");
    const undone = await api.botDecide(id, "undo");
    expect(undone).toMatchObject({ status: "undone", can_undo: false });
    expect((await api.botSuggestions("today")).map((s) => s.suggestion_id)).toContain(card?.suggestion_id);
    expect((await api.rsiChanges()).find((c) => c.patch_id === id)?.status).toBe("rolled_back");
  });

  it("approve / reject / undo follow the ledger states; a second approve and an unknown id fail", async () => {
    const api = new FakeWikiApi();
    const [proposed] = await api.botActions("proposed");
    expect(proposed).toMatchObject({ kind: "page_merge", status: "proposed", can_undo: false });
    expect(proposed?.diff.length).toBeGreaterThan(0);
    const id = proposed?.action_id ?? "";
    expect((await api.botDecide(id, "approve")).status).toBe("executed");
    expect((await api.botDecide(id, "approve")).status).toBe("executed");   // actions.py: the same op again returns the row
    await expect(api.botDecide(id, "reject")).rejects.toMatchObject({ status: 409, detail: "动作已经是 executed，不能拒绝" });
    expect(await status(api.botDecide("nope", "approve"))).toBe(404);
    expect((await api.botDecide(id, "undo")).status).toBe("undone");   // an ask row goes to its executor, which undoes it
    const edit = (await api.botActions("executed")).find((a) => a.kind === "assistant_edit" && a.can_undo);
    expect(edit?.diff.length).toBeGreaterThan(0);
    expect((await api.botDecide(edit?.action_id ?? "", "undo")).status).toBe("undone");
    expect(await status(api.botDecide(edit?.action_id ?? "", "undo"))).toBe(409);
  });

  it("can_undo is one rule: executed only; assistant_edit / page_merge yes, preference yes, rsi mirrors never; stale approved rows count as pending", async () => {
    let t = Date.parse("2026-10-15T00:00:00Z");
    const api = new FakeWikiApi({ now: () => new Date(t) });
    const [merge] = await api.botActions("proposed");
    expect(merge?.can_undo).toBe(false);
    const done = await api.botDecide(merge?.action_id ?? "", "approve");
    expect(done).toMatchObject({ status: "executed", can_undo: true });
    expect((await api.rsiChanges()).find((c) => c.patch_id === done.action_id)?.can_undo).toBe(true);
    await api.botDecide(done.action_id, "undo");
    expect((await api.botActions("undone")).find((a) => a.action_id === done.action_id)?.can_undo).toBe(false);
    const api2 = new FakeWikiApi({ now: () => new Date(t), bot: { actions: [
      { ...merge!, action_id: "rsi1", kind: "rsi_patch", status: "executed", can_undo: false },
      { ...merge!, action_id: "ap", kind: "assistant_edit", scope: "page_body", status: "approved", decided_at: "2026-10-14T23:59:00Z" }] } });
    expect((await api2.botBadge()).pending_actions).toBe(0);   // approved for 1 minute: still executing
    t += 90_000;
    expect((await api2.botBadge()).pending_actions).toBe(1);
    expect((await api2.botActions("proposed")).map((a) => a.action_id)).toEqual(["ap"]);
    expect(await api2.botDecide("ap", "approve")).toMatchObject({ status: "executed", can_undo: true });
  });

  it("reject marks a proposal rejected", async () => {
    const api = new FakeWikiApi();
    const [p] = await api.botActions("proposed");
    expect((await api.botDecide(p?.action_id ?? "", "reject")).status).toBe("rejected");
    expect((await api.botDecide(p?.action_id ?? "", "reject")).status).toBe("rejected");   // same op again: the row
    expect(await status(api.botDecide(p?.action_id ?? "", "approve"))).toBe(409);
    expect(await api.botActions("proposed")).toEqual([]);
  });

  it("accepted keeps the card listed; done only follows accepted; a repeat is a 409; an unknown id a 404", async () => {
    const api = new FakeWikiApi();
    const [a, b] = await api.botSuggestions("today");
    const id = a?.suggestion_id ?? "";
    await expect(api.botReact(id, "done")).rejects.toMatchObject({ status: 409 });   // done before accepted
    expect((await api.botReact(id, "accepted")).status).toBe("accepted");
    expect((await api.botSuggestions("today")).map((s) => [s.suggestion_id, s.status])).toEqual([[id, "accepted"], [b?.suggestion_id, "shown"]]);
    expect(await status(api.botReact(id, "accepted"))).toBe(409);
    expect(await status(api.botReact(id, "not_interested"))).toBe(409);
    expect((await api.botReact(id, "done")).status).toBe("done");
    expect((await api.botSuggestions("today")).map((s) => s.suggestion_id)).toEqual([b?.suggestion_id]);
    expect(await status(api.botReact(id, "done"))).toBe(409);
    await expect(api.botReact("nope", "accepted")).rejects.toMatchObject({ status: 404, detail: "unknown suggestion 'nope'" });
  });

  it("a card whose day is over takes no reaction (409)", async () => {
    let t = Date.parse("2026-10-15T00:00:00Z");
    const api = new FakeWikiApi({ now: () => new Date(t), bot: { suggestions: [{ ...(await new FakeWikiApi().botSuggestions("today"))[0]!, expires_at: "2026-10-15T16:00:00Z" }] } });
    const [a] = await api.botSuggestions("today");
    t = Date.parse("2026-10-15T16:00:01Z");
    expect(await status(api.botReact(a?.suggestion_id ?? "", "accepted"))).toBe(409);
    expect(await api.botSuggestions("today")).toEqual([]);
  });

  it("each 不感兴趣 on the same kind doubles the cooldown days; a repeat on one card reuses its pref action; undo takes the count back", async () => {
    const api = new FakeWikiApi();
    const [a] = await api.botSuggestions("today");
    const id = a?.suggestion_id ?? "";
    const first = await api.botReact(id, "not_interested");
    expect(first.preference_action_id).toBe(`act_pref_${id}`);
    await api.botDecide(first.preference_action_id ?? "", "undo");
    const again = await api.botReact(id, "not_interested");   // back to count 1 after the undo; the same action id
    expect(again.preference_action_id).toBe(first.preference_action_id);
    expect((await api.botActions()).filter((x) => x.kind === "preference")).toHaveLength(1);
    expect((await api.botActions()).find((x) => x.action_id === again.preference_action_id)).toMatchObject({ status: "executed" });
    expect((await api.botActions()).find((x) => x.action_id === again.preference_action_id)?.summary).toContain("以后少提醒");
    expect((await api.rsiChanges()).filter((c) => c.patch_id === again.preference_action_id)).toHaveLength(1);
  });

  it("why answers by suggestion and by trigger; recent hides skipped rows unless asked", async () => {
    const api = new FakeWikiApi();
    const [card] = await api.botSuggestions("today");
    const byS = await api.botWhy({ suggestionId: card?.suggestion_id ?? "" });
    const byT = await api.botWhy({ triggerId: card?.trigger_id ?? "" });
    expect(byS.trigger?.rule_id).toBe(byT.trigger?.rule_id);
    expect(await status(api.botWhy({ suggestionId: "nope" }))).toBe(404);
    const quiet = await api.botRecent(false);
    const all = await api.botRecent(true);
    expect(quiet.some((r) => r.result === "skipped")).toBe(false);
    expect(all.some((r) => r.result === "skipped")).toBe(true);
    expect(all.length).toBeGreaterThan(quiet.length);
  });

  it("pause sets the settings as B3 does; putBotSettings stores and rejects out-of-range numbers", async () => {
    const api = new FakeWikiApi({ now: () => new Date("2026-10-15T03:00:00Z") });
    const t = await api.botPause("tomorrow");
    expect(t).toMatchObject({ paused: "until", paused_until: "2026-10-15T16:00:00.000Z" });
    expect(await api.botPause("forever")).toMatchObject({ paused: "always", paused_until: null });
    expect(await api.botPause("off")).toMatchObject({ paused: "no", paused_until: null });
    const s = await api.botSettings();
    expect((await api.putBotSettings({ ...s, today_cap: 7 })).today_cap).toBe(7);
    expect((await api.botSettings()).today_cap).toBe(7);
    expect(await status(api.putBotSettings({ ...s, today_cap: 99 }))).toBe(422);
  });

  it("putBotSettings refuses the levels B3 refuses, with its texts", async () => {
    const api = new FakeWikiApi();
    const s = await api.botSettings();
    const put = (levels: Partial<BotSettings["levels"]>): Promise<BotSettings> => api.putBotSettings({ ...s, levels: { ...s.levels, ...levels } });
    await expect(put({ user_notes: "auto" })).rejects.toMatchObject({ status: 422, detail: "user_notes 的审批级别固定为 never，不能修改" });
    await expect(put({ page_body: "auto" })).rejects.toMatchObject({ status: 422, detail: "page_body 的级别只能是 ask / auto_undo" });
    await expect(put({ teaching_params: "auto" })).rejects.toMatchObject({ status: 422, detail: "teaching_params 的级别只能是 ask / auto_undo" });
    expect((await put({ page_body: "ask", delete_merge: "auto_undo" })).levels).toMatchObject({ page_body: "ask", delete_merge: "auto_undo" });
    expect((await put({ new_page: "ask", teaching_params: "ask", rsi: "ask" })).levels).toMatchObject({ new_page: "ask", teaching_params: "ask", rsi: "ask" });
  });

  it("review/today carries page_round_today: 0, streak_days: consecutive local days with a review, ending today or yesterday", async () => {
    let t = Date.parse("2026-10-15T03:00:00Z");
    const api = await organizedP1({ now: () => new Date(t) });
    expect((await api.reviewToday()).streak_days).toBe(0);
    const grade = async (): Promise<void> => {
      const item = (await api.reviewToday()).items.find((i) => i.mode === "cue");
      if (!item) throw new Error("no cue item");
      await api.gradeReview({ cell: item.cell, grade: "recalled", page_id: item.page_id, section_id: item.section_id });
    };
    await grade();
    expect((await api.reviewToday()).streak_days).toBe(1);
    t += 86_400_000;
    expect((await api.reviewToday()).streak_days).toBe(1);   // yesterday still counts while today has none
    await grade();
    expect((await api.reviewToday()).streak_days).toBe(2);
    t += 3 * 86_400_000;
    expect((await api.reviewToday()).streak_days).toBe(0);
  });

  it("review/today: a check answered from the list counts in done_today (as the server's reviews_done)", async () => {
    const api = await organizedP1();
    const chk = (await api.reviewToday()).items.find((i) => i.mode === "check");
    if (!chk) throw new Error("no check item");
    await api.gradeReview({ cell: chk.cell, choice: 0, page_id: chk.page_id, section_id: chk.section_id });
    expect((await api.reviewToday()).done_today).toBe(1);
  });

  it("review/today more = the next full batch once today's is answered (as the server)", async () => {
    const api = await organizedP1();
    await api.putSettings({ review_per_day: 1 });
    const first = await api.reviewToday();
    expect(first.due).toBeGreaterThanOrEqual(2);
    const item = first.items[0];
    if (!item) throw new Error("no item");
    await api.gradeReview(item.mode === "check" ? { cell: item.cell, choice: 0, page_id: item.page_id, section_id: item.section_id }
      : { cell: item.cell, grade: "recalled", page_id: item.page_id, section_id: item.section_id });
    const after = await api.reviewToday();
    expect(after.done_today).toBe(1);
    expect(after.items).toEqual([]);
    const more = await api.reviewToday(true);
    expect(more.items).toHaveLength(1);
    expect(more.items[0]?.cell).not.toBe(item.cell);
    expect(api.calls[api.calls.length - 1]).toEqual({ method: "reviewToday", args: [true] });
  });

  it("failNext and the usage limit: reads and writes pass (no LLM)", async () => {
    const api = new FakeWikiApi();
    api.__budget(true);
    expect(await status(api.botSuggestions("today"))).toBe(0);
    expect(await status(api.botPause("off"))).toBe(0);
    api.failNext("botBadge", new ApiError(500, "x"));
    expect(await status(api.botBadge())).toBe(500);
  });
});


it("replays only the requested fake run and checks its page ownership", async () => {
  const api = new FakeWikiApi({ delayMs: 0 });
  const first: OrganizeEvent = { event: "planning", data: { pass: 1 } };
  const second: OrganizeEvent = { event: "planning", data: { pass: 2 } };
  api.runEvents.r1 = { pageId: "p", events: [first] };
  api.runEvents.r2 = { pageId: "p", events: [second] };
  const got: OrganizeEvent[] = [];
  await api.runStream("p", "r1", (event) => got.push(event));
  expect(got).toEqual([first]);
  await expect(api.runStream("other-page", "r1", () => undefined)).rejects.toMatchObject({ status: 404 });
  await expect(api.runStream("p", "missing-run", () => undefined)).rejects.toMatchObject({ status: 404 });
});
