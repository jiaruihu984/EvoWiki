import { describe, expect, it, vi } from "vitest";
import { ApiError, OfflineError } from "../../api/errors";
import type { ActionOut } from "../bot/bot-schema";
import { DraftDecisions, retryable } from "./decisions";

describe("DraftDecisions (one in-page draft decision, two places)", () => {
  it("one claim at a time; a failure offers 请重试 (a new claim); went through: no more claims; every change is told", () => {
    const d = new DraftDecisions(), told = vi.fn<(id: string) => void>();
    d.subscribe(told);
    expect(d.get("a")).toEqual({ busy: null, failed: null, settled: null });
    expect(d.start("a", "approve")).toBe(true);
    expect(d.start("a", "reject")).toBe(false);
    d.fail("a", "approve");
    expect(d.get("a")).toEqual({ busy: null, failed: "approve", settled: null });
    expect(d.start("a", "approve")).toBe(true);
    const row = { action_id: "a", status: "executed" } as ActionOut;
    d.settle("a", row);
    expect(d.get("a").settled).toBe(row);
    expect(d.start("a", "reject")).toBe(false);
    d.clear("b");                              // nothing to clear: not told
    expect(told.mock.calls.map((x) => x[0])).toEqual(["a", "a", "a", "a"]);
  });

  it("retryable: not reached or failed on the server (offline, network, 5xx); a 4xx refusal is not", () => {
    expect(retryable(new OfflineError())).toBe(true);
    expect(retryable(new TypeError("fetch failed"))).toBe(true);
    expect(retryable(new ApiError(502, "bad gateway"))).toBe(true);
    expect(retryable(new ApiError(409, "这一页之后又改过了"))).toBe(false);
    expect(retryable(new ApiError(404, "gone"))).toBe(false);
  });
});
