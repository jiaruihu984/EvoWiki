import { describe, expect, it } from "vitest";
import { demoRsiLog } from "../fixtures-demo";
import { INITIAL_WIKI_STATE, type WikiState } from "../state";
import type { ActionOut } from "./bot-schema";
import { changeRows, waiting } from "./change-rows";

const act = (o: Partial<ActionOut> & Pick<ActionOut, "action_id" | "scope" | "source" | "status">): ActionOut => ({
  kind: "x", level: "ask", summary: "s", why: "w", content: {}, page_id: null, detail: "", created_at: "2026-10-10T08:00:00Z",
  decided_at: null, expires_at: null, can_undo: false, diff: [], confirm: false, cost_usd: null, made: [], revise: null, result: {}, section_id: null, ...o });
const NOW = new Date(2026, 9, 10, 12);
const graphRow = () => demoRsiLog(NOW).find((c) => c.track === "G")!;
const state = (actions: ActionOut[], o: Partial<WikiState> = {}): WikiState => ({
  ...INITIAL_WIKI_STATE, rsi: { kind: "ready", value: demoRsiLog(NOW) },
  bot: { ...INITIAL_WIKI_STATE.bot, actions: { kind: "ready", value: actions } }, ...o });

describe("changeRows", () => {
  it("waits first (newest first), then the shown changes in order; a joined ledger change keeps its action", () => {
    const g = graphRow();
    const joined = act({ action_id: g.patch_id, scope: "page_body", source: "bot", status: "executed" });
    const rows = changeRows(state([
      act({ action_id: "w_old", scope: "structure", source: "assistant", status: "proposed", created_at: "2026-10-09T00:00:00Z" }),
      joined, act({ action_id: "w_new", scope: "structure", source: "assistant", status: "proposed", created_at: "2026-10-11T00:00:00Z" })]));
    expect(rows.map((r) => [r.type, r.key])).toEqual([
      ["wait", "act:w_new"], ["wait", "act:w_old"], ["change", "rsi:forgep:demo-1"], ["change", `act:${g.patch_id}`], ["change", "rsi:pb-demo-6"],
      ["change", "rsi:forge:demo-1"], ["change", "rsi:pb-demo-2"], ["change", "rsi:pa-demo-3"], ["change", "rsi:pb-demo-3"], ["change", "rsi:pb-demo-4"]]);
    const graph = rows.find((r) => r.key === `act:${g.patch_id}`)!;
    expect(graph.type === "change" && graph.action).toBe(joined);
  });

  it("a track G row without a loaded ledger is still keyed act:; an unread ledger/RSI draws only what is there", () => {
    expect(changeRows(state([])).map((r) => r.key)).toContain("act:pg-demo-1");
    const s = state([act({ action_id: "w", scope: "structure", source: "assistant", status: "proposed" })], { rsi: { kind: "loading" } });
    expect(changeRows(s).map((r) => r.type)).toEqual(["wait"]);
    expect(changeRows({ ...INITIAL_WIKI_STATE, rsi: { kind: "ready", value: [] } })).toEqual([]);
  });

  it("a proposed action is never also drawn as a change", () => {
    const g = graphRow();
    const rows = changeRows(state([act({ action_id: g.patch_id, scope: "structure", source: "assistant", status: "proposed" })]));
    expect(rows.filter((r) => r.key === `act:${g.patch_id}`).map((r) => r.type)).toEqual(["wait"]);
  });

  it("a fresh approved action (executing right now) does not wait; one older than 2 minutes does (decided_at, else created_at)", () => {
    const now = Date.now();
    const fresh = act({ action_id: "f", scope: "page_body", source: "assistant", status: "approved", decided_at: new Date(now - 30_000).toISOString() });
    const stale = act({ action_id: "s", scope: "page_body", source: "assistant", status: "approved", decided_at: new Date(now - 150_000).toISOString() });
    const noDecided = act({ action_id: "n", scope: "page_body", source: "assistant", status: "approved", created_at: new Date(now - 150_000).toISOString() });
    expect([fresh, stale, noDecided].map((a) => waiting(a, now))).toEqual([false, true, true]);
  });

  it("an approved action (B3: a 429 left it approved) still waits for a yes", () => {
    const rows = changeRows(state([act({ action_id: "ap", scope: "page_body", source: "assistant", status: "approved" })]));
    expect(rows.filter((r) => r.key === "act:ap").map((r) => r.type)).toEqual(["wait"]);
  });
});
