import { afterEach, describe, expect, it, vi } from "vitest";
import { at, rect, settle, sized, viewport } from "../ui/floating-test-kit";
import { cardActions } from "../../test/card-rig";
import { ewNotices } from "../../test/ew-notices";
import { AssistantClient, parseModelsOut } from "./client";
import { ApiError } from "../api/errors";
import { AssistantController, type AssistantState, type Conversation, type HistoryState } from "./controller";
import { FakeAssistantApi } from "./fake-api";
import { type AssistantActions, AssistantRenderer } from "./render";
import type { AssistantSessionStore, Target } from "./session-store";
import type { ModelIn, ModelsOut } from "./types";

const LEVELS = [{ reasoning: "none", name: "关闭" }, { reasoning: "high", name: "高" }] as const;
const MODELS: ModelsOut = { current: "deepseek/deepseek-v4-pro", reasoning: "high", models: [
  { id: "deepseek/deepseek-v4-pro", name: "DeepSeek V4 Pro", provider: "deepseek", provider_name: "DeepSeek", available: true, reason: "", levels: [...LEVELS] },
  { id: "openai/gpt-5.4", name: "GPT-5.4", provider: "openai", provider_name: "OpenAI", available: true, reason: "", levels: [{ reasoning: "medium", name: "中" }] },
  { id: "anthropic/claude-opus-4-6", name: "Claude Opus 4.6", provider: "anthropic", provider_name: "Claude", available: false, reason: "未配置 API key", levels: [] }] };
const view = (open: boolean) => ({ list: MODELS.models, current: MODELS.current, reasoning: MODELS.reasoning, open });

const actions = (): AssistantActions & Record<string, ReturnType<typeof vi.fn>> => ({
  send: vi.fn(), sendEdit: vi.fn(), setDraft: vi.fn(), removeQuote: vi.fn(), retry: vi.fn(), decide: vi.fn(), newSession: vi.fn(), reload: vi.fn(),
  toggleHistory: vi.fn(), searchHistory: vi.fn(), reloadHistory: vi.fn(), resume: vi.fn(), openSource: vi.fn(),
  addFiles: vi.fn(), dropLibrary: vi.fn(), removeAttachment: vi.fn(), toggleModels: vi.fn(), pickModel: vi.fn(), ...cardActions() }) as never;
const hooks = { renderMarkdown: vi.fn(), conceptName: (id: string) => id };
const conv = (over: Partial<Conversation> = {}): Conversation => ({ key: "k", target: { kind: "free" }, sessionId: "s", turns: [], draft: "",
  quote: null, busy: false, attachments: [], restore: "none", recap: null, ...over });
const HIST0: HistoryState = { open: false, q: "", status: "loading", items: [], error: "", notice: "", deleting: [] };
const st = (c: Conversation, over: Partial<AssistantState> = {}): AssistantState =>
  ({ configured: true, active: { kind: "none" }, conv: c, focusTick: 0, history: HIST0, writes: new Map(), canAttach: true, ...over });

afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); });
const buttons = (r: AssistantRenderer) => [...r.root.querySelectorAll<HTMLButtonElement>(".ew-mmenu .ew-mitem")];
const byName = (r: AssistantRenderer, name: string) => buttons(r).find((x) => x.textContent === name)!;

describe("provider, model and reasoning picker", () => {
  it("shows the concrete selection and separates the three choices", () => {
    const a = actions(), r = new AssistantRenderer(a, hooks);
    r.update(st(conv(), { models: view(true) }));
    expect(r.root.querySelector(".ew-mpick")!.textContent).toBe("DeepSeek V4 Pro · 高");
    expect(buttons(r).map((x) => x.textContent)).toEqual(["DeepSeek", "OpenAI", "Claude", "DeepSeek V4 Pro", "关闭", "高"]);
    expect(byName(r, "Claude").disabled).toBe(true);
    expect(r.root.querySelector(".ew-tipbox")!.textContent).toBe("未配置 API key");
    byName(r, "关闭").click();
    expect(a.pickModel).toHaveBeenCalledWith({ reasoning: "none" });
    expect(byName(r, "max")).toBeUndefined();
  });
  it("browses a vendor locally then posts only the concrete model", () => {
    const a = actions(), r = new AssistantRenderer(a, hooks);
    r.update(st(conv(), { models: view(true) }));
    byName(r, "OpenAI").click();
    expect(a.pickModel).not.toHaveBeenCalled();
    expect(buttons(r).map((x) => x.textContent)).toEqual(["DeepSeek", "OpenAI", "Claude", "GPT-5.4"]);
    byName(r, "GPT-5.4").click();
    expect(a.pickModel).toHaveBeenCalledWith({ model: "openai/gpt-5.4" });
    r.update(st(conv(), { models: { ...view(true), current: "openai/gpt-5.4", reasoning: "medium" } }));
    expect(buttons(r).map((x) => x.textContent)).toContain("中");
    expect(buttons(r).map((x) => x.textContent)).not.toContain("关闭");
  });
  it("allows selection with no current model and disables the picker during save", () => {
    const r = new AssistantRenderer(actions(), hooks);
    r.update(st(conv(), { models: { ...view(false), current: null, reasoning: null } }));
    expect(r.root.querySelector<HTMLElement>(".ew-mpickw")!.hidden).toBe(false);
    expect(r.root.querySelector(".ew-mpick")!.textContent).toBe("选择模型");
    r.update(st(conv(), { models: { ...view(false), pending: true } }));
    expect(r.root.querySelector<HTMLButtonElement>(".ew-mpick")!.disabled).toBe(true);
  });
  it("keeps floating placement and keyboard focus", async () => {
    viewport(2160, 1350);
    const a = actions(), r = new AssistantRenderer(a, hooks);
    document.body.append(r.root);
    r.update(st(conv(), { models: view(false) }));
    r.root.querySelector<HTMLButtonElement>(".ew-mpick")!.click();
    r.update(st(conv(), { models: view(true) }));
    at(r.root.querySelector<HTMLElement>(".ew-mpick")!, rect(400, 900, 120, 28));
    const menu = r.root.querySelector<HTMLElement>(".ew-mmenu")!;
    sized(menu, 180, 200); await settle();
    expect([menu.style.position, menu.style.left, menu.style.top]).toEqual(["fixed", "400px", "694px"]);
    expect(document.activeElement).toBe(byName(r, "DeepSeek"));
    byName(r, "DeepSeek").dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement).toBe(byName(r, "OpenAI"));
    (a.toggleModels as ReturnType<typeof vi.fn>).mockImplementation(() => r.update(st(conv(), { models: view(false) })));
    byName(r, "OpenAI").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(document.activeElement).toBe(r.root.querySelector(".ew-mpick"));
  });
});

describe("model selection controller", () => {
  const FREE: Target = { kind: "free" } as never;
  function rig(api: FakeAssistantApi & { listModels?: () => Promise<ModelsOut>; pickModel?: (m: ModelIn) => Promise<ModelsOut> }) {
    const store: AssistantSessionStore = { get: () => null, set: () => Promise.resolve() };
    return new AssistantController({ api, store, identity: () => ({ server: "http://x", user: "u" }), newId: () => "t" });
  }
  const flush = () => new Promise((r) => setTimeout(r, 0));
  it("restores server choice and posts valid model and reasoning without changing conversation", async () => {
    let saved = MODELS;
    const api = Object.assign(new FakeAssistantApi(), {
      listModels: vi.fn(() => Promise.resolve(saved)),
      pickModel: vi.fn((m: ModelIn) => Promise.resolve(saved = { ...saved, current: m.model ?? saved.current, reasoning: m.reasoning ?? saved.reasoning })),
    });
    const ctl = rig(api); ctl.show(FREE); await flush();
    const conversation = ctl.state.conv;
    await ctl.pickModel({ reasoning: "max" });
    await ctl.pickModel({ model: "anthropic/claude-opus-4-6" });
    expect(api.pickModel).not.toHaveBeenCalled();
    await ctl.pickModel({ reasoning: "none" });
    expect(api.pickModel).toHaveBeenCalledWith({ reasoning: "none" });
    expect(ctl.state.models?.reasoning).toBe("none");
    expect(ctl.state.conv).toBe(conversation);
    const reloaded = rig(api); reloaded.show(FREE); await flush();
    expect(reloaded.state.models?.reasoning).toBe("none");
  });
  it("ignores a stale catalog response and blocks overlapping saves", async () => {
    let resolveRead!: (m: ModelsOut) => void, resolvePick!: (m: ModelsOut) => void;
    const api = Object.assign(new FakeAssistantApi(), {
      listModels: vi.fn(() => Promise.resolve(MODELS)),
      pickModel: vi.fn(() => new Promise<ModelsOut>((resolve) => { resolvePick = resolve; })),
    });
    const ctl = rig(api); ctl.show(FREE); await flush();
    api.listModels.mockImplementationOnce(() => new Promise((resolve) => { resolveRead = resolve; }));
    ctl.toggleModels(true);
    const pending = ctl.pickModel({ reasoning: "none" });
    expect(ctl.state.models?.pending).toBe(true);
    await ctl.pickModel({ model: "openai/gpt-5.4" });
    expect(api.pickModel).toHaveBeenCalledTimes(1);
    resolvePick({ ...MODELS, reasoning: "none" }); await pending;
    resolveRead(MODELS); await flush();
    expect(ctl.state.models?.reasoning).toBe("none");
    expect(ctl.state.models?.pending).toBe(false);
  });
  it("preserves the selected model on rejected saves and failed reloads", async () => {
    const api = Object.assign(new FakeAssistantApi(), {
      listModels: vi.fn(() => Promise.resolve(MODELS)),
      pickModel: vi.fn(() => Promise.reject(new ApiError(409, "模型暂不可用"))),
    });
    const ctl = rig(api); ctl.show(FREE); await flush();
    await ctl.pickModel({ model: "openai/gpt-5.4" });
    expect(ctl.state.models?.current).toBe(MODELS.current);
    api.listModels.mockRejectedValueOnce(new Error("down"));
    await ctl.loadModels();
    expect(ctl.state.models?.current).toBe(MODELS.current);
    expect(ewNotices()).toContain("模型列表读取失败，请重试");
    expect(ewNotices()).toContain("模型暂不可用");
  });
});


describe("model selection wire contract", () => {
  it("posts concrete canonical model ids and reasoning, and validates the catalog", async () => {
    const http = { request: vi.fn((_request: import("../api/client").HttpRequest) => Promise.resolve({ status: 200, text: JSON.stringify(MODELS) })) };
    const client = new AssistantClient(() => ({ baseUrl: "http://x", userId: "u" }), http, { stream: vi.fn() });
    await client.pickModel({ model: "openai/gpt-5.4", reasoning: "medium" });
    expect(JSON.parse(http.request.mock.calls[0]![0].body!)).toEqual({ model: "openai/gpt-5.4", reasoning: "medium" });
    expect(parseModelsOut(MODELS)).toEqual(MODELS);
    for (const reasoning of ["minimal", "default"]) {
      expect(parseModelsOut({ ...MODELS, reasoning, models: [{ ...MODELS.models[0], levels: [{ reasoning, name: reasoning }] }] }).reasoning).toBe(reasoning);
    }
    expect(() => parseModelsOut({ ...MODELS, models: [{ ...MODELS.models[0], levels: [{ reasoning: "unsupported", name: "bad" }] }] })).toThrow();
  });
});
