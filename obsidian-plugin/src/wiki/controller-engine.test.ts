import { describe, expect, it, vi } from "vitest";
import { ApiError, OfflineError } from "../api/errors";
import { WikiController } from "./controller";
import { FakeConn, methods, setup } from "./controller-test-kit";
import { WIKI_COPY } from "./copy";
import { FakeWikiApi } from "./fake";
import { ENGINE_PARAMS, ENGINE_PROMPTS, PROMPT_STEPS } from "./fixtures";

describe("个性化: loadEngine (GET /wiki/engine + GET /wiki/teach/spec + GET /assistant/memory/conflicts)", () => {
  it("reads both (loading first), then the prompts / params and the rules", async () => {
    const { c, api } = setup();
    const p = c.loadEngine();
    expect(c.state.engine.kind).toBe("loading");
    expect(c.state.prefs.kind).toBe("loading");
    await p;
    expect(c.state.engine).toEqual({ kind: "ready", value: { prompts: [...ENGINE_PROMPTS], params: [...ENGINE_PARAMS] } });
    const r = c.state.prefs;
    if (r.kind !== "ready") throw new Error(r.kind);
    expect(r.value.items.map((i) => i.origin)).toEqual(r.value.items.map(() => "default"));
    expect(methods(api).sort()).toEqual(["botActions", "botRecent", "botSettings", "engine", "listSources", "memoryConflicts", "prompts", "rsiSwitch", "teachSpec"]);
  });

  it("showing 个性化 reads them; showing it again re-reads (what is shown stays meanwhile)", async () => {
    const { c, api } = setup();
    c.setTab("engine");
    await vi.waitFor(() => expect(c.state.engine.kind).toBe("ready"));
    await vi.waitFor(() => expect(c.state.prefs.kind).toBe("ready"));
    c.setTab("today");
    const before = methods(api).filter((m) => m === "engine").length;
    c.setTab("engine");
    expect(c.state.engine.kind).toBe("ready");
    await vi.waitFor(() => expect(methods(api).filter((m) => m === "engine").length).toBe(before + 1));
  });

  it("each read fails on its own: its error, the other one lands", async () => {
    const { c, api } = setup();
    api.failNext("engine", new ApiError(500, "boom"));
    await c.loadEngine();
    expect(c.state.engine.kind).toBe("error");
    expect(c.state.prefs.kind).toBe("ready");
    api.failNext("teachSpec", new OfflineError());
    await c.loadEngine();
    expect(c.state.engine.kind).toBe("ready");
    expect(c.state.prefs.kind).toBe("ready"); // a failed re-read keeps what is shown
  });

  it("unconfigured: nothing is sent, both say so", async () => {
    const { c, api } = setup({ configured: false });
    await c.loadEngine();
    expect(c.state.engine).toEqual({ kind: "error", message: WIKI_COPY.needsSettings });
    expect(c.state.prefs).toEqual({ kind: "error", message: WIKI_COPY.needsSettings });
    expect(methods(api)).toEqual([]);
  });

  it("an older read's answer is dropped when a newer one started", async () => {
    const api = new FakeWikiApi({ delayMs: 0 });
    let release: () => void = () => undefined;
    const slow = new Promise<void>((r) => { release = r; });
    const real = api.engine.bind(api);
    let n = 0;
    vi.spyOn(api, "engine").mockImplementation(async () => {
      n += 1;
      const out = await real();
      if (n === 1) { await slow; return { ...out, prompts: [] }; }
      return out;
    });
    const c = new WikiController({ blockEventStore: null, blockProgressStore: null, api, conn: new FakeConn(), sync: null, openNote: () => true, configured: () => true, demo: false });
    const first = c.loadEngine();
    await c.loadEngine();
    release();
    await first;
    const e = c.state.engine;
    if (e.kind !== "ready") throw new Error(e.kind);
    expect(e.value.prompts.length).toBe(ENGINE_PROMPTS.length);
  });

  it("an undone change re-reads 个性化 once it was read (the rules may have changed); not before", async () => {
    const { c, api } = setup();
    await c.init();
    const read = methods(api).filter((m) => m === "teachSpec").length;   // 个性化's dot: the spec read with 今天
    await c.undoPatch("pm1");
    expect(methods(api).filter((m) => m === "teachSpec").length).toBe(read);
    await c.loadEngine();
    const r0 = c.state.prefs;
    if (r0.kind !== "ready") throw new Error(r0.kind);
    await c.undoPatch("pa2");
    await vi.waitFor(() => expect(methods(api).filter((m) => m === "teachSpec").length).toBe(read + 2));
  });

  it("openSettings runs the injected opener (absent: nothing happens)", () => {
    const openSettings = vi.fn();
    const c = new WikiController({ blockEventStore: null, blockProgressStore: null, api: new FakeWikiApi({ delayMs: 0 }), conn: new FakeConn(), sync: null,
      openNote: () => true, configured: () => true, demo: false, openSettings });
    c.openSettings();
    expect(openSettings).toHaveBeenCalledOnce();
    expect(() => setup().c.openSettings()).not.toThrow();
  });
});

describe("提示词设置: savePrompt / resetPrompt", () => {
  it("loadEngine reads the prompts too; the list is the demo's steps, none edited", async () => {
    const { c } = setup();
    await c.loadEngine();
    const p = c.state.prompts;
    if (p.kind !== "ready") throw new Error(p.kind);
    expect(p.value).toHaveLength(PROMPT_STEPS.length);
    expect(p.value.some((x) => x.edited)).toBe(false);
  });

  it("savePrompt: 「已保存。以后这一步按这个写。」 and the step is edited in the list; resetPrompt: 「已恢复默认。」 and the default text", async () => {
    const { c, notices } = setup();
    await c.loadEngine();
    expect(await c.savePrompt("write", "我的写法")).toBe(true);
    expect(notices).toEqual(["已保存。以后这一步按这个写。"]);
    const p = c.state.prompts;
    if (p.kind !== "ready") throw new Error(p.kind);
    expect(p.value.find((x) => x.step === "write")).toMatchObject({ text: "我的写法", edited: true });
    expect(await c.resetPrompt("write")).toBe(PROMPT_STEPS.find((x) => x.step === "write")?.default_text);
    expect(notices[notices.length - 1]).toBe("已恢复默认。");
    expect(c.state.prompts.kind === "ready" && c.state.prompts.value.some((x) => x.edited)).toBe(false);
  });

  it("a 422 shows its reason as a notice and changes nothing", async () => {
    const { c, notices } = setup();
    await c.loadEngine();
    for (const [text, reason] of [
      ["  ", "写法不能是空的。"], ["x".repeat(4001), "写法太长了，最多 4000 个字。"], ["a ⟦b⟧", "写法里不能有 ⟦ 或 ⟧。"],
      ["输出 JSON", "这里只写怎么做；输出的格式是固定的，不能在这里改。"],
    ] as const) {
      expect(await c.savePrompt("write", text)).toBe(false);
      expect(notices[notices.length - 1]).toBe(reason);
    }
    expect(c.state.prompts.kind === "ready" && c.state.prompts.value.some((x) => x.edited)).toBe(false);
  });

  it("offline / unconfigured: the reason as a notice, nothing sent", async () => {
    const { c, api, notices } = setup({ configured: false });
    expect(await c.savePrompt("write", "x")).toBe(false);
    expect(notices).toEqual([WIKI_COPY.needsSettings]);
    expect(methods(api)).toEqual([]);
  });

});
