// 删除对话 (full-demo asst.js forgetDialog, the user's mockup of 2026-10-28): the forget calls' contract checks, the client's
// requests, the controller's dialog / 「删除中」 state and the renderer's rows and dialog. No server, no model.
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, OfflineError } from "../api/errors";
import type { HttpRequest, JsonTransport } from "../api/client";
import { AssistantClient } from "./client";
import { AssistantController, type AssistantState, type Conversation, type HistoryState } from "./controller";
import { FakeAssistantApi } from "./fake-api";
import { parseForgetOut, parseForgetPending, parseForgetPreviewOut, usedLine, usedTip, type ForgetPicks } from "./forget";
import { ContractError } from "./parse";
import { type AssistantActions, AssistantRenderer } from "./render";
import { sessionKey, type AssistantSessionStore, type Identity, type Target } from "./session-store";
import type { ForgetOut, ForgetPendingOut, ForgetPreviewOut, SessionSummaryOut } from "./types";

const PREVIEW: ForgetPreviewOut = {
  rules: [{ item_id: "si1", text: "讲概念时先给一个生活里的例子", authority: "user" }],
  facts: [{ key: "k1", section: "我是谁", text: "在学线性代数，目标是看懂 PCA" }],
  tainted_sessions: [{ session_id: "c3", title: "协方差为负是什么意思", turns: 1 }],
  memory_counts: { episodes: 2, items: 1, tainted_turns: 1 },
  materials: [
    { source_id: "src1", title: "线性代数讲义.pdf", in_use_elsewhere: true, used_by: ["page:p1", "session:c1"] },
    { source_id: "src2", title: "截图 2026-10-21.png", in_use_elsewhere: false, used_by: [] },
  ],
  note: "没勾选的偏好会继续生效",
};
const NO_PICKS: ForgetPicks = { revokeRuleIds: [], revokeFactKeys: [], deleteSourceIds: [] };
const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

afterEach(() => { document.querySelectorAll(".ew-layer").forEach((x) => x.remove()); vi.useRealTimers(); });

describe("contract checks", () => {
  it("accept the server's shapes", () => {
    expect(parseForgetPreviewOut(clone(PREVIEW))).toEqual(PREVIEW);
    expect(parseForgetOut({ state: "committed", detail: "" }).state).toBe("committed");
    expect(parseForgetOut({ state: "failed", detail: "删除没有完成，会自动重试" }).state).toBe("failed");
    expect(parseForgetPending({ sessions: [{ session_id: "s9", state: "failed", requested_at: "2026-10-28T09:00:00Z" }] })).toHaveLength(1);
  });
  it("fail closed on anything else", () => {
    expect(() => parseForgetPreviewOut({ ...PREVIEW, rules: "x" })).toThrow(ContractError);
    expect(() => parseForgetPreviewOut({ ...PREVIEW, materials: [{ ...PREVIEW.materials[0], in_use_elsewhere: "yes" }] })).toThrow(ContractError);
    expect(() => parseForgetPreviewOut({ ...PREVIEW, materials: [{ ...PREVIEW.materials[0], used_by: [1] }] })).toThrow(ContractError);
    expect(() => parseForgetOut({ state: "gone", detail: "" })).toThrow(ContractError);
    expect(() => parseForgetPending({ sessions: [{ session_id: "s", state: "done", requested_at: "" }] })).toThrow(ContractError);
  });
  it("where a material is used: counted per kind, conversations named in the ? when known", () => {
    expect(usedLine(["page:p1", "session:c1", "page:p2", "node:n"])).toBe("1 段对话、2 个页面、1 个知识点");
    expect(usedTip(PREVIEW.materials[0]!, (id) => (id === "c1" ? "特征值为什么叫「特征」" : null)))
      .toEqual(["引用此内容的地方", "特征值为什么叫「特征」", "1 个页面"]);
    expect(usedTip({ ...PREVIEW.materials[0]!, used_by: ["session:c1"] }, () => "特征值为什么叫「特征」")).toEqual(["引用此内容的对话", "特征值为什么叫「特征」"]);
    expect(usedTip({ ...PREVIEW.materials[0]!, used_by: ["session:zz"] }, () => null)).toEqual(["引用此内容的对话", "1 段对话"]);
  });
});

describe("client", () => {
  const cfg = () => ({ baseUrl: "http://127.0.0.1:8000", userId: "jerry" });
  const http = (status: number, body: unknown) => {
    const reqs: { method: string; url: string; body?: string }[] = [];
    const t: JsonTransport = { request: vi.fn((r: HttpRequest) => { reqs.push(r); return Promise.resolve({ status, text: JSON.stringify(body) }); }) };
    return { t, reqs };
  };
  const none = { stream: () => Promise.resolve({ status: 200, text: "" }) };
  it("GET …/forget-preview, DELETE with the ticked ids, GET /assistant/forget-requests", async () => {
    const a = http(200, PREVIEW);
    expect(await new AssistantClient(cfg, a.t, none).forgetPreview("s/1")).toEqual(PREVIEW);
    expect(a.reqs[0]).toMatchObject({ method: "GET", url: "http://127.0.0.1:8000/assistant/sessions/s%2F1/forget-preview" });
    const b = http(202, { state: "failed", detail: "删除没有完成，会自动重试" });
    const out = await new AssistantClient(cfg, b.t, none).deleteSession("s1", { revokeRuleIds: ["si1"], revokeFactKeys: [], deleteSourceIds: ["src2"] });
    expect(out.state).toBe("failed");
    expect(b.reqs[0]).toMatchObject({ method: "DELETE", url: "http://127.0.0.1:8000/assistant/sessions/s1" });
    expect(JSON.parse(b.reqs[0]!.body!)).toEqual({ revoke_rule_ids: ["si1"], revoke_fact_keys: [], delete_source_ids: ["src2"] });
    const c = http(200, { sessions: [] });
    expect(await new AssistantClient(cfg, c.t, none).forgetRequests()).toEqual([]);
    expect(c.reqs[0]!.url).toBe("http://127.0.0.1:8000/assistant/forget-requests");
  });
  it("a 503 is an ApiError carrying the server's sentence", async () => {
    const a = http(503, { detail: "删除没完成，请再试一次" });
    const e = await new AssistantClient(cfg, a.t, none).deleteSession("s1", NO_PICKS).catch((x: unknown) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect((e as ApiError).detail).toBe("删除没完成，请再试一次");
  });
});

class ForgetApi extends FakeAssistantApi {
  preview: ForgetPreviewOut | Error = clone(PREVIEW);
  answer: ForgetOut | Error = { state: "committed", detail: "" };
  pending: ForgetPendingOut[] = [];
  deletes: { sessionId: string; picks: ForgetPicks }[] = [];
  forgetPreview(): Promise<ForgetPreviewOut> { return this.preview instanceof Error ? Promise.reject(this.preview) : Promise.resolve(clone(this.preview)); }
  deleteSession(sessionId: string, picks: ForgetPicks): Promise<ForgetOut> {
    this.deletes.push({ sessionId, picks: clone(picks) });
    return this.answer instanceof Error ? Promise.reject(this.answer) : Promise.resolve(this.answer);
  }
  forgetRequests(): Promise<ForgetPendingOut[]> { return Promise.resolve(clone(this.pending)); }
}
const NOTE: Target = { kind: "note", path: "ML/PCA.md", conceptId: "pca", title: "PCA" };
const JERRY: Identity = { server: "http://127.0.0.1:8000", user: "jerry" };
const ROW = (id: string, title: string, at = "2026-10-28T10:05:00Z"): SessionSummaryOut => ({ session_id: id, title, page_id: null, page_title: "均值和方差",
  note_concept_id: null, created_at: at, last_activity_at: at, message_count: 2 });
const flush = () => new Promise((r) => setTimeout(r, 0));
const notices = () => [...document.querySelectorAll(".ew-notice")].map((n) => n.textContent);

function rig(stored: Record<string, string> = {}, memoryForgotten?: () => void) {
  const api = new ForgetApi(); const mem = { ...stored };
  const store: AssistantSessionStore = { get: (k) => mem[k] ?? null, set: (k, v) => { delete mem[k]; if (v) mem[k] = v; return Promise.resolve(); } };
  const ctl = new AssistantController({ api, store, identity: () => JERRY, newId: () => "t", ...(memoryForgotten ? { memoryForgotten } : {}) });
  api.sessionList = [ROW("c2", "为什么方差要平方而不是取绝对值"), ROW("c3", "协方差为负是什么意思", "2026-10-27T21:40:00Z")];
  return { api, mem, ctl };
}

describe("controller", () => {
  it("the list leaves out the conversations being deleted and keeps them as 「删除中」 rows with the title seen before", async () => {
    const { api, ctl } = rig();
    ctl.show(NOTE); ctl.toggleHistory(); await flush();
    api.pending = [{ session_id: "c3", state: "deleting", requested_at: "2026-10-28T11:00:00Z" }, { session_id: "zz", state: "failed", requested_at: "2026-10-28T11:00:00Z" }];
    await ctl.loadHistory();
    const h = ctl.state.history;
    expect(h.items.map((x) => x.session_id)).toEqual(["c2", "c3"].filter((x) => x !== "c3"));
    expect(h.deleting).toEqual([
      { session_id: "c3", state: "deleting", requested_at: "2026-10-28T11:00:00Z", title: "协方差为负是什么意思", at: "2026-10-27T21:40:00Z" },
      { session_id: "zz", state: "failed", requested_at: "2026-10-28T11:00:00Z", title: "", at: "2026-10-28T11:00:00Z" }]);
  });
  it("opens the dialog with nothing ticked, ticks and unticks, 取消 sends nothing", async () => {
    const { api, ctl } = rig();
    ctl.show(NOTE); ctl.toggleHistory(); await flush();
    const opening = ctl.openForget("c2");
    expect(ctl.state.forget).toMatchObject({ sessionId: "c2", title: "为什么方差要平方而不是取绝对值", preview: null, busy: false });
    await opening;
    expect(ctl.state.forget?.preview).toEqual(PREVIEW);
    expect(ctl.state.forget?.picks).toEqual(NO_PICKS);
    ctl.pickForget("rule", "si1", true); ctl.pickForget("source", "src2", true); ctl.pickForget("source", "src2", false); ctl.pickForget("fact", "k1", true);
    expect(ctl.state.forget?.picks).toEqual({ revokeRuleIds: ["si1"], revokeFactKeys: ["k1"], deleteSourceIds: [] });
    ctl.closeForget();
    expect(ctl.state.forget).toBeNull();
    expect(api.deletes).toEqual([]);
  });
  it("删除对话 (200): one DELETE with the ticks, 「已删除」, the conversation showing it is emptied and its stored id cleared", async () => {
    const { api, mem, ctl } = rig({ [sessionKey(JERRY, NOTE)]: "c2" });
    api.sessionOuts.set("c2", { gone_sources: [], session_id: "c2", page_id: null, recap: null, messages: [
      { message_id: "m1", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "为什么方差要平方而不是取绝对值？", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null },
    ], decisions: [], proposals: [], actions: [] });
    ctl.show(NOTE); await flush();
    ctl.toggleHistory(); await flush();
    await ctl.openForget("c2");
    ctl.pickForget("rule", "si1", true);
    await ctl.confirmForget();
    expect(api.deletes).toEqual([{ sessionId: "c2", picks: { revokeRuleIds: ["si1"], revokeFactKeys: [], deleteSourceIds: [] } }]);
    expect(ctl.state.forget).toBeNull();
    expect(notices()).toContain("已删除");
    expect(api.calls.getSession).toEqual(["c2"]);             // it was showing c2
    expect(ctl.state.conv?.sessionId).toBeNull();
    expect(ctl.state.conv?.turns).toEqual([]);
    expect(Object.values(mem)).not.toContain("c2");
  });
  it("删除对话 (202): 「删除还没完成，会自动继续」 and the list is read again (its row then shows 删除中)", async () => {
    const { api, ctl } = rig();
    ctl.show(NOTE); ctl.toggleHistory(); await flush();
    await ctl.openForget("c3");
    api.answer = { state: "failed", detail: "删除没有完成，会自动重试" };
    api.pending = [{ session_id: "c3", state: "requested", requested_at: "2026-10-28T11:00:00Z" }];
    await ctl.confirmForget(); await flush();
    expect(notices()).toContain("删除还没完成，会自动继续");
    expect(ctl.state.history.deleting.map((d) => [d.session_id, d.title])).toEqual([["c3", "协方差为负是什么意思"]]);
    expect(ctl.state.history.items.map((x) => x.session_id)).toEqual(["c2"]);
  });
  it("a refused delete keeps the dialog open (nothing changed) and says what to do; offline says to check the connection", async () => {
    const { api, ctl } = rig();
    ctl.show(NOTE); ctl.toggleHistory(); await flush();
    await ctl.openForget("c2");
    api.answer = new ApiError(503, "删除没完成，请再试一次");
    await ctl.confirmForget();
    expect(ctl.state.forget).toMatchObject({ sessionId: "c2", busy: false });
    expect(notices()).toContain("删除没完成，请再试一次");
    api.answer = new OfflineError(new Error("x"));
    await ctl.confirmForget();
    expect(notices()).toContain("未连接到服务器");
  });
  it("a preview that cannot be read closes the dialog and says so (never a dialog that lists nothing)", async () => {
    const { api, ctl } = rig();
    api.preview = new ApiError(500, "boom");
    ctl.show(NOTE); ctl.toggleHistory(); await flush();
    await ctl.openForget("c2");
    expect(ctl.state.forget).toBeNull();
    expect(notices()).toContain("没能读到，重试");
  });
  it("删除对话 does nothing until the preview is read", async () => {
    const { api, ctl } = rig();
    ctl.show(NOTE); ctl.toggleHistory(); await flush();
    void ctl.openForget("c2");
    await ctl.confirmForget();
    expect(api.deletes).toEqual([]);
  });
});

describe("renderer (asst.js historyView / convMenu / forgetDialog)", () => {
  const actions = (): AssistantActions & Record<string, ReturnType<typeof vi.fn>> => ({
    send: vi.fn(), sendEdit: vi.fn(), setDraft: vi.fn(), removeQuote: vi.fn(), retry: vi.fn(), decide: vi.fn(), newSession: vi.fn(), reload: vi.fn(),
    toggleHistory: vi.fn(), searchHistory: vi.fn(), reloadHistory: vi.fn(), resume: vi.fn(), openSource: vi.fn(), addFiles: vi.fn(),
    dropLibrary: vi.fn(), removeAttachment: vi.fn(), confirmAction: vi.fn(), cancelAction: vi.fn(), undoAction: vi.fn(), moveDraft: vi.fn(),
    removeDraft: vi.fn(), addDraft: vi.fn(), showPage: vi.fn(), showInParams: vi.fn(), answerQuiz: vi.fn(), nextQuiz: vi.fn(), rate: vi.fn(),
    openForget: vi.fn(), pickForget: vi.fn(), closeForget: vi.fn(), confirmForget: vi.fn() }) as never;
  const NOW = new Date(2026, 9, 28, 20, 0);
  const local = (d: number, h: number, mi: number) => new Date(2026, 9, d, h, mi).toISOString();
  const hooks = { renderMarkdown: vi.fn(), conceptName: (id: string) => id, now: () => NOW };
  const conv: Conversation = { key: "k", target: NOTE, sessionId: null, turns: [], draft: "", quote: null, busy: false, attachments: [], restore: "none", recap: null };
  const HIST: HistoryState = { open: true, q: "", status: "ready", items: [ROW("c2", "为什么方差要平方而不是取绝对值"), ROW("c1", "特征值为什么叫「特征」", "2026-10-28T07:20:00Z")],
    error: "", notice: "", deleting: [] };
  const st = (over: Partial<AssistantState> = {}): AssistantState =>
    ({ configured: true, active: NOTE, conv, focusTick: 0, history: HIST, writes: new Map(), canAttach: true, ...over });
  const mount = (act = actions()) => { const r = new AssistantRenderer(act, hooks); document.body.append(r.root); return { r, act }; };
  const layer = () => document.querySelector<HTMLElement>("body > .ew.ew-layer .ew-modal.ew-fg");

  it("each row is .ew-hitem > row button + ⋯; ⋯ opens a menu whose one item, 删除, opens the dialog", () => {
    const { r, act } = mount();
    r.update(st());
    const item = r.root.querySelector(".ew-hitem")!;
    expect([...item.children].map((c) => c.className)).toEqual(["ew-hrow", "ew-more"]);
    const more = item.querySelector<HTMLButtonElement>(".ew-more")!;
    expect([more.textContent, more.getAttribute("aria-label"), more.getAttribute("aria-haspopup")]).toEqual(["⋯", "这个对话的操作", "menu"]);
    more.click();
    expect(more.getAttribute("aria-expanded")).toBe("true");
    const menu = item.querySelector(".ew-menu")!;
    expect([...menu.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["删除"]);
    r.update(st());                                        // an unrelated update keeps the menu
    expect(item.querySelector(".ew-menu")).not.toBeNull();
    menu.querySelector("button")!.click();
    expect(act.openForget).toHaveBeenCalledWith("c2");
    expect(item.querySelector(".ew-menu")).toBeNull();
    expect(act.resume).not.toHaveBeenCalled();
    r.root.remove();
  });
  it("a conversation being deleted: greyed, 删除中 (or 删除没完成，会自动重试), disabled, no ⋯; without a known title 「一个对话」", () => {
    const { r } = mount();
    r.update(st({ history: { ...HIST, items: [HIST.items[0]!], deleting: [
      { session_id: "c3", state: "deleting", requested_at: "", title: "协方差为负是什么意思", at: local(27, 21, 40) },
      { session_id: "zz", state: "failed", requested_at: "", title: "", at: local(26, 10, 0) }] } }));
    const del = [...r.root.querySelectorAll<HTMLButtonElement>(".ew-hrow.ew-del")];
    expect(del.map((b) => [b.disabled, b.textContent])).toEqual([[true, "协方差为负是什么意思删除中"], [true, "一个对话删除没完成，会自动重试"]]);
    expect(del[0]!.querySelector(".ew-hs > .ew-ws > i")).not.toBeNull();
    expect(del[0]!.parentElement!.querySelector(".ew-more")).toBeNull();
    expect([...r.root.querySelectorAll(".ew-hgroup")].map((g) => g.textContent)).toEqual(["今天", "昨天", "更早"]);
    r.update(st({ history: { ...HIST, q: "方差", deleting: [{ session_id: "c3", state: "deleting", requested_at: "", title: "协方差", at: "" }] } }));
    expect(r.root.querySelector(".ew-hrow.ew-del")).toBeNull();      // search results: the server's hits only
    r.root.remove();
  });
  it("the dialog is the mockup: trash + 删除这段对话？, the title, what stays, two folding sections, 取消 + 删除对话", () => {
    const { r, act } = mount();
    r.update(st({ forget: { sessionId: "c2", title: "为什么方差要平方而不是取绝对值", preview: clone(PREVIEW), picks: NO_PICKS, busy: false } }));
    const m = layer()!;
    expect(m.getAttribute("role")).toBe("dialog");
    expect([...m.children].map((c) => c.className)).toEqual(["ew-fg-head", "ew-fg-title", "ew-fg-desc", "ew-fg-hr", "ew-fg-sec", "ew-fg-hr", "ew-fg-sec", "ew-fg-ops"]);
    expect(m.querySelector(".ew-fg-head .ew-fg-ic svg")).not.toBeNull();
    expect(m.querySelector(".ew-fg-head h2")!.textContent).toBe("删除这段对话？");
    expect(m.querySelector(".ew-fg-title")!.textContent).toBe("为什么方差要平方而不是取绝对值");
    expect(m.querySelector(".ew-fg-desc")!.textContent).toBe("删除后，这段对话及其派生的历史记忆将被清除，无法恢复。已保存的学习偏好、学习背景和独立资料不会受到影响。");
    expect(m.querySelector(".ew-fg-desc b")!.textContent).toBe("不会受到影响。");
    const secs = [...m.querySelectorAll(".ew-fg-sec")];
    expect(secs.map((s) => [s.querySelector(".ew-fg-st")!.textContent, s.querySelector(".ew-fg-ss")!.textContent])).toEqual([
      ["同时删除已保存的学习偏好与背景", "默认不删除。如需一并删除，可勾选并选择具体内容。"],
      ["同时删除这段对话用过的资料", "默认不删除。如需一并删除，可勾选并选择具体内容。"]]);
    expect(secs.some((s) => s.querySelector(".ew-fg-sh input"))).toBe(false);     // no box on the section heading (the user's ruling)
    const rows = [...m.querySelectorAll(".ew-fg-item")];
    expect(rows.map((x) => [x.querySelector(".ew-fg-it")!.textContent, x.querySelector(".ew-fg-ik")!.firstChild!.textContent])).toEqual([
      ["讲概念时先给一个生活里的例子", "学习偏好"], ["在学线性代数，目标是看懂 PCA", "学习背景"],
      ["线性代数讲义.pdf", "另有 1 段对话、1 个页面引用了此内容"], ["截图 2026-10-21.png", "资料"]]);
    expect(rows.every((x) => !(x.querySelector("input") as HTMLInputElement).checked)).toBe(true);
    expect(rows[2]!.querySelector(".ew-fg-used .ew-qtip .ew-tipbox")!.textContent).toBe("引用此内容的地方特征值为什么叫「特征」1 个页面");
    const box = (i: number) => rows[i]!.querySelector("input") as HTMLInputElement;
    box(0).click(); box(1).click(); box(3).click();
    expect(vi.mocked(act.pickForget!).mock.calls).toEqual([["rule", "si1", true], ["fact", "k1", true], ["source", "src2", true]]);
    const sh = secs[0]!.querySelector<HTMLButtonElement>(".ew-fg-sh")!;
    expect(sh.getAttribute("aria-expanded")).toBe("true");
    sh.click();
    expect([sh.getAttribute("aria-expanded"), secs[0]!.querySelector<HTMLElement>(".ew-fg-box")!.hidden]).toEqual(["false", true]);
    const [no, yes] = [...m.querySelectorAll<HTMLButtonElement>(".ew-fg-ops .ew-btn")];
    expect([no!.textContent, yes!.textContent, yes!.className]).toEqual(["取消", "删除对话", "ew-btn ew-fg-yes"]);
    yes!.click(); expect(act.confirmForget).toHaveBeenCalledTimes(1);
    no!.click(); expect(act.closeForget).toHaveBeenCalledTimes(1);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(act.closeForget).toHaveBeenCalledTimes(2);
    (m.parentElement as HTMLElement).click();              // the scrim
    expect(act.closeForget).toHaveBeenCalledTimes(3);
    r.update(st({ forget: null }));
    expect(layer()).toBeNull();
    r.root.remove();
  });
  it("only what the conversation left: no sections when it left nothing; 删除对话 waits for the read and for its answer", () => {
    const { r } = mount();
    const base = { sessionId: "c2", title: "t", picks: NO_PICKS, busy: false };
    r.update(st({ forget: { ...base, preview: null } }));
    const yes = () => layer()!.querySelector<HTMLButtonElement>(".ew-fg-yes")!;
    expect(yes().disabled).toBe(true);
    r.update(st({ forget: { ...base, preview: { ...clone(PREVIEW), rules: [], facts: [], materials: [] } } }));
    expect(layer()!.querySelector(".ew-fg-sec")).toBeNull();
    expect(yes().disabled).toBe(false);
    r.update(st({ forget: { ...base, busy: true, preview: { ...clone(PREVIEW), rules: [], facts: [], materials: [] } } }));
    expect(yes().disabled).toBe(true);
    r.root.remove();
  });
  it("a panel not on screen draws no dialog (one dialog when two panels show the same controller)", () => {
    const r = new AssistantRenderer(actions(), hooks);       // never mounted
    r.update(st({ forget: { sessionId: "c2", title: "t", preview: clone(PREVIEW), picks: NO_PICKS, busy: false } }));
    expect(layer()).toBeNull();
  });
});


it("reconciles pending interactive evidence after a confirmed forget and a pending deletion completes", async () => {
  const reconcile = vi.fn();
  const { api, ctl } = rig({}, reconcile);
  ctl.show(NOTE); ctl.toggleHistory(); await flush();
  await ctl.openForget("c2");
  await ctl.confirmForget();
  expect(reconcile).toHaveBeenCalledTimes(1);
  api.pending = [{ session_id: "c3", state: "deleting", requested_at: "2026-10-28T11:00:00Z" }];
  await ctl.loadHistory();
  expect(reconcile).toHaveBeenCalledTimes(1);
  api.pending = [];
  api.sessionList = [];
  await ctl.loadHistory();
  expect(reconcile).toHaveBeenCalledTimes(2);
});
