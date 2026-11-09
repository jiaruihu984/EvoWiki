import { describe, expect, it } from "vitest";
import { ApiError, OfflineError } from "../api/errors";
import { WIKI_COPY } from "./copy";
import { RSI_CHANGES } from "./fixtures";
import { isShown, newChanges, newestTime, recentChange, rsiCard, rsiErrorText, visibleChanges } from "./rsi-changes";
import { CHANGE_TRACKS, type ChangeTrack, PATCH_STATUSES, type PatchStatus, type RsiChangeOut } from "./schema";

const change = (track: ChangeTrack, status: PatchStatus, p: Partial<RsiChangeOut> = {}): RsiChangeOut => ({ tool_live: false,
  patch_id: `${track}-${status}`, track, status, summary: "讲新词之前先给一个例子", why: "你说新词没讲清楚",
  signals: [], before: "", after: "", at: "2026-10-09T09:00:00Z", can_undo: false, reading: "", kind: "", rule_before: "", rule_after: "",
  user_edited: false, layer: 0, target: "", outcome: "pending", effect: [], effect_line: "", pages_waiting: 0, pages_failed: 0, ...p,
});

describe("which changes are shown (no confirmation: nothing waits)", () => {
  const shown: PatchStatus[] = ["personal_accepted", "rolled_back"];
  for (const status of PATCH_STATUSES) {
    for (const track of CHANGE_TRACKS) {
      const want = shown.includes(status) || (status === "rejected" && track === "B");
      it(`${track} ${status}: ${want ? "shown" : "hidden"}`, () => {
        expect(isShown(change(track, status))).toBe(want);
        expect(rsiCard(change(track, status)) !== null).toBe(want);
      });
    }
  }

  it("awaiting_user (old confirm-flow records) and the system's own attempts are hidden; layer-2 rows of either track are shown", () => {
    expect(isShown(change("B", "awaiting_user"))).toBe(false);
    expect(isShown(change("B", "gate_failed", { layer: 2 }))).toBe(false);
    expect(isShown(change("B", "rejected", { layer: 2 }))).toBe(false);
    expect(isShown(change("A", "personal_accepted", { layer: 2 }))).toBe(true);
    expect(isShown(change("B", "rolled_back", { layer: 2 }))).toBe(true);
  });

  it("a code change (H, forgep:…): waiting for review → 等你审阅 (no 撤销); discarded → 没有采用; other H ids stay hidden", () => {
    const code = (status: PatchStatus): RsiChangeOut => change("H", status, { patch_id: "forgep:c1" });
    expect(rsiCard(code("awaiting_user"))).toMatchObject({ state: "review", undoable: false });
    expect(rsiCard(code("rejected"))?.state).toBe("declined");
    expect(isShown(code("candidate"))).toBe(false);
    expect(isShown(change("H", "awaiting_user", { patch_id: "forge:tool:1" }))).toBe(false);
  });

  it("a refused 采用 / 不要: off → a plain sentence; the backend's plain Chinese as is; anything raw → the busy line", () => {
    expect(rsiErrorText(new ApiError(409, "代码修改已关闭", "patches_off"), "forgep:c1")).toBe(WIKI_COPY.rsiPatchesOff);
    // by the code, not by the words: the same text without the code is just the backend's sentence
    expect(rsiErrorText(new ApiError(409, "代码修改已关闭", "action_refused"), "forgep:c1")).toBe("代码修改已关闭");
    expect(rsiErrorText(new ApiError(409, "这项改动不在等你审阅", "action_refused"), "forgep:c1")).toBe("这项改动不在等你审阅");
    expect(rsiErrorText(new ApiError(409, "patch is queued"), "forgep:c1")).toBe(WIKI_COPY.rsiBusy);
    expect(rsiErrorText(new ApiError(404, "x"), "forgep:c1")).toBe(WIKI_COPY.rsiGone);
    // a harness setting change / a forged tool: its plain refusal, not the busy line
    expect(rsiErrorText(new ApiError(409, "这次改动之后，同一个设置又被改过了，要先撤销后面那次", "action_refused"), "h_c1")).toBe("这次改动之后，同一个设置又被改过了，要先撤销后面那次");
    expect(rsiErrorText(new ApiError(409, "这个工具的上线记录找不到了，不能撤销", "action_refused"), "forge:tool:1")).toBe("这个工具的上线记录找不到了，不能撤销");
    expect(rsiErrorText(new ApiError(409, "busy"), "h_c1")).toBe(WIKI_COPY.rsiBusy);
  });

  it("keeps the backend's order (newest first)", () => {
    const list = [change("B", "personal_accepted", { patch_id: "x1" }), change("A", "candidate"), change("B", "gate_failed"),
      change("A", "rolled_back", { patch_id: "x2" }), change("B", "rejected", { patch_id: "x3" }), change("A", "rejected")];
    expect(visibleChanges(list).map((c) => c.patch_id)).toEqual(["x1", "x2", "x3"]);
    expect(visibleChanges(RSI_CHANGES).map((c) => c.patch_id)).toEqual(["pm1", "pb4", "pb3", "pa2", "pb2"]);
  });
});

describe("rsiCard", () => {
  it("summary and why exactly as the backend sends them (trimmed); the status as the plugin's word", () => {
    const c = rsiCard(change("B", "personal_accepted", { can_undo: true, effect_line: "之后看不懂的比例 30% → 12%，有效" }));
    expect(c).toMatchObject({ state: "live", summary: "讲新词之前先给一个例子", why: "你说新词没讲清楚", undoable: true });
    expect(WIKI_COPY.rsiStatus[c?.state ?? "undone"]).toBe("已生效");
    expect(c).not.toHaveProperty("effectLine");
    expect(c).not.toHaveProperty("date");
  });

  it("an empty why is null (no second line text); a track-A summary is shown as is, never re-parsed", () => {
    expect(rsiCard(change("B", "rolled_back", { why: " ", signals: ["一", "二"] }))).toMatchObject({ state: "undone", why: null, undoable: false });
    expect(rsiCard(change("A", "personal_accepted", { summary: "复习间隔每次拉长 ×2.0 → ×2.5", why: "按记录" })))
      .toMatchObject({ summary: "复习间隔每次拉长 ×2.0 → ×2.5", why: "按记录" });
    expect(rsiCard(change("A", "personal_accepted", { summary: "调高了「记得住多久」：2.00 → 2.50" }))?.summary)
      .toBe("调高了「记得住多久」：2.00 → 2.50");
  });

  it("撤销 only for a change in effect with can_undo", () => {
    expect(rsiCard(change("B", "personal_accepted", { can_undo: true }))?.undoable).toBe(true);
    expect(rsiCard(change("B", "personal_accepted", { can_undo: false }))?.undoable).toBe(false);
    expect(rsiCard(change("B", "rolled_back", { can_undo: true }))?.undoable).toBe(false);
    expect(rsiCard(change("B", "rejected"))).toMatchObject({ state: "declined", undoable: false });
  });

  it("「N 页等你打开时更新」 only for a ledger rule (track G) with pages still waiting; a track B change keeps it out (redesign)", () => {
    expect(rsiCard(change("G", "personal_accepted", { pages_waiting: 4 }))?.waiting).toBe("4 页等你打开时更新");
    expect(rsiCard(change("G", "rolled_back", { pages_waiting: 1 }))?.waiting).toBe("1 页等你打开时更新");
    expect(rsiCard(change("G", "personal_accepted"))?.waiting).toBeNull();
    expect(rsiCard(change("B", "personal_accepted", { pages_waiting: 3 }))?.waiting).toBeNull();
    expect(rsiCard(change("A", "personal_accepted", { pages_waiting: 2 }))?.waiting).toBeNull();
  });

  it("「M 页更新失败」 apart from the waiting pages, only for a ledger rule", () => {
    const c = rsiCard(change("G", "personal_accepted", { pages_waiting: 2, pages_failed: 1 }));
    expect([c?.waiting, c?.failed]).toEqual(["2 页等你打开时更新", "1 页更新失败"]);
    expect(rsiCard(change("G", "personal_accepted", { pages_failed: 1 }))).toMatchObject({ waiting: null, failed: "1 页更新失败" });
    expect(rsiCard(change("B", "personal_accepted", { pages_failed: 1 }))?.failed).toBeNull();
    expect(rsiCard(change("A", "personal_accepted", { pages_failed: 2 }))?.failed).toBeNull();
  });
});

describe("new changes (the 参数 red dot)", () => {
  const list = [change("B", "personal_accepted", { patch_id: "n3", at: "2026-10-11T09:00:00Z" }),
    change("B", "gate_failed", { patch_id: "f", at: "2026-10-12T09:00:00Z" }),
    change("A", "rolled_back", { patch_id: "n2", at: "2026-10-10T09:00:00Z" }),
    change("B", "personal_accepted", { patch_id: "bad", at: "?" }),
    change("B", "personal_accepted", { patch_id: "n1", at: "2026-10-09T09:00:00Z" })];
  const t = (iso: string) => Date.parse(iso);

  it("newChanges: shown changes newer than the time 参数 last showed; never opened = all; an unreadable time is never new", () => {
    expect(newChanges(list, null).map((c) => c.patch_id)).toEqual(["n3", "n2", "n1"]);
    expect(newChanges(list, t("2026-10-10T09:00:00Z")).map((c) => c.patch_id)).toEqual(["n3"]);
    expect(newChanges(list, t("2026-10-11T09:00:00Z"))).toEqual([]);
  });

  it("newestTime: the newest shown change's time (a hidden attempt does not count); recentChange: the newest new one in effect", () => {
    expect(newestTime(list)).toBe(t("2026-10-11T09:00:00Z"));
    expect(newestTime([])).toBeNull();
    expect(recentChange(list, null)?.patch_id).toBe("n3");
    expect(recentChange(list, t("2026-10-11T09:00:00Z"))).toBeNull();
    expect(recentChange([list[2]!], null)).toBeNull();          // undone: not in effect
  });
});

describe("rsiErrorText", () => {
  it("409 → one busy sentence, 404 → gone, 5xx → plain retry-later; never the server's detail", () => {
    for (const detail of ["改进正在进行中", "patch pb3 is B/rejected; only accepted patches can be undone"]) {
      expect(rsiErrorText(new ApiError(409, detail))).toBe(WIKI_COPY.rsiBusy);
    }
    // a ledger row's 409 carries a plain sentence: shown as is
    expect(rsiErrorText(new ApiError(409, "「陪读助手」的写法后来又改过了，先撤销后来的那次"), "act_0003"))
      .toBe("「陪读助手」的写法后来又改过了，先撤销后来的那次");
    expect(rsiErrorText(new ApiError(409, "改进正在进行中"), "pb3")).toBe(WIKI_COPY.rsiBusy);
    expect(rsiErrorText(new ApiError(404, "no patch pb9"))).toBe(WIKI_COPY.rsiGone);
    expect(rsiErrorText(new ApiError(500, "DecisionError: x"))).toBe(WIKI_COPY.rsiFailed);
    expect(rsiErrorText(new ApiError(502, "bad gateway"))).toBe(WIKI_COPY.rsiFailed);
    expect(rsiErrorText(new OfflineError())).toBe(WIKI_COPY.offline);
    expect(rsiErrorText(new ApiError(429, "Too Many Requests"))).toBe(WIKI_COPY.budget);
    expect(rsiErrorText(new Error("boom"))).toBe(WIKI_COPY.failed);
  });
});
