import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cardRig } from "../../test/card-rig";
import type { HttpRequest, JsonTransport, StreamTransport } from "../api/client";
import { ApiError, OfflineError } from "../api/errors";
import { AssistantClient } from "./client";
import { ContractError, parseSessionOut } from "./parse";
import { COPIED_MS, RatingRow } from "./rating-row";
import type { SessionOut } from "./types";

const flush = () => new Promise((r) => setTimeout(r, 0));
const TEXT: { type: "text_delta"; text: string; at: null } = { type: "text_delta", text: "方差大的方向保留的信息最多。", at: null };

let rig: ReturnType<typeof cardRig> | null = null;
afterEach(() => { rig?.dispose(); rig = null; vi.useRealTimers(); vi.unstubAllGlobals(); document.querySelectorAll(".ew-layer").forEach((x) => x.remove()); });

async function answered() {
  rig = cardRig();
  await rig.ask([TEXT]);
  return rig;
}
const btns = (g: ReturnType<typeof cardRig>) => g.all<HTMLButtonElement>(".ew-arow .ew-rbtns .ew-ib");
const thumb = (g: ReturnType<typeof cardRig>, k: "up" | "down") => btns(g)[k === "up" ? 1 : 2]!;
const pressed = (g: ReturnType<typeof cardRig>) => btns(g).slice(1).map((b) => b.getAttribute("aria-pressed"));
const lastBody = (g: ReturnType<typeof cardRig>) => g.api.rated[g.api.rated.length - 1]!.body;
const notices = () => [...document.querySelectorAll(".ew-notices > *")].map((n) => n.textContent ?? "");

describe("the rating row (app.js rateRow)", () => {
  it("a finished answer ends with .ew-arow > .ew-rbtns of 复制 / 👍 / 👎 (line icons, tooltips 复制 / 评价回复), no text line", async () => {
    const g = await answered();
    const a = g.q(".ew-thread > .ew-amsg")!;
    expect(a.lastElementChild!.className).toBe("ew-arow");
    expect(a.classList.contains("ew-last")).toBe(true);
    const row = g.q(".ew-arow")!;
    expect([...row.children].map((c) => c.className)).toEqual(["ew-rbtns"]);
    const wraps = [...row.querySelectorAll(".ew-rbtns > .ew-qtip")];
    expect(wraps).toHaveLength(3);
    for (const [i, w] of wraps.entries()) {
      const [b, t] = [...w.children] as HTMLElement[];
      expect(b!.tagName).toBe("BUTTON"); expect(b!.className).toBe("ew-ib"); expect((b as HTMLButtonElement).type).toBe("button");
      expect(b!.querySelector("svg")!.getAttribute("aria-hidden")).toBe("true");
      expect(t!.className).toBe("ew-tipbox"); expect(t!.getAttribute("role")).toBe("tooltip");
      expect(b!.getAttribute("aria-describedby")).toBe(t!.id);
      expect(b!.getAttribute("aria-label")).toBe(i === 0 ? "复制" : "评价回复");
      expect(t!.textContent).toBe(i === 0 ? "复制" : "评价回复");
    }
    expect(btns(g)[0]!.hasAttribute("aria-pressed")).toBe(false);
    expect(pressed(g)).toEqual(["false", "false"]);
    expect(row.textContent).toBe("复制评价回复评价回复");
    expect(g.q(".ew-chips")).toBeNull();
    expect(g.api.rated).toEqual([]);
  });

  it("a pointer click on a thumb lets go of the focus (the 评价回复 tooltip does not stay over the chips); Enter / Space keeps it", async () => {
    const g = await answered();
    const down = thumb(g, "down");
    down.focus();
    down.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 })); await flush();
    expect(pressed(g)).toEqual(["false", "true"]);
    expect(document.activeElement).not.toBe(down);
    down.focus();
    down.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 0 })); await flush();
    expect(pressed(g)).toEqual(["false", "false"]);
    expect(document.activeElement).toBe(down);
  });

  it("no row while the answer streams, nor under a failed turn", async () => {
    rig = cardRig();
    const g = rig;
    g.api.messageReplies.push({ throws: new OfflineError() });
    g.ctl.setDraft("问"); await g.ctl.send();
    expect(g.q(".ew-arow")).toBeNull();
  });

  it("👍 posts at once and stays highlighted; clicking it again clears it (value null)", async () => {
    const g = await answered();
    thumb(g, "up").click(); await flush();
    expect(g.api.rated.map((x) => [x.sessionId, x.body])).toEqual([["ses_fake_1", { value: "up", chips: [], note: "" }]]);
    expect(g.api.rated[0]!.messageId).toMatch(/^m\d+$/);
    expect(pressed(g)).toEqual(["true", "false"]);
    expect(g.q(".ew-arow")!.classList.contains("ew-rated")).toBe(true);
    thumb(g, "up").click(); await flush();
    expect(g.api.rated.map((x) => x.body.value)).toEqual(["up", null]);
    expect(pressed(g)).toEqual(["false", "false"]);
    expect(g.q(".ew-arow")!.classList.contains("ew-rated")).toBe(false);
  });

  it("the highlight shows before the server answers", async () => {
    const g = await answered();
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const rate = g.api.rate.bind(g.api);
    g.api.rate = async (s, m, b) => { await gate; return rate(s, m, b); };
    thumb(g, "down").click();
    expect(pressed(g)).toEqual(["false", "true"]);
    release(); await flush(); await flush();
    expect(pressed(g)).toEqual(["false", "true"]);
  });

  it("the other thumb switches; chips and the note show only while 👎 is on", async () => {
    const g = await answered();
    thumb(g, "up").click(); await flush();
    expect(g.q(".ew-chips")).toBeNull();
    thumb(g, "down").click(); await flush();
    expect(g.api.rated.map((x) => x.body.value)).toEqual(["up", "down"]);
    expect(pressed(g)).toEqual(["false", "true"]);
    const row = g.q(".ew-arow")!;
    expect([...row.children].map((c) => c.className)).toEqual(["ew-rbtns", "ew-rpanel"]);
    expect([...g.q(".ew-rpanel")!.children].map((c) => c.className)).toEqual(["ew-chips", "ew-rnote"]);
    expect(g.all(".ew-rpanel > .ew-chips > button.ew-pill").map((p) => p.textContent)).toEqual(["不对", "没帮上忙", "太啰嗦", "其他"]);
    const note = g.q<HTMLInputElement>("input.ew-rnote")!;
    expect(note.type).toBe("text"); expect(note.placeholder).toBe("补充一句"); expect(note.value).toBe("");
    thumb(g, "up").click(); await flush();
    expect(g.q(".ew-rpanel")).toBeNull(); expect(g.q(".ew-chips")).toBeNull(); expect(g.q(".ew-rnote")).toBeNull();
    expect(lastBody(g)).toEqual({ value: "up", chips: [], note: "" });
  });

  it("a chip re-posts the 👎 (single-select: another replaces it, the same one clears it)", async () => {
    const g = await answered();
    thumb(g, "down").click(); await flush();
    const pill = (k: string) => g.all<HTMLButtonElement>(".ew-pill").find((p) => p.textContent === k)!;
    pill("没帮上忙").click(); await flush();
    expect(lastBody(g)).toEqual({ value: "down", chips: ["没帮上忙"], note: "" });
    expect(g.all(".ew-pill").map((p) => p.getAttribute("aria-pressed"))).toEqual(["false", "true", "false", "false"]);
    pill("太啰嗦").click(); await flush();
    expect(lastBody(g).chips).toEqual(["太啰嗦"]);
    pill("太啰嗦").click(); await flush();
    expect(lastBody(g)).toEqual({ value: "down", chips: [], note: "" });
    expect(g.all(".ew-pill").every((p) => p.getAttribute("aria-pressed") === "false")).toBe(true);
    expect(pressed(g)).toEqual(["false", "true"]);
  });

  it("a chip keeps the card open and gives the note the focus; the focus leaving the card folds it away (only the pressed 👎 stays)", async () => {
    const g = await answered();
    thumb(g, "down").click(); await flush();
    const pill = (k: string) => g.all<HTMLButtonElement>(".ew-pill").find((p) => p.textContent === k)!;
    pill("太啰嗦").focus(); pill("太啰嗦").click(); await flush();
    const note = g.q<HTMLInputElement>(".ew-rnote")!;
    expect(document.activeElement).toBe(note);
    expect(g.q(".ew-rpanel")).not.toBeNull();
    pill("其他").focus(); await flush();              // between its own chips and the note: still open
    expect(g.q(".ew-rpanel")).not.toBeNull();
    const out = document.body.appendChild(document.createElement("button"));
    out.focus(); await flush();                       // the focus goes to a button: its click must land first
    expect(g.q(".ew-rpanel")).not.toBeNull();
    out.click(); await flush();                       // then folded
    expect(g.q(".ew-rpanel")).toBeNull();
    expect([...g.q(".ew-arow")!.children].map((c) => c.className)).toEqual(["ew-rbtns"]);
    expect(pressed(g)).toEqual(["false", "true"]);
    expect(g.q(".ew-rnoted")).toBeNull();
    expect(lastBody(g)).toEqual({ value: "down", chips: ["太啰嗦"], note: "" });
    thumb(g, "down").click(); await flush();          // 👎 again clears it
    expect(pressed(g)).toEqual(["false", "false"]); expect(lastBody(g).value).toBeNull();
    thumb(g, "down").click(); await flush();          // and afresh opens an empty card
    expect(g.q(".ew-rpanel")).not.toBeNull();
    expect(g.all(".ew-pill").every((p) => p.getAttribute("aria-pressed") === "false")).toBe(true);
    out.remove();
  });

  it("the focus leaving an empty card (no chip, no note) keeps it open and posts nothing", async () => {
    const g = await answered();
    thumb(g, "down").click(); await flush();
    const n0 = g.api.rated.length;
    const note = g.q<HTMLInputElement>(".ew-rnote")!;
    note.focus(); note.blur(); await flush();
    expect(g.api.rated).toHaveLength(n0);
    expect(g.q(".ew-rpanel")).not.toBeNull();
  });

  it("the note posts on blur or Enter (once, never per keystroke) and the card folds away", async () => {
    const g = await answered();
    thumb(g, "down").click(); await flush();
    const n0 = g.api.rated.length;
    const note = g.q<HTMLInputElement>(".ew-rnote")!;
    note.focus();
    for (const v of ["例", "例子", "例子再具体一点"]) { note.value = v; note.dispatchEvent(new Event("input")); }
    await flush();
    expect(g.api.rated).toHaveLength(n0);
    note.blur(); await flush();
    expect(lastBody(g)).toEqual({ value: "down", chips: [], note: "例子再具体一点" });
    expect(g.api.rated).toHaveLength(n0 + 1);
    expect(g.q(".ew-rpanel")).toBeNull(); expect(pressed(g)).toEqual(["false", "true"]);
    thumb(g, "down").click(); await flush();
    thumb(g, "down").click(); await flush();
    const n1 = g.api.rated.length;
    expect(note.value).toBe("");
    note.focus(); note.value = "例子再具体一点吧";
    note.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); await flush();
    expect(lastBody(g).note).toBe("例子再具体一点吧");
    expect(g.api.rated).toHaveLength(n1 + 1);
    expect(g.q(".ew-rpanel")).toBeNull(); expect(pressed(g)).toEqual(["false", "true"]);
    expect(document.activeElement).not.toBe(note);
  });

  it("a failed post puts back the last stored rating and says so in a toast (plain Chinese)", async () => {
    const g = await answered();
    thumb(g, "up").click(); await flush();
    g.api.rateReplies.push({ throws: new ApiError(500, "boom") });
    thumb(g, "down").click();
    expect(pressed(g)).toEqual(["false", "true"]);
    await flush(); await flush();
    expect(pressed(g)).toEqual(["true", "false"]);
    expect(notices()).toContain("评价没发出去。可以再点一次。");
    g.api.rateReplies.push({ throws: new OfflineError() });
    thumb(g, "up").click(); await flush(); await flush();
    expect(pressed(g)).toEqual(["true", "false"]);
    expect(notices()).toContain("未连接到服务器");
  });

  it("the latest answer is .ew-last; an older one loses it (it shows its row on hover, or always once rated)", async () => {
    const g = await answered();
    await g.ask([{ type: "text_delta", text: "第二个回答", at: null }]);
    const msgs = g.all(".ew-thread > .ew-amsg");
    expect(msgs.map((m) => m.classList.contains("ew-last"))).toEqual([false, true]);
    expect(g.all(".ew-arow")).toHaveLength(2);
    const css = readFileSync(join(process.cwd(), "styles.css"), "utf8");
    expect(css).toContain("body .ew .ew-amsg:not(.ew-last) .ew-arow:not(.ew-rated) .ew-rbtns{opacity:0}");
    expect(css).toContain("body .ew .ew-amsg:hover .ew-arow .ew-rbtns,body .ew .ew-arow.ew-rated .ew-rbtns,body .ew .ew-arow .ew-rbtns:focus-within{opacity:1}");
  });

  it("tooltips open below the icons (the shared .ew-tipbox top, not moved above)", () => {
    const css = readFileSync(join(process.cwd(), "styles.css"), "utf8");
    const rule = /body \.ew \.ew-arow \.ew-qtip \.ew-tipbox\{([^}]*)\}/.exec(css)![1]!;
    expect(rule).not.toMatch(/bottom|top:auto/);
    expect(css).toContain("body .ew .ew-qtip .ew-tipbox{position:absolute;right:-6px;top:calc(100% + 6px)");
  });

  it("复制 puts the answer on the clipboard and the tooltip says 已复制 for 1.5 s", async () => {
    const g = await answered();
    const writeText = vi.fn(() => Promise.resolve());
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    vi.useFakeTimers();
    btns(g)[0]!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(writeText).toHaveBeenCalledWith("方差大的方向保留的信息最多。");
    const tip = g.q(".ew-arow .ew-qtip .ew-tipbox")!;
    expect(tip.textContent).toBe("已复制");
    await vi.advanceTimersByTimeAsync(COPIED_MS - 10);
    expect(tip.textContent).toBe("已复制");
    await vi.advanceTimersByTimeAsync(20);
    expect(tip.textContent).toBe("复制");
    expect(g.api.rated).toEqual([]);
  });

  it("a refused copy says so in a toast and leaves the tooltip as is", async () => {
    const g = await answered();
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: () => Promise.reject(new Error("denied")) } });
    btns(g)[0]!.click(); await flush(); await flush();
    expect(g.q(".ew-arow .ew-tipbox")!.textContent).toBe("复制");
    expect(notices()).toContain("没复制上，可以再点一次。");
  });

  it("a resumed session shows each answer's stored rating (MessageOut.rating)", async () => {
    rig = cardRig();
    const g = rig;
    const out: SessionOut = { gone_sources: [], session_id: "s9", page_id: "page_pca", messages: [
      { message_id: "u1", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "一", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null }, { message_id: "a1", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "答一", rating: { message_id: "a1", value: "up", chips: [], note: "" }, sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [] },
      { message_id: "u2", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "二", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null }, { message_id: "a2", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "答二", rating: { message_id: "a2", value: "down", chips: ["太啰嗦"], note: "短一点" }, sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [] },
      { message_id: "u3", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "三", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null }, { message_id: "a3", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "答三", rating: null, sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [] },
    ], recap: null, decisions: [], proposals: [], actions: [] };
    expect(parseSessionOut(out)).toEqual(out);
    g.api.sessionOuts.set("s9", out);
    await g.ctl.resume("s9");
    const rows = g.all(".ew-arow");
    expect(rows.map((r) => r.classList.contains("ew-rated"))).toEqual([true, true, false]);
    const press = (r: HTMLElement) => [...r.querySelectorAll(".ew-rbtns .ew-ib")].slice(1).map((b) => b.getAttribute("aria-pressed"));
    expect(rows.map(press)).toEqual([["true", "false"], ["false", "true"], ["false", "false"]]);
    // a stored reason stays folded: only the pressed 👎 (the card is for giving one)
    expect([...rows[1]!.children].map((c) => c.className)).toEqual(["ew-rbtns"]);
    (rows[0]!.querySelectorAll<HTMLButtonElement>(".ew-ib")[1]!).click(); await flush();
    expect(g.api.rated).toEqual([{ sessionId: "s9", messageId: "a1", body: { value: null, chips: [], note: "" } }]);
  });
});

describe("rating contract", () => {
  const cfg = () => ({ baseUrl: "http://127.0.0.1:8000", userId: "jerry" });
  const sse: StreamTransport = { stream: () => Promise.resolve({ status: 200, text: "" }) };
  const json = (body: unknown): JsonTransport & { reqs: HttpRequest[] } => {
    const reqs: HttpRequest[] = [];
    return { reqs, request: (r) => { reqs.push(r); return Promise.resolve({ status: 200, text: JSON.stringify(body) }); } };
  };
  it("rate POSTs to the answer's rating route; chips and the note only with 👎", async () => {
    const http = json({ message_id: "m 1", value: "down", chips: ["其他"], note: "x" });
    const c = new AssistantClient(cfg, http, sse);
    await expect(c.rate("s/1", "m 1", { value: "down", chips: ["其他"], note: "x" })).resolves.toEqual({ message_id: "m 1", value: "down", chips: ["其他"], note: "x" });
    expect(http.reqs[0]).toMatchObject({ method: "POST", url: "http://127.0.0.1:8000/assistant/sessions/s%2F1/messages/m%201/rating",
      body: '{"value":"down","chips":["其他"],"note":"x"}' });
    const up = json({ message_id: "m", value: null, chips: [], note: "" });
    await new AssistantClient(cfg, up, sse).rate("s", "m", { value: null, chips: ["其他"], note: "x" });
    expect(up.reqs[0]!.body).toBe('{"value":null}');
  });
  it("a reply outside the contract is refused (fail closed)", async () => {
    for (const bad of [{ message_id: "m", value: "meh" }, { message_id: "m", value: "down", chips: ["好"] }, { message_id: "m" }, { message_id: "m", value: null, extra: 1 }]) {
      await expect(new AssistantClient(cfg, json(bad), sse).rate("s", "m", { value: null })).rejects.toBeInstanceOf(ContractError);
    }
  });
});

describe("a question's 👍 / 👎 (app.js rateRow(m, lead))", () => {
  const ASK = { type: "ask" as const, call_id: "c1", kind: "ask_user", payload: { question: "你学过线性代数吗？", options: ["学过", "没学过"] } };
  async function asked() {
    rig = cardRig();
    await rig.ask([{ type: "text_delta", text: "先问你一句。", at: null }, ASK]);
    return rig;
  }
  const qthumb = (g: ReturnType<typeof cardRig>, k: "up" | "down") => g.all<HTMLButtonElement>(".ew-askq .ew-rbtns .ew-ib")[k === "up" ? 0 : 1]!;

  it("rates the turn's message; 👎 offers a question's reasons and the note; a reason in and the card left folds it away", async () => {
    const g = await asked();
    expect(g.all(".ew-arow")).toHaveLength(1);
    expect(g.q(".ew-arow")!.classList.contains("ew-ask")).toBe(true);
    expect(g.q(".ew-amsg")!.classList.contains("ew-last")).toBe(true);
    qthumb(g, "up").click(); await flush();
    expect(g.q(".ew-chips")).toBeNull();
    qthumb(g, "down").click(); await flush();
    expect(g.api.rated.map((x) => x.body)).toEqual([{ value: "up", chips: [], note: "" }, { value: "down", chips: [], note: "" }]);
    expect(g.api.rated[1]!.messageId).toMatch(/^m\d+$/);
    const row = g.q(".ew-arow.ew-ask")!;
    expect([...row.children].map((c) => c.className)).toEqual(["ew-askq", "ew-rpanel"]);
    expect(g.all(".ew-rpanel > .ew-chips > button.ew-pill").map((p) => p.textContent)).toEqual(["问过了", "我说过不会", "问得看不懂", "没必要问"]);
    g.all<HTMLButtonElement>(".ew-pill")[0]!.click(); await flush();
    expect(lastBody(g)).toEqual({ value: "down", chips: ["问过了"], note: "" });
    expect(g.q(".ew-rpanel")).not.toBeNull();
    const note = g.q<HTMLInputElement>(".ew-rnote")!;
    expect(document.activeElement).toBe(note);
    note.value = "上一轮刚问过"; note.blur(); await flush();
    expect(lastBody(g)).toEqual({ value: "down", chips: ["问过了"], note: "上一轮刚问过" });
    expect([...row.children].map((c) => c.className)).toEqual(["ew-askq"]);
    expect(qthumb(g, "down").getAttribute("aria-pressed")).toBe("true");
    qthumb(g, "up").click(); await flush();
    expect(g.q(".ew-rpanel")).toBeNull(); expect(g.q(".ew-chips")).toBeNull();
  });

  it("once he has answered the question its 👎 reasons fold away; the pressed 👎 stays", () => {
    const row = new RatingRow(() => "", () => {}, document.createElement("p"));
    row.paint({ value: "down", chips: [], note: "" });
    expect(row.root.querySelector(".ew-rpanel")).not.toBeNull();
    const down = { value: "down" as const, chips: ["问过了" as const], note: "上一轮刚问过" };
    row.paint(down, false);
    expect([...row.root.children].map((c) => c.className)).toEqual(["ew-askq"]);
    expect(row.root.querySelectorAll(".ew-ib")[1]!.getAttribute("aria-pressed")).toBe("true");
  });
});
