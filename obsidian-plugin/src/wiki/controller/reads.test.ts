import { describe, expect, it } from "vitest";
import { FakeConn } from "../controller-test-kit";
import { FakeWikiApi } from "../fake";
import type { ProgressOut } from "../schema";
import { WikiCore } from "./core";
import { Notes } from "./notes";
import { Reads } from "./reads";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

describe("learning progress request order", () => {
  it.each(["success", "error"])("drops an older %s after the newest progress lands", async (outcome) => {
    const api = new FakeWikiApi({ delayMs: 0 });
    const old = await api.progress();
    const current = { ...old, today: { ...old.today, goal: "current goal" } };
    const first = deferred<ProgressOut>();
    const second = deferred<ProgressOut>();
    const replies = [first, second];
    api.progress = () => replies.shift()!.promise;
    const core = new WikiCore({ blockEventStore: null, blockProgressStore: null, api, conn: new FakeConn(), sync: null, openNote: () => false, configured: () => true, demo: true });
    const reads = new Reads(core, new Notes(core));
    const a = reads.loadProgress();
    const b = reads.loadProgress();
    second.resolve(current);
    await b;
    if (outcome === "success") first.resolve(old);
    else first.reject(new Error("old request failed"));
    await a;
    expect(core.state.progress).toEqual({ kind: "ready", value: current });
  });

  it("does not hide the newest failure with an older successful response", async () => {
    const api = new FakeWikiApi({ delayMs: 0 });
    const old = await api.progress();
    const first = deferred<ProgressOut>();
    const second = deferred<ProgressOut>();
    const replies = [first, second];
    api.progress = () => replies.shift()!.promise;
    const core = new WikiCore({ blockEventStore: null, blockProgressStore: null, api, conn: new FakeConn(), sync: null, openNote: () => false, configured: () => true, demo: true });
    const reads = new Reads(core, new Notes(core));
    const a = reads.loadProgress();
    const b = reads.loadProgress();
    second.reject(new Error("new request failed"));
    await b;
    const latest = core.state.progress;
    expect(latest.kind).toBe("error");
    first.resolve(old);
    await a;
    expect(core.state.progress).toEqual(latest);
  });
});
