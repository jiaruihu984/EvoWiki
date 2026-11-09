import { describe, expect, it } from "vitest";
import { ApiError, OfflineError } from "../api/errors";
import { WIKI_COPY } from "./copy";
import { methods, setup } from "./controller-test-kit";
import type { WikiController } from "./controller";

const status = (c: WikiController, id: string): string | undefined => {
  const r = c.state.rsi;
  return r.kind === "ready" ? r.value.find((x) => x.patch_id === id)?.status : undefined;
};

describe("撤销 (undoPatch): every layer goes through POST …/undo, then the list is re-read", () => {
  it("a layer-2 method change: POST undo, Notice 撤销了…, the list re-read, the row 已撤销", async () => {
    const { c, api, notices } = setup();
    await c.init();
    const n = api.calls.length;
    await c.undoPatch("pm1");
    expect(methods(api).slice(n)).toEqual(["undoPatch", "rsiChanges"]);
    expect(api.calls.find((x) => x.method === "undoPatch")?.args).toEqual(["pm1"]);
    expect(notices).toEqual(["已撤销"]);   // a layer-2 method change: the generic word (copy guide: undo notice by kind)
    expect(status(c, "pm1")).toBe("rolled_back");
    expect(c.state.rsiNote).toBeNull();
    expect(c.state.busy).toBeNull();
  });

  it("a layer-0 change of track A too", async () => {
    const { c, api } = setup();
    await c.init();
    const n = api.calls.length;
    await c.undoPatch("pa2");
    expect(methods(api).slice(n)).toEqual(["undoPatch", "rsiChanges"]);
    expect(status(c, "pa2")).toBe("rolled_back");
  });

  it("a round in progress: one plain Notice (never the server's text), nothing left on the row, the list re-read", async () => {
    const { c, api, notices } = setup();
    await c.init();
    api.failNext("undoPatch", new ApiError(409, "patch pm1 is not the latest of its kind"));
    const n = api.calls.length;
    await c.undoPatch("pm1");
    expect(c.state.rsiNote).toBeNull();
    expect(notices).toEqual([WIKI_COPY.rsiBusy]);
    expect(status(c, "pm1")).toBe("personal_accepted");
    expect(methods(api).slice(n)).toEqual(["undoPatch", "rsiChanges"]);
  });

  it("offline: the sentence, no re-read", async () => {
    const { c, api } = setup();
    await c.init();
    api.failNext("undoPatch", new OfflineError());
    const n = api.calls.length;
    await c.undoPatch("pm1");
    expect(methods(api).slice(n)).toEqual(["undoPatch"]);
    expect(c.state.rsiNote?.note.text).toBe(WIKI_COPY.offline);
  });

  it("in the demo nothing is sent", async () => {
    const { c, api } = setup({ demo: true });
    await c.init();
    const n = api.calls.length;
    await c.undoPatch("pm1");
    expect(api.calls.length).toBe(n);
  });

  it("the controller has no accept / reject / drafts any more (changes apply by themselves)", () => {
    const { c } = setup();
    const any = c as unknown as Record<string, unknown>;
    for (const k of ["acceptPatch", "acceptPatchWith", "rejectPatch", "setRsiDraft", "viewStyleChange"]) expect(any[k], k).toBeUndefined();
    expect("rsiDrafts" in c.state).toBe(false);
    expect("waitingFocus" in c.state).toBe(false);
  });
});

describe("a 今天 refresh", () => {
  it("re-reads the changes quietly", async () => {
    const { c, api } = setup();
    await c.init();
    const n = api.calls.length;
    await c.refreshToday(true);
    expect(methods(api).slice(n)).toContain("rsiChanges");
    expect(c.state.rsi.kind).toBe("ready");
  });
});
