// The send slot's stop (controller.stopTurn): the composer is free at once, the turn ends on the stream it was already
// followed on, a message sent before that end waits for it, a card that comes out meanwhile still arrives.
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/errors";
import { AssistantController } from "./controller";
import { FakeAssistantApi } from "./fake-api";
import type { AssistantEvent } from "./types";

const ID = { server: "http://x", user: "u" };
const DONE = (stopped: boolean): AssistantEvent => ({ type: "done", message_id: "m", tool_calls: 0, llm_calls: 1, cost_usd: null,
  attachments: [], session_attachments: [], stopped, confirmed: false });
const settle = async (): Promise<void> => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

/** A controller whose first message stays open until `finish`. */
function rig() {
  const api = new FakeAssistantApi();
  let id = 0;
  const ctl = new AssistantController({ api, identity: () => ID, newId: () => `l${++id}`, store: { get: () => null, set: () => Promise.resolve() } });
  ctl.show({ kind: "none" });
  let push: (e: AssistantEvent) => void = () => {};
  let finish: () => void = () => {};
  const open = new Promise<void>((r) => { finish = r; });
  const send = api.sendMessage.bind(api);
  vi.spyOn(api, "sendMessage").mockImplementationOnce(async (_s, _b, onEvent) => { push = onEvent; await open; });
  return { api, ctl, send, push: (e: AssistantEvent) => push(e), finish: () => finish() };
}

const typed = (ctl: AssistantController, text: string): void => { ctl.setDraft(text); };

describe("stopTurn", () => {
  it("a new chat after stop sends independently while the previous turn ends", async () => {
    const r = rig();
    typed(r.ctl, "first");
    const first = r.ctl.send();
    await settle();
    const old = r.ctl.state.conv!;
    await r.ctl.stopTurn();
    await r.ctl.newSession();
    typed(r.ctl, "second");
    const second = r.ctl.send();
    await settle();
    try {
      expect(r.ctl.state.conv).not.toBe(old);
      expect(r.api.calls.sendMessage.map((m) => m.body.text)).toEqual(["second"]);
    } finally {
      r.push(DONE(true)); r.finish(); await first; await second;
    }
  });
  it("frees the composer at once, keeps the text, then the turn ends as stopped", async () => {
    const r = rig();
    typed(r.ctl, "写长一点");
    const sent = r.ctl.send();
    await settle();
    r.push({ type: "text_delta", text: "写到一半", at: 0 });
    expect(r.ctl.state.canStop).toBe(true);
    await r.ctl.stopTurn();
    expect(r.api.stops).toEqual([r.ctl.state.conv!.sessionId]);
    const c = r.ctl.state.conv!;
    expect(r.ctl.state.canStop).toBe(false);
    expect(c.busy).toBe(false);
    expect(c.turns[0]!.stopping).toBe(true);
    expect(c.turns[0]!.thinking).toBe(false);
    r.push({ type: "action", action_id: "a1", kind: "note", scope: "", level: "auto", status: "executed", summary: "", content: {}, page_id: null,
      undoable: false, detail: "" } as unknown as AssistantEvent);     // a tool that was already running
    expect(c.turns[0]!.actions).toHaveLength(1);
    expect(c.turns[0]!.thinking).toBe(false);
    r.push(DONE(true));
    r.finish(); await sent;
    expect(c.turns[0]!.answer).toBe("写到一半");
    expect(c.turns[0]!.stopped).toBe(true);
    expect(c.turns[0]!.status).toBe("done");
  });

  it("a message sent right after it waits for the stopped turn's end, then goes out", async () => {
    const r = rig();
    typed(r.ctl, "写长一点");
    const first = r.ctl.send();
    await settle();
    await r.ctl.stopTurn();
    typed(r.ctl, "短一点");
    const second = r.ctl.send();
    await settle();
    const c = r.ctl.state.conv!;
    expect(c.turns).toHaveLength(2);               // shown at once
    expect(r.api.calls.sendMessage).toHaveLength(0);
    expect(r.ctl.state.canStop).toBe(false);       // its stop comes when it has gone out
    r.push(DONE(true));
    r.finish(); await first;
    await second;
    expect(r.api.calls.sendMessage.map((m) => m.body.text)).toEqual(["短一点"]);
    expect(c.busy).toBe(false);
    expect(c.turns[1]!.status).toBe("done");
  });

  it("asks again while the server has not started the turn yet (404)", async () => {
    vi.useFakeTimers();
    try {
      const r = rig();
      r.api.stopReplies.push(new ApiError(404, "x"));
      typed(r.ctl, "问");
      const sent = r.ctl.send();
      await vi.advanceTimersByTimeAsync(0);
      const stop = r.ctl.stopTurn();
      await vi.advanceTimersByTimeAsync(1000);
      await stop;
      expect(r.api.stops).toHaveLength(2);
      r.push(DONE(true)); r.finish(); await sent;
    } finally { vi.useRealTimers(); }
  });

  it("a stop that did not go through says so and the answer is running again", async () => {
    const r = rig();
    r.api.stopReplies.push(new ApiError(500, "x"));
    typed(r.ctl, "问");
    const sent = r.ctl.send();
    await settle();
    await r.ctl.stopTurn();
    expect(r.ctl.state.canStop).toBe(true);
    expect(r.ctl.state.conv!.turns[0]!.stopping).toBe(false);
    r.push(DONE(false)); r.finish(); await sent;
  });

  it("the stream lost after the stop: stopped, not an error", async () => {
    const r = rig();
    typed(r.ctl, "问");
    const sent = r.ctl.send();
    await settle();
    await r.ctl.stopTurn();
    r.finish(); await sent;                        // the stream ended without done
    const t = r.ctl.state.conv!.turns[0]!;
    expect(t.status).toBe("done");
    expect(t.stopped).toBe(true);
    expect(t.error).toBeNull();
  });
});
