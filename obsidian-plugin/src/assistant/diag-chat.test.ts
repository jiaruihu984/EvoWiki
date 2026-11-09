// 学点新的's diagnosis in the chat: the 摸底 grid (ask_rough) and the 细问 cards (ask_check with block_id / block_name), on
// master's ask model (the turn ends on the question; his next message answers it).
import { describe, expect, it } from "vitest";
import { AssistantController } from "./controller";
import { FakeAssistantApi } from "./fake-api";
import { AssistantRenderer, type AssistantActions } from "./render";
import type { AssistantSessionStore, Identity, Target } from "./session-store";
import { cardActions } from "../../test/card-rig";
import type { AskEvent, AssistantEvent } from "./types";

const NOTE: Target = { kind: "note", path: "ML/PCA.md", conceptId: "pca", title: "PCA" };
const JERRY: Identity = { server: "http://127.0.0.1:8000", user: "jerry" };
const done = (id: string): AssistantEvent =>
  ({ type: "done", message_id: id, tool_calls: 1, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false });
const ROUGH: AskEvent = { type: "ask", call_id: "r1", kind: "ask_rough",
  payload: { draft_id: "d1", blocks: [{ id: "id", name: "分类与鉴别" }, { id: "wq", name: "水质与环境" }] } };
const CHECK: AskEvent = { type: "ask", call_id: "k1", kind: "ask_check",
  payload: { question: "新开的缸第二天就放虾，最可能出什么问题？", options: ["氨氮升高，虾中毒", "水太干净", "不知道"], block_id: "wq", block_name: "水质与环境" } };

function rig(api: FakeAssistantApi) {
  const store: AssistantSessionStore = { get: () => null, set: () => Promise.resolve() };
  let n = 0;
  const ctl = new AssistantController({ api, store, identity: () => JERRY, newId: () => `t${++n}` });
  const f = (): (() => void) => () => {};
  const actions = { send: f(), sendEdit: f(), setDraft: f(), removeQuote: f(), retry: f(), decide: f(), newSession: f(), reload: f(),
    toggleHistory: f(), searchHistory: f(), reloadHistory: f(), resume: f(), openSource: f(), addFiles: f(), dropLibrary: f(),
    removeAttachment: f(), ...cardActions(), rate: (id: string, next: Parameters<AssistantController["rate"]>[1]) => void ctl.rate(id, next),
    answerAsk: (id: string, t: string) => void ctl.answerAsk(id, t), markRough: (id: string, b: string, v: "know" | "partly" | "dont") => { ctl.markRough(id, b, v); },
    submitRough: (id: string) => void ctl.submitRough(id) } as unknown as AssistantActions;
  const r = new AssistantRenderer(actions, { renderMarkdown: () => {}, conceptName: (id: string) => id });
  document.body.append(r.root);
  const draw = (): void => { if (ctl.state.conv) r.update(ctl.state); };
  ctl.subscribe(draw);
  return { ctl, host: r.root, draw };
}

describe("学点新的's diagnosis in the chat", () => {
  it("摸底: the grid; a tap is 学点新的's at once (diag); 提交 sends his line with the marks as reply_data; then the squares", async () => {
    const api = new FakeAssistantApi();
    api.messageReplies.push({ events: [ROUGH, done("m1")] });
    const { ctl, host, draw } = rig(api);
    ctl.show(NOTE); ctl.setDraft("确认"); await ctl.send(); draw();
    expect(host.querySelectorAll(".ew-dg-mat .ew-dg-row").length).toBe(3);
    expect(ctl.diag()).toEqual({ asking: null, marks: {}, open: true });
    host.querySelector<HTMLButtonElement>('[aria-label="分类与鉴别：掌握"]')!.click();
    host.querySelector<HTMLButtonElement>('[aria-label="水质与环境：不会"]')!.click();
    expect(ctl.diag()).toEqual({ asking: null, marks: { id: "know", wq: "dont" }, open: true });
    api.messageReplies.push({ events: [CHECK, done("m2")] });
    host.querySelector<HTMLButtonElement>(".ew-dg-btn.ew-pri")!.click();
    await new Promise((r) => setTimeout(r, 0)); await new Promise((r) => setTimeout(r, 0));
    const sent = api.calls.sendMessage[1]!.body;
    expect([sent.text, sent.reply_data]).toEqual(["分类与鉴别 掌握、水质与环境 不会", { marks: { id: "know", wq: "dont" } }]);
    draw();
    expect(host.querySelector(".ew-dg-mini .ew-dg-mt")!.textContent).toBe("已提交");
    // 细问: the block, the question, its options; the block is 正在问 in 学点新的
    expect(ctl.diag()).toEqual({ asking: "wq", marks: { id: "know", wq: "dont" }, open: false });
    const q = [...host.querySelectorAll(".ew-dg-q")].pop()!;
    expect([q.querySelector(".ew-dg-qk")!.textContent, q.querySelectorAll(".ew-dg-ob").length]).toEqual(["水质与环境", 3]);
    expect(q.querySelector(".ew-dg-foot .ew-arow")).not.toBeNull();       // the chat's own 👍 / 👎 row
    api.messageReplies.push({ events: [{ type: "text_delta", text: "从「水质与环境」开始，第一页在写。", at: 0 }, done("m3")] });
    q.querySelector<HTMLButtonElement>(".ew-dg-ob")!.click();
    await new Promise((r) => setTimeout(r, 0)); await new Promise((r) => setTimeout(r, 0));
    expect(api.calls.sendMessage[2]!.body.text).toBe("氨氮升高，虾中毒");
    expect(ctl.diag()).toEqual({ asking: null, marks: { id: "know", wq: "dont" }, open: false });   // sent: shown only where the map has no band
  });

  it("👎 → 我说过不会 on the waiting 细问 answers it 不知道 (the block is 不会) and the next question follows", async () => {
    const api = new FakeAssistantApi();
    api.messageReplies.push({ events: [CHECK, done("m1")] });
    const { ctl, host, draw } = rig(api);
    ctl.show(NOTE); ctl.setDraft("确认"); await ctl.send(); draw();
    const q = host.querySelector(".ew-dg-q")!;
    const thumbs = [...q.querySelectorAll<HTMLButtonElement>(".ew-ib")];
    thumbs[thumbs.length - 1]!.click(); draw();
    api.messageReplies.push({ events: [{ type: "text_delta", text: "好。", at: 0 }, done("m2")] });
    [...host.querySelectorAll<HTMLButtonElement>(".ew-dg-q .ew-pill")].find((p) => p.textContent === "我说过不会")!.click();
    await new Promise((r) => setTimeout(r, 0)); await new Promise((r) => setTimeout(r, 0));
    expect(api.calls.sendMessage.map((c) => c.body.text)).toEqual(["确认", "不知道"]);
  });
});
