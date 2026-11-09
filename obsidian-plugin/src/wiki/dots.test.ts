import { describe, expect, it, vi } from "vitest";
import { DotTracker, MAX_SEEN_DOTS, pendingKeys } from "./dots";
import { RSI_CHANGES } from "./fixtures";
import type { ActionOut, SuggestionOut } from "./bot/bot-schema";
import type { ConfirmCardOut, ReviewItemOut, RsiChangeOut, SpecItem, TeachSpecOut } from "./schema";
import { INITIAL_WIKI_STATE, type WikiState } from "./state";

const NOW = new Date(2026, 9, 10, 9, 0);
const PM1 = RSI_CHANGES.find((c) => c.patch_id === "pm1") as RsiChangeOut;
/** A change applied after PM1 (newer `at`). */
const NEWER: RsiChangeOut = { ...PM1, patch_id: "pb9", at: "2026-10-12T09:00:00Z" };
const item = (cell: string): ReviewItemOut => ({ card_id: `card-${cell}`, cell, cells: [cell], concept_id: "c", concept_name: "均值", aspect: "procedure",
  band: "likely", mode: "cue", page_id: "p1", page_title: "页一", section_id: "s1", heading: "h", cue: "x", answer: "", check: null, recall_now: 0.5,
  days_to_threshold: -1, in_goal: true, exam: false, artifact_id: null });
const confirm = (id: string): ConfirmCardOut => ({ followup_id: id } as unknown as ConfirmCardOut);
const sug = (id: string): SuggestionOut => ({ suggestion_id: id, kind: "goal_idle", title: `t${id}`, why: "w", placement: "today", status: "open" } as unknown as SuggestionOut);
const act = (id: string, status = "proposed"): ActionOut => ({ action_id: id, status, created_at: "2026-10-01T00:00:00Z", decided_at: null } as unknown as ActionOut);
const pendingRule = (id: string): SpecItem => ({ item_id: id, text: `t${id}`, authority: "pending", origin: "memory_backfill" } as unknown as SpecItem);
const prefs = (pending: string[]): TeachSpecOut => ({ items: pending.map(pendingRule), decisions: [], evidence: {} } as unknown as TeachSpecOut);
/** A code change waiting for review (track H, forgep:). */
const CODE: RsiChangeOut = { ...PM1, patch_id: "forgep:c1", track: "H", status: "awaiting_user", at: "2026-10-11T09:00:00Z" };
function state(o: { items?: ReviewItemOut[]; confirms?: ConfirmCardOut[]; rsi?: RsiChangeOut[]; cards?: string[]; actions?: ActionOut[]; seen?: string[]; pending?: string[] } = {}): WikiState {
  return { ...INITIAL_WIKI_STATE, rsi: { kind: "ready", value: o.rsi ?? [] }, prefs: { kind: "ready", value: prefs(o.pending ?? []) },
    bot: { ...INITIAL_WIKI_STATE.bot, today: { kind: "ready", value: (o.cards ?? []).map(sug) }, actions: { kind: "ready", value: o.actions ?? [] } },
    review: { ...INITIAL_WIKI_STATE.review, seen: o.seen ?? [], today: { kind: "ready",
      value: { items: o.items ?? [], due: 0, done_today: 0, limit: 10, confirms: o.confirms ?? [], page_round_today: 0, streak_days: 0 } } } };
}

describe("pendingKeys (app.js pending)", () => {
  it("个性化 counts a memory group and a profile fact waiting for him", () => {
    const fact = { action_id: "act1", kind: "profile_fact", status: "proposed", expires_at: null, summary: "记进档案：护士", created_at: "2026-10-10T00:00:00Z" } as unknown as ActionOut;
    const s = { ...state({ actions: [fact] }), conflicts: { kind: "ready" as const, value: [{ id: "mc1", rows: [] }] } };
    const keys = pendingKeys(s, NOW, null).engine;
    expect(keys).toContain("pend:mc1");
    expect(keys).toContain("pend:act1");
  });
  it("今天: review:<due cards + due confirms>:<day> while not done, card:<suggestion id>, confirm:<id>; 个性化: pend:<rule> per preference to confirm, code:<patch> per code change to review, log:<patch> per new change", () => {
    const s = state({ items: [item("a"), item("b")], confirms: [confirm("f1")], rsi: [CODE, PM1], cards: ["g1"], actions: [act("a1"), act("a2", "applied")], pending: ["q1"] });
    expect(pendingKeys(s, NOW)).toEqual({
      today: ["review:3:2026-10-10", "card:g1", "confirm:f1"],
      engine: ["pend:q1", "code:forgep:c1", `log:pm1@${Date.parse(PM1.at)}`],
    });
  });
  it("what he did himself in the chat is no new change on 个性化; what the Bot did by itself is", () => {
    const mine: ActionOut = { ...act("pm1", "executed"), source: "assistant" };
    expect(pendingKeys(state({ rsi: [PM1], actions: [mine] }), NOW).engine).toEqual([]);
    const bot: ActionOut = { ...act("pm1", "executed"), source: "bot" };
    expect(pendingKeys(state({ rsi: [PM1], actions: [bot] }), NOW).engine).toEqual([`log:pm1@${Date.parse(PM1.at)}`]);
  });
  it("a ledger action waiting for a yes / no is not counted on 个性化 (it waits on its page)", () => {
    expect(pendingKeys(state({ actions: [act("a1"), act("a2", "approved")] }), NOW).engine).toEqual([]);
  });
  it("a finished review raises nothing; a change no newer than the time 个性化 last showed, or never shown, is no log entry", () => {
    const s = state({ items: [item("a")], seen: ["a"], rsi: [NEWER, PM1, { ...PM1, patch_id: "gf", status: "gate_failed", at: "2026-10-13T09:00:00Z" }] });
    expect(pendingKeys(s, NOW, Date.parse(PM1.at))).toEqual({ today: [], engine: [`log:pb9@${Date.parse(NEWER.at)}`] });
    expect(pendingKeys(s, NOW, Date.parse(NEWER.at))).toEqual({ today: [], engine: [] });
  });
  it("his own hand edit is in 活动记录 but lights no dot", () => {
    const s = state({ rsi: [NEWER], actions: [{ ...act("pb9", "executed"), kind: "hand_edit" }] });
    expect(pendingKeys(s, NOW, Date.parse(PM1.at)).engine).toEqual([]);
  });
});

describe("DotTracker (updateDots / SEEN)", () => {
  it("a forged tool that went live counts on 个性化 and 活动记录 until 活动记录 was open with it, also after opening 个性化 and a restart; an undone one stops counting", () => {
    const TOOL: RsiChangeOut = { ...PM1, patch_id: "h_t1", track: "H", tool_live: true, at: "2026-10-12T09:00:00Z" };
    let saved: string[] = [];
    const store = { load: () => saved, save: (k: string[]) => { saved = k; } };
    const t = new DotTracker(store);
    const s = state({ rsi: [TOOL] });
    expect(t.update(s, "today", NOW)).toMatchObject({ engine: 1, log: 1 });
    expect(t.update(s, "engine", NOW)).toMatchObject({ engine: 1, log: 1 });   // 活动记录 still folded
    expect(new DotTracker(store).update(s, "today", NOW)).toMatchObject({ engine: 1, log: 1 });
    const open = { ...s, personal: { ...s.personal, log: true } };
    expect(t.update(open, "engine", NOW)).toMatchObject({ engine: 0, log: 0 });
    expect(new DotTracker(store).update(s, "today", NOW)).toMatchObject({ engine: 0, log: 0 });
    const undone = state({ rsi: [{ ...TOOL, patch_id: "h_t2", tool_live: false, status: "rolled_back" }] });
    expect(new DotTracker(store).update(undone, "engine", NOW).log).toBe(0);
  });

  it("a dot shows on a tab that is not shown and has an unseen key; showing it marks its keys seen (saved)", () => {
    let saved: string[] = [];
    const save = vi.fn((k: string[]) => { saved = k; });
    const t = new DotTracker({ load: () => saved, save });
    const s = state({ items: [item("a")], rsi: [PM1] });
    expect(t.update(s, "assistant", NOW)).toEqual({ today: 1, engine: 1, log: 0 });
    expect(save).not.toHaveBeenCalled();
    expect(t.update(s, "today", NOW)).toEqual({ today: 0, engine: 1, log: 0 });
    expect(saved).toEqual(["today|review:1:2026-10-10"]);
    expect(t.update(s, "assistant", NOW)).toEqual({ today: 0, engine: 1, log: 0 });
    expect(t.update(s, "engine", NOW)).toEqual({ today: 0, engine: 0, log: 0 });
    expect(saved).toEqual(["today|review:1:2026-10-10", `engine@${Date.parse(PM1.at)}`]);
    expect(save).toHaveBeenCalledTimes(2);
    expect(t.update(s, "engine", NOW)).toEqual({ today: 0, engine: 0, log: 0 });
    expect(save).toHaveBeenCalledTimes(2);                 // nothing new: no write
    // A new item raises it again; the same review tomorrow is a new item too.
    const more = state({ items: [item("a")], rsi: [PM1], confirms: [confirm("f2")] });
    expect(t.update(more, "assistant", NOW)).toEqual({ today: 2, engine: 0, log: 0 });
    expect(t.update(s, "assistant", new Date(2026, 9, 11, 9, 0))).toEqual({ today: 1, engine: 0, log: 0 });
  });

  it("个性化: a newly applied change lights the dot, opening the tab clears it, an already-seen change (also once undone) does not relight", () => {
    const t = new DotTracker(null);
    t.update(state({ rsi: [PM1] }), "engine", NOW);
    expect(t.update(state({ rsi: [PM1] }), "today", NOW).engine).toBe(0);
    expect(t.update(state({ rsi: [NEWER, PM1] }), "today", NOW).engine).toBeGreaterThan(0);        // new change applied
    expect(t.update(state({ rsi: [NEWER, PM1] }), "engine", NOW).engine).toBe(0);      // opened: cleared
    expect(t.update(state({ rsi: [NEWER, PM1] }), "today", NOW).engine).toBe(0);       // seen: stays off
    expect(t.update(state({ rsi: [{ ...NEWER, status: "rolled_back" }, PM1] }), "today", NOW).engine).toBe(0);
    // the system's own attempt that never took effect raises nothing
    const attempt: RsiChangeOut = { ...PM1, patch_id: "gf", status: "gate_failed", at: "2026-10-13T09:00:00Z" };
    expect(t.update(state({ rsi: [attempt, NEWER, PM1] }), "today", NOW).engine).toBe(0);
  });

  it("个性化: a seen change rolled back automatically (the backend bumps its `at`) relights the dot; opening the tab clears it again", () => {
    const t = new DotTracker(null);
    t.update(state({ rsi: [NEWER, PM1] }), "engine", NOW);
    expect(t.update(state({ rsi: [NEWER, PM1] }), "today", NOW).engine).toBe(0);
    const rolled: RsiChangeOut = { ...NEWER, status: "rolled_back", effect_line: "之后看不懂的比例 12% → 25%，变差，已自动撤回",
      at: "2026-10-13T09:00:00Z" };
    expect(t.update(state({ rsi: [rolled, PM1] }), "today", NOW).engine).toBeGreaterThan(0);
    expect(t.update(state({ rsi: [rolled, PM1] }), "engine", NOW).engine).toBe(0);
    expect(t.update(state({ rsi: [rolled, PM1] }), "today", NOW).engine).toBe(0);
  });

  it("log keys are never stored as seen (the last-opened time decides): the key list stays within its bound", () => {
    let saved: string[] = [];
    const t = new DotTracker({ load: () => saved, save: (k) => { saved = k; } });
    const many = Array.from({ length: MAX_SEEN_DOTS + 20 }, (_, i) =>
      ({ ...PM1, patch_id: `m${i}`, at: new Date(Date.parse(PM1.at) + i * 1000).toISOString() }));
    t.update(state({ rsi: many }), "engine", NOW);
    expect(saved.filter((k) => k.includes("log:"))).toEqual([]);
    expect(saved).toEqual([`engine@${Date.parse(PM1.at) + (MAX_SEEN_DOTS + 19) * 1000}`]);
    // an old version's stored log keys are dropped on load
    const old = new DotTracker({ load: () => ["engine|log:pb4", "today|confirm:f1"], save: () => undefined });
    expect(old.update(state({ rsi: [PM1] }), "assistant", NOW).engine).toBeGreaterThan(0);
  });

  it("persisted keys and the last-opened time survive a restart; the key list is bounded to the newest MAX_SEEN_DOTS", () => {
    const old = Array.from({ length: MAX_SEEN_DOTS + 5 }, (_, i) => `today|card:x${i}`);
    let saved: string[] = [...old, `engine@${Date.parse(PM1.at)}`];
    const t = new DotTracker({ load: () => saved, save: (k) => { saved = k; } });
    expect(t.update(state({ rsi: [PM1] }), "assistant", NOW).engine).toBe(0);
    expect(t.update(state({ rsi: [NEWER, PM1] }), "assistant", NOW).engine).toBeGreaterThan(0);
    expect(t.update(state({ cards: ["new"] }), "assistant", NOW).today).toBe(1);   // a new task card
    t.update(state({ cards: ["new"] }), "today", NOW);
    expect(saved.filter((k) => !k.startsWith("engine@"))).toHaveLength(MAX_SEEN_DOTS);
    expect(saved).toContain("today|card:new");
    expect(saved).toContain(`engine@${Date.parse(PM1.at)}`);
  });

  it("今天: two unseen cards count 2; showing 今天 shows 0 and persists, so a restart stays 0 and a third card is 1", () => {
    let saved: string[] = [];
    const t = new DotTracker({ load: () => saved, save: (k) => { saved = k; } });
    expect(t.update(state({ cards: ["g1", "g2"] }), "engine", NOW).today).toBe(2);
    expect(t.update(state({ cards: ["g1", "g2"] }), "today", NOW).today).toBe(0);
    expect(saved).toEqual(["today|card:g1", "today|card:g2"]);
    const again = new DotTracker({ load: () => saved, save: (k) => { saved = k; } });
    expect(again.update(state({ cards: ["g1", "g2"] }), "assistant", NOW).today).toBe(0);
    expect(again.update(state({ cards: ["g1", "g2", "g3"] }), "assistant", NOW).today).toBe(1);
  });

  it("个性化: a preference to confirm and a code change to review stay counted after opening 个性化 (never seen) until decided; a change after the last opening adds 1; stored prop: keys are dropped", () => {
    let saved: string[] = ["engine|prop:old"];
    const t = new DotTracker({ load: () => saved, save: (k) => { saved = k; } });
    const waiting = state({ pending: ["q1"], rsi: [CODE, PM1] });
    expect(t.update(waiting, "today", NOW).engine).toBe(3);
    expect(t.update(waiting, "engine", NOW).engine).toBe(2);
    expect(saved.some((k) => k.includes("pend:") || k.includes("code:") || k.includes("prop:"))).toBe(false);
    expect(t.update(waiting, "today", NOW).engine).toBe(2);
    expect(t.update(state({ pending: ["q1"], rsi: [CODE, NEWER, PM1] }), "today", NOW).engine).toBe(3);
    expect(t.update(state({ rsi: [{ ...CODE, status: "personal_accepted" }, PM1] }), "today", NOW).engine).toBe(0);
  });

  it("confirm: all due confirmation questions are ONE key (demo pending() pushes a single \"confirm\"), sorted ids joined by a comma", () => {
    const s = state({ items: [item("a")], confirms: [confirm("f2"), confirm("f1")] });
    expect(pendingKeys(s, NOW).today).toEqual(["review:3:2026-10-10", "confirm:f1,f2"]);
    const t = new DotTracker(null);
    expect(t.update(s, "assistant", NOW).today).toBe(2);                 // the review card + one confirm entry, not three
    t.update(s, "today", NOW);
    expect(t.update(state({ items: [item("a")], confirms: [confirm("f1"), confirm("f2")] }), "assistant", NOW).today).toBe(0);   // order does not matter
    expect(t.update(state({ items: [item("a")], confirms: [confirm("f1"), confirm("f2"), confirm("f3")] }), "assistant", NOW).today).toBe(2);   // the set changed
  });

  it("confirm: keys only count while the local review card is drawn (hidden review card -> no confirm keys)", () => {
    const s = state({ items: [item("a")], confirms: [confirm("f1")] });
    expect(pendingKeys(s, NOW).today).toEqual(["review:2:2026-10-10", "confirm:f1"]);
    const hidden = { ...s, bot: { ...s.bot, reviewHidden: true } };
    expect(pendingKeys(hidden, NOW).today).toEqual([]);
  });

  it("without a store it still works for the view's life", () => {
    const t = new DotTracker(null);
    const s = state({ rsi: [PM1] });
    expect(t.update(s, "today", NOW).engine).toBeGreaterThan(0);
    t.update(s, "engine", NOW);
    expect(t.update(s, "today", NOW).engine).toBe(0);
  });
});
