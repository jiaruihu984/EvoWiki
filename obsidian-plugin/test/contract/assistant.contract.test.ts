// @vitest-environment jsdom
/**
 * Reading-assistant contract run against a real EvoWiki backend with the assistant wired to a scripted LLM
 * (real HTTP + SSE). Skipped unless EVOWIKI_ASSISTANT_API, EVOWIKI_ASSISTANT_USER and EVOWIKI_ASSISTANT_PAGE are set.
 * Drives the REAL AssistantClient (node transports) through the REAL AssistantController and AssistantRenderer.
 *
 * Backend script per message: search_wiki + read_page (read tools: no tool events on the wire) → propose_page_edit
 * (add_section) → final text; the observe call yields one observation. A question containing "[slow]" holds the
 * final step for a few seconds after the proposal streamed, so the session lock is held (busy checks).
 */
import { describe, expect, it } from "vitest";
import type { HttpRequest, JsonTransport, StreamTransport } from "../../src/api/client";
import { nodeStreamTransport } from "../../src/api/transport-node-sse";
import { SseDecoder } from "../../src/api/sse";
import { AssistantClient } from "../../src/assistant/client";
import { AssistantController } from "../../src/assistant/controller";
import { AssistantRenderer, type AssistantActions } from "../../src/assistant/render";
import { recordStore, type Target } from "../../src/assistant/session-store";
import { nodeJsonTransport } from "../node-transports";

const API = process.env.EVOWIKI_ASSISTANT_API;
const USER = process.env.EVOWIKI_ASSISTANT_USER ?? "";
const PAGE = process.env.EVOWIKI_ASSISTANT_PAGE ?? "";
const ANSWER = "z=-1 表示比均值**低一个标准差**。";
const QUOTE_TEXT = "z = (x - μ) / σ";

async function until(cond: () => boolean, what: string, ms = 15_000): Promise<void> {
  const end = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 20));
  }
}

describe.skipIf(!API || !USER || !PAGE)("assistant contract against a scripted-LLM backend", () => {
  const target: Target = { kind: "page", pageId: PAGE, title: "z 分数" };
  const requests: string[] = [];
  const streams: { url: string; body: string; raw: string; status: number }[] = [];
  const json: JsonTransport = {
    request: async (req: HttpRequest) => {
      requests.push(`${req.method} ${req.url.slice(API!.length)}`);
      return nodeJsonTransport.request(req);
    },
  };
  const sse: StreamTransport = {
    stream: async (req, onChunk) => {
      requests.push(`${req.method} ${req.url.slice(API!.length)}`);
      const rec = { url: req.url, body: req.body ?? "", raw: "", status: 0 };
      streams.push(rec);
      const res = await nodeStreamTransport.stream(req, (c) => { rec.raw += c; onChunk(c); });
      rec.status = res.status;
      return res;
    },
  };
  const record: Record<string, string> = {};
  const store = recordStore(() => record, () => Promise.resolve());
  let n = 0;

  function rig() {
    const api = new AssistantClient(() => ({ baseUrl: API!, userId: USER }), json, sse);
    const ctl = new AssistantController({ api, store, identity: () => ({ server: API!, user: USER }), newId: () => `t${++n}` });
    let pending: Promise<void> = Promise.resolve();
    const actions: AssistantActions = {
      send: () => { pending = ctl.send(); }, sendEdit: (t) => ctl.sendEdit(t), setDraft: (t) => ctl.setDraft(t), removeQuote: () => ctl.removeQuote(),
      retry: (id) => { pending = ctl.retry(id); }, decide: (t, p, d) => { pending = ctl.decide(t, p, d); }, undoProposal: (t, p) => { pending = ctl.undoProposal(t, p); },
      newSession: () => { pending = ctl.newSession(); }, reload: () => { pending = ctl.reload(); }, openSource: () => {},
      addFiles: (f) => { pending = ctl.addFiles(f); }, dropLibrary: () => {}, removeAttachment: (id) => ctl.removeAttachment(id),
      toggleHistory: () => ctl.toggleHistory(), searchHistory: (q) => ctl.searchHistory(q), reloadHistory: () => { pending = ctl.loadHistory(); },
      resume: (id) => { pending = ctl.resume(id).then(() => undefined); },
      confirmAction: () => {}, cancelAction: () => {}, undoAction: () => {}, moveDraft: () => {}, removeDraft: () => {}, addDraft: () => {},
      showPage: () => {}, showInParams: () => {}, answerQuiz: () => {}, nextQuiz: () => {},
      rate: (id, next) => { pending = ctl.rate(id, next); },
    };
    const r = new AssistantRenderer(actions, { renderMarkdown: (el, md) => { el.textContent = md; }, conceptName: (id) => id });
    ctl.subscribe(() => r.update(ctl.state));
    const q = (sel: string) => [...r.root.querySelectorAll(sel)] as HTMLElement[];
    // The demo's thread: per turn a .ew-umsg (quote + question), then a .ew-amsg (tools, answer, sources, cards, lines).
    const userEl = (i: number) => q(".ew-thread > .ew-umsg")[i]!;
    const turnEl = (i: number) => q(".ew-thread > .ew-amsg")[i]!;
    const lines = (i: number) => [...turnEl(i).querySelectorAll(":scope > .ew-muted")] as HTMLElement[];
    const cardStatus = (i: number) => turnEl(i).querySelector(".ew-propcard > .ew-res, .ew-propcard > .ew-muted:last-child")!.textContent;
    const buttons = (i: number) => [...turnEl(i).querySelectorAll(".ew-propcard button")] as HTMLButtonElement[];
    return { ctl, r, q, userEl, turnEl, lines, cardStatus, buttons, settle: () => pending };
  }

  it("ask, decide, ask again, restore, busy — state and DOM over real HTTP/SSE", async () => {
    // 1. Showing, attaching a quote and typing send nothing.
    const a = rig();
    a.ctl.show(target);
    a.ctl.attachQuote(target, { text: QUOTE_TEXT, heading: "z 分数" });
    a.ctl.setDraft("z=-1 是什么意思？");
    expect(requests).toEqual([]);
    await a.ctl.send();
    const c = a.ctl.state.conv!;
    const t1 = c.turns[0]!;
    expect(t1.status, JSON.stringify(t1.error)).toBe("done");
    expect(requests).toEqual(["POST /assistant/sessions", `POST /assistant/sessions/${c.sessionId}/messages`]);
    expect(Object.values(record)).toEqual([c.sessionId]);
    // The quote went out with the question (the server's session read does not return it).
    const sent = JSON.parse(streams[0]!.body) as { text: string; selection?: { text: string; heading?: string } };
    expect(sent.text).toBe("z=-1 是什么意思？");
    expect(sent.selection).toEqual({ text: QUOTE_TEXT, heading: "z 分数" });
    // Real server: read tools (search_wiki, read_page) emit no tool events, so a live turn has no tool lines.
    expect(t1.tools).toEqual([]);
    expect(a.q(".ew-tools")).toHaveLength(0);
    expect(t1.answer).toBe(ANSWER);
    expect(a.userEl(0).querySelector(".ew-uquote")!.textContent).toContain(QUOTE_TEXT);
    expect(a.turnEl(0).querySelector(".ew-answer")!.textContent).toBe(ANSWER);
    expect(t1.proposals).toHaveLength(1);
    const card1 = a.turnEl(0).querySelector(".ew-propcard")!;
    expect(card1.querySelector(".ew-muted")!.textContent).toMatch(/^加成新的一节「/);
    expect(a.buttons(0).map((b) => [b.textContent, b.disabled])).toEqual([["加入页面", false], ["不要", false]]);
    expect(t1.observed!.observations).toBeGreaterThanOrEqual(1);
    expect(a.lines(0)[0]!.textContent).toMatch(/^这次从你的提问里看出：/);

    // 2. 不要 → rejected; second question → second proposal → 加入页面 → server answers failed (edits not available).
    a.buttons(0)[1]!.click(); await a.settle();
    expect(t1.proposals[0]!.status).toBe("rejected");
    expect(a.cardStatus(0)).toBe("已忽略这条提议。");
    expect(a.buttons(0).every((b) => b.disabled)).toBe(true);
    a.ctl.setDraft("那 z=2 呢？"); await a.ctl.send();
    const t2 = a.ctl.state.conv!.turns[1]!;
    expect(t2.status, JSON.stringify(t2.error)).toBe("done");
    expect(JSON.parse(streams[1]!.body)).toEqual({ text: "那 z=2 呢？" });
    expect(t2.proposals).toHaveLength(1);
    a.buttons(1)[0]!.click(); await a.settle();
    expect(t2.proposals[0]!.status).toBe("failed");
    expect(a.cardStatus(1)).toBe("没加进去，页面没有改动。可以再点一次。");
    expect(a.turnEl(1).textContent).not.toMatch(/not available/);
    expect(a.buttons(1).map((b) => b.disabled)).toEqual([false, false]);

    // 3. A new controller over the same store: one GET rebuilds both turns.
    const before = requests.length;
    const b = rig();
    b.ctl.show(target);
    await until(() => b.ctl.state.conv?.restore === "none" && b.ctl.state.conv.turns.length > 0, "restore");
    expect(requests.slice(before)).toEqual([`GET /assistant/sessions/${c.sessionId}`]);
    const restored = b.ctl.state.conv!.turns;
    expect(restored.map((t) => [t.question, t.answer, t.restored?.proposalCount])).toEqual([
      ["z=-1 是什么意思？", ANSWER, 1], ["那 z=2 呢？", ANSWER, 1]]);
    expect(b.q(".ew-thread > .ew-umsg")).toHaveLength(2);
    expect(b.turnEl(0).querySelectorAll(".ew-propcard")).toHaveLength(1);
    expect(b.buttons(0).map((x) => x.textContent)).toEqual(["加进去", "不用"]);
    expect(b.turnEl(0).querySelectorAll(".ew-srcs > .ew-srcchip").length).toBeGreaterThan(0);

    // 4. Busy: while b's [slow] turn holds the session lock, a's decide → 409 and a's message → 409 (busy, session kept).
    b.ctl.setDraft("[slow] 再讲一遍？");
    const slow = b.ctl.send();
    await until(() => (b.ctl.state.conv!.turns[2]?.proposals.length ?? 0) > 0, "the slow turn's proposal");
    expect(b.ctl.state.conv!.turns[2]!.status).toBe("streaming");
    expect(b.buttons(2).map((x) => x.disabled)).toEqual([true, true]);   // the raising turn is still streaming
    a.buttons(1)[0]!.click(); await a.settle();
    expect(t2.proposals[0]!.status).toBe("error");
    expect(t2.proposals[0]!.error).toEqual({ kind: "http", status: 409 });
    expect(a.cardStatus(1)).toBe("这一轮还在进行，等回答结束再点。");
    const stillStreaming = b.ctl.state.conv!.turns[2]!.status === "streaming";
    a.ctl.setDraft("插一句"); await a.ctl.send();
    const t3 = a.ctl.state.conv!.turns[2]!;
    const busyChecked = stillStreaming && b.ctl.state.conv!.turns[2]!.status === "streaming";
    console.log(`[assistant-contract] busy message check ${busyChecked ? "ran" : "skipped (slow turn ended first)"}`);
    if (busyChecked) {
      expect(t3.error).toEqual({ kind: "busy" });
      expect(a.lines(2).map((l) => l.textContent).join("")).toContain("上一轮还没结束，等它结束再问。");
      expect(a.ctl.state.conv!.sessionId).toBe(c.sessionId);
      expect(Object.values(record)).toEqual([c.sessionId]);
    }
    await slow;
    expect(b.ctl.state.conv!.turns[2]!.status).toBe("done");
    expect(b.buttons(2).map((x) => x.disabled)).toEqual([false, false]);
    if (t3.error?.kind === "busy") {
      a.turnEl(2).querySelector<HTMLButtonElement>(":scope > .ew-muted > .ew-link")!.click(); await a.settle();
      expect(t3.status, JSON.stringify(t3.error)).toBe("done");
      expect(requests.filter((x) => x === "POST /assistant/sessions")).toHaveLength(1);
    }

    // Event sequences observed on the wire, for the report.
    for (const s of streams) {
      const names = new SseDecoder().push(s.raw + "\n\n").map((f) => f.event);
      console.log(`[assistant-contract] ${s.status} ${(JSON.parse(s.body) as { text: string }).text} → ${names.join(", ")}`);
    }
    console.log(`[assistant-contract] requests: ${requests.join(" | ")}`);
  }, 60_000);
});
