import fc from "fast-check";
import { expect, it, vi } from "vitest";
import { OfflineError } from "../api/errors";
import { AssistantController } from "./controller";
import { FakeAssistantApi } from "./fake-api";
import type { AssistantEvent, SessionOut } from "./types";

const settle = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
const identity = { server: "http://model", user: "u" };
const target = (n: number) => ({ kind: "note" as const, path: `${n}.md`, conceptId: `${n}`, title: `${n}` });
type Phase = "idle" | "running" | "ending" | "queued";
type Turn = { question: string; status: "streaming" | "done" | "error"; stopping: boolean };
type Conv = { phase: Phase; turns: Turn[]; active: string | null; queued: string | null };
type Model = { current: number; convs: Map<number, Conv>; streams: Map<string, Conv>; sent: number; sequence: number };
type Wire = { push: (e: AssistantEvent) => void; finish: () => void };
type Real = { ctl: AssistantController; api: FakeAssistantApi; wires: Map<string, Wire>; requests: Promise<void>[] };
const empty = (): Conv => ({ phase: "idle", turns: [], active: null, queued: null });

function setup(): { model: Model; real: Real } {
  const api = new FakeAssistantApi();
  const memory = new Map<string, string>();
  let id = 0;
  const ctl = new AssistantController({ api, identity: () => identity, newId: () => `t${++id}`,
    store: { get: (key) => memory.get(key) ?? null, set: (key, value) => {
      if (value === null) memory.delete(key); else memory.set(key, value);
      return Promise.resolve();
    } } });
  const real: Real = { ctl, api, wires: new Map(), requests: [] };
  api.sendMessage = async (sid, body, push) => {
    api.calls.sendMessage.push({ sessionId: sid, body });
    await new Promise<void>((finish) => real.wires.set(body.text!, { push, finish }));
  };
  ctl.show(target(0));
  return { model: { current: 0, convs: new Map([[0, empty()]]), streams: new Map(), sent: 0, sequence: 0 }, real };
}

function assertState(m: Model, r: Real) {
  const c = m.convs.get(m.current)!;
  expect(r.ctl.state.conv?.turns.map((t) => ({ question: t.question, status: t.status, stopping: t.stopping }))).toEqual(c.turns);
  expect(r.ctl.state.conv?.busy).toBe(c.phase === "running" || c.phase === "queued");
  expect(r.ctl.state.canStop).toBe(c.phase === "running");
  expect(r.api.calls.sendMessage).toHaveLength(m.sent);
}

type Operation = "send" | "stop" | "stopFails" | "raceStop" | "finish" | "disconnect" | "switch" | "new" | "restore" | "replay";
const coverage = new Map<string, number>();
const effective = new Map<string, number>();
class Command implements fc.AsyncCommand<Model, Real> {
  constructor(readonly operation: Operation, readonly choice: number) {}
  check() { return true; } // Invalid clicks are exercised as no-ops, never filtered out.
  async run(m: Model, r: Real) {
    coverage.set(this.operation, (coverage.get(this.operation) ?? 0) + 1);
    let c = m.convs.get(m.current)!;
    const acts = this.operation === "send" ? c.phase === "idle" || c.phase === "ending"
      : ["stop", "stopFails", "raceStop"].includes(this.operation) ? c.phase === "running"
      : ["finish", "disconnect", "replay"].includes(this.operation) ? m.streams.size > 0 : true;
    if (acts) effective.set(this.operation, (effective.get(this.operation) ?? 0) + 1);
    switch (this.operation) {
      case "send": {
        const question = `q${++m.sequence}`;
        r.ctl.setDraft(question);
        r.requests.push(r.ctl.send());
        if (c.phase === "idle" || c.phase === "ending") {
          c.turns.push({ question, status: "streaming", stopping: false });
          if (c.phase === "ending") { c.phase = "queued"; c.queued = question; }
          else { c.phase = "running"; c.active = question; m.streams.set(question, c); m.sent++; }
        }
        break;
      }
      case "stop":
      case "stopFails": {
        if (c.phase === "running") {
          if (this.operation === "stopFails") r.api.stopReplies.push(new OfflineError());
          else { c.phase = "ending"; c.turns.find((t) => t.question === c.active)!.stopping = true; }
        }
        await r.ctl.stopTurn();
        break;
      }
      case "raceStop": {
        if (c.phase !== "running") { await r.ctl.stopTurn(); break; }
        const question = c.active!;
        const prior = r.api.stopTurn.bind(r.api);
        let resolve!: () => void;
        let reject!: (error: Error) => void;
        r.api.stopTurn = () => new Promise<void>((yes, no) => { resolve = yes; reject = no; });
        const stopping = r.ctl.stopTurn();
        await settle();
        const wire = r.wires.get(question)!;
        wire.push({ type: "done", message_id: question, tool_calls: 0, llm_calls: 0,
          cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false });
        wire.finish(); r.wires.delete(question); m.streams.delete(question);
        await settle();
        if (this.choice % 2) reject(new OfflineError()); else resolve();
        await stopping;
        r.api.stopTurn = prior;
        const turn = c.turns.find((t) => t.question === question)!;
        turn.status = "done"; turn.stopping = false; c.phase = "idle"; c.active = null;
        break;
      }
      case "finish":
      case "disconnect": {
        const keys = [...m.streams.keys()];
        const question = keys[this.choice % Math.max(1, keys.length)];
        if (question === undefined) break;
        c = m.streams.get(question)!;
        const turn = c.turns.find((t) => t.question === question)!;
        const wire = r.wires.get(question)!;
        if (this.operation === "finish") wire.push({ type: "done", message_id: question, tool_calls: 0, llm_calls: 0,
          cost_usd: null, attachments: [], session_attachments: [], stopped: turn.stopping, confirmed: false });
        wire.finish(); r.wires.delete(question); m.streams.delete(question);
        turn.status = this.operation === "finish" || turn.stopping ? "done" : "error";
        turn.stopping = false;
        if (c.queued !== null) {
          c.active = c.queued; c.queued = null; c.phase = "running"; m.streams.set(c.active, c); m.sent++;
        } else { c.active = null; c.phase = "idle"; }
        break;
      }
      case "switch":
        m.current = this.choice % 2;
        if (!m.convs.has(m.current)) m.convs.set(m.current, empty());
        r.ctl.show(target(m.current));
        break;
      case "new":
        await r.ctl.newSession();
        m.convs.set(m.current, empty());
        break;
      case "restore": {
        const sid = `restored${++m.sequence}`;
        const out: SessionOut = { session_id: sid, page_id: null, messages: [], decisions: [], proposals: [], actions: [], gone_sources: [], recap: null };
        r.api.sessionOuts.set(sid, out);
        expect(await r.ctl.resume(sid)).toBe("ok");
        m.convs.set(m.current, empty());
        break;
      }
      case "replay": {
        const keys = [...m.streams.keys()];
        const question = keys[this.choice % Math.max(1, keys.length)];
        if (question !== undefined) {
          const wire = r.wires.get(question)!;
          wire.push({ type: "text_delta", text: "answer", at: 0 });
          wire.push({ type: "text_delta", text: "answer", at: 0 });
          const actual = r.ctl.state.conv?.turns.find((t) => t.question === question);
          if (actual) expect(actual.answer).toBe("answer");
        }
      }
    }
    await settle();
    assertState(m, r);
  }
  toString() { return `${this.operation}(${this.choice})`; }
}

it("models send/stop/refusal/completion/replay/switch/new/restore without cross-chat callbacks", async () => {
  const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
  const operations: Operation[] = ["send", "send", "stop", "stopFails", "raceStop", "finish", "disconnect", "switch", "new", "restore", "replay"];
  try {
    await fc.assert(fc.asyncProperty(fc.commands([fc.tuple(fc.constantFrom(...operations), fc.nat(20)).map(([op, n]) => new Command(op, n))], { maxCommands: 50 }), async (commands) => {
      const state = setup();
      try { await fc.asyncModelRun(() => state, commands); }
      finally {
        for (let round = 0; round < 3; round++) { for (const wire of state.real.wires.values()) wire.finish(); await settle(); }
        await Promise.all(state.real.requests);
        state.real.ctl.dispose();
      }
    }), { numRuns: 200 });
    expect([...coverage.keys()].sort()).toEqual([...new Set(operations)].sort());
    console.info("S2 command coverage", { executed: Object.fromEntries(coverage), effective: Object.fromEntries(effective), filtered: 0 });
  } finally { warning.mockRestore(); }
}, 30000);
