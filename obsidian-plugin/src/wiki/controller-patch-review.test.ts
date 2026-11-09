import { describe, expect, it } from "vitest";
import { OfflineError } from "../api/errors";
import { WIKI_COPY } from "./copy";
import { FakeWikiApi } from "./fake";
import { RSI_CHANGES } from "./fixtures";
import { methods, setup } from "./controller-test-kit";
import type { WikiController } from "./controller";
import { renderEngine } from "./render-engine";
import { fakeActions } from "./render-test-kit";
import type { RsiChangeOut } from "./schema";

/** A code change the agent wrote, waiting for review (forge/change_source.py ForgePatchChangeSource). */
const CODE: RsiChangeOut = {
  ...RSI_CHANGES[0]!, patch_id: "forgep:c1", track: "H", layer: 0, target: "代码", status: "awaiting_user", kind: "",
  summary: "等你审阅的代码改动：复习卡先给一句提示", why: "改了 2 个文件；检查全部通过；花费 $0.12", can_undo: false,
  at: "2099-01-01T00:00:00Z",
};

const row = (c: WikiController, id = CODE.patch_id): RsiChangeOut | undefined => {
  const r = c.state.rsi;
  return r.kind === "ready" ? r.value.find((x) => x.patch_id === id) : undefined;
};
/** The row in 个性化 · 活动记录 (expanded, 更早 shown). */
const drawn = (c: WikiController): HTMLElement | null =>
  renderEngine({ ...c.state, personal: { ...c.state.personal, log: true, older: true } }, fakeActions())
    .querySelector<HTMLElement>(`[data-chg="rsi:${CODE.patch_id}"]`);

function start(o: { off?: boolean } = {}) {
  const api = new FakeWikiApi({ rsi: [structuredClone(CODE), ...structuredClone([...RSI_CHANGES])] });
  api.codePatchesOff = o.off === true;
  return setup({ api });
}

describe("a code change waiting for review: 采用 / 不要 (POST …/approve | …/discard), then the list is re-read", () => {
  it("采用: approved, the row now says it is approved and not live yet (no buttons, no 撤销 until it lands)", async () => {
    const { c, api, notices } = start();
    await c.init();
    expect(drawn(c)?.dataset.rsiState).toBe("review");
    const n = api.calls.length;
    await c.approvePatch(CODE.patch_id);
    expect(methods(api).slice(n)).toEqual(["approvePatch", "rsiChanges"]);
    expect(notices).toEqual([WIKI_COPY.rsiAdopted]);
    expect(row(c)).toMatchObject({ status: "personal_accepted", can_undo: false, summary: "已批准、还没生效的代码改动：复习卡先给一句提示" });
    const r = drawn(c)!;
    expect(r.className).toBe("ew-pz-it");
    expect(r.querySelector("[data-action=patch-approve], [data-action=patch-discard], [data-action=rsi-undo]")).toBeNull();
    expect(c.state.rsiNote).toBeNull();
    expect(c.state.busy).toBeNull();
  });

  it("不要: discarded, the row is a faint 已取消", async () => {
    const { c, api, notices } = start();
    await c.init();
    const n = api.calls.length;
    await c.discardPatch(CODE.patch_id);
    expect(methods(api).slice(n)).toEqual(["discardPatch", "rsiChanges"]);
    expect(notices).toEqual([WIKI_COPY.rsiDiscarded]);
    expect(row(c)).toMatchObject({ status: "rejected", summary: "没采用的代码改动：复习卡先给一句提示" });
    const r = drawn(c)!;
    expect(r.querySelector(".ew-pz-t")?.textContent).toBe("没采用的代码改动：复习卡先给一句提示");
    expect(r.querySelector(".ew-pz-t")?.className).toBe("ew-pz-t ew-off");
    expect(r.querySelector(".ew-pz-tag")?.textContent).toBe("已取消");
    expect(r.querySelector("button[data-action]")).toBeNull();
  });

  it("采用 while code changes are off: one plain sentence under the row, the row still waits, no Notice", async () => {
    const { c, api, notices } = start({ off: true });
    await c.init();
    const n = api.calls.length;
    await c.approvePatch(CODE.patch_id);
    expect(methods(api).slice(n)).toEqual(["approvePatch", "rsiChanges"]);
    expect(c.state.rsiNote).toEqual({ patchId: CODE.patch_id, note: { text: WIKI_COPY.rsiPatchesOff, tone: "error" } });
    expect(notices).toEqual([]);
    expect(row(c)?.status).toBe("awaiting_user");
    const r = drawn(c)!;
    expect(r.querySelector("[data-role=rsi-note]")?.textContent).toBe(WIKI_COPY.rsiPatchesOff);
  });

  it("a change no longer under review: the backend's plain sentence; offline: the offline sentence, no re-read", async () => {
    const { c, api } = start();
    await c.init();
    await c.discardPatch(CODE.patch_id);
    await c.approvePatch(CODE.patch_id);
    expect(c.state.rsiNote?.note.text).toBe("这项改动不在等你审阅");
    api.failNext("discardPatch", new OfflineError());
    const n = api.calls.length;
    await c.discardPatch(CODE.patch_id);
    expect(methods(api).slice(n)).toEqual(["discardPatch"]);
    expect(c.state.rsiNote?.note.text).toBe(WIKI_COPY.offline);
  });

  it("in the demo nothing is sent", async () => {
    const api = new FakeWikiApi({ rsi: [structuredClone(CODE)] });
    const { c } = setup({ api, demo: true });
    await c.init();
    const n = api.calls.length;
    await c.approvePatch(CODE.patch_id);
    await c.discardPatch(CODE.patch_id);
    expect(api.calls.length).toBe(n);
  });
});
