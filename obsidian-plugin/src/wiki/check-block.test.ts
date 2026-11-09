import { describe, expect, it } from "vitest";
import { CheckBlockHub, mountCheckBlock, parseCheckBlock, renderCheckBlock } from "./check-block";
import { firstPage, setup, until, view } from "./controller-test-kit";
import { WIKI_COPY } from "./copy";
import { btns } from "./render-test-kit";
import { type CheckOut, IDK } from "./schema";
import type { WikiState } from "./state";

const SRC = "page: p1\nsection: s5\n";

async function opened() {
  const s = setup();
  await firstPage(s.c);
  const hub = new CheckBlockHub();
  hub.set(s.c);
  const el = document.createElement("div");
  const stop = mountCheckBlock(el, SRC, hub);
  return { ...s, hub, el, stop };
}
const options = (el: HTMLElement) => btns(el, ".ew-check > .ew-opts > button.ew-o:not(.ew-idk)");
const idk = (el: HTMLElement) => btns(el, ".ew-check > .ew-opts > button.ew-o.ew-idk")[0]!;
/** User review: no result sentence after answering — the options' colours say it. */
const verdict = (el: HTMLElement) => el.querySelector(".ew-res")?.textContent ?? null;

describe("parseCheckBlock", () => {
  it("reads page and section lines (CRLF, spaces, other lines ignored); null when one is missing", () => {
    expect(parseCheckBlock(SRC)).toEqual({ pageId: "p1", sectionId: "s5" });
    expect(parseCheckBlock("\r\n  page :  p_x \r\nnote: hi\r\nsection: s-1\r\n")).toEqual({ pageId: "p_x", sectionId: "s-1" });
    expect(parseCheckBlock("page: p1\n")).toBeNull();
    expect(parseCheckBlock("section: s5")).toBeNull();
    expect(parseCheckBlock("page: a b\nsection: s5")).toBeNull();
    expect(parseCheckBlock("")).toBeNull();
  });
});

describe("renderCheckBlock without the question at hand", () => {
  it("unreadable block / section gone or not a check: one plain line; no wiki yet / another page open: one button that opens it", async () => {
    const one = (el: HTMLElement) => { expect(el.querySelectorAll("button")).toHaveLength(0); expect(el.className).toBe("ew-muted"); return el.textContent; };
    expect(one(renderCheckBlock("hello", null))).toBe(WIKI_COPY.checkUnreadable);
    expect(renderCheckBlock(SRC, null).textContent).toBe(WIKI_COPY.checkOpen);
    const { c } = setup();
    expect(renderCheckBlock(SRC, c).textContent).toBe(WIKI_COPY.checkOpen); // nothing open
    await firstPage(c);
    const opened: string[] = [];
    const go = renderCheckBlock("page: p9\nsection: s5", c, (id) => opened.push(id)) as HTMLButtonElement;
    expect(go.textContent).toBe(WIKI_COPY.checkOpen);
    go.click();
    expect(opened).toEqual(["p9"]);                 // one click, no trip to the sidebar
    expect(one(renderCheckBlock("page: p1\nsection: s99", c))).toBe(WIKI_COPY.checkGone);
    expect(one(renderCheckBlock("page: p1\nsection: s4", c))).toBe(WIKI_COPY.checkGone); // s4 is not a check
  });
});

describe("the check card (app.js checkCard)", () => {
  it("the demo's DOM: .ew wrapper > .ew-check > .ew-h (icon + title), .ew-stem, .ew-opts with 「A. …」 and the dashed 不知道", async () => {
    const { c, el } = await opened();
    const check = view(c).page.sections.find((x) => x.id === "s5")!.check!;
    const card = el.querySelector(":scope > .ew.ew-inline > .ew-check")!;
    expect([...card.children].map((x) => x.className)).toEqual(["ew-h", "ew-stem", "ew-opts"]);
    const head = card.querySelector(".ew-h")!;
    expect(head.firstElementChild?.tagName.toLowerCase()).toBe("svg");
    expect(head.firstElementChild?.getAttribute("viewBox")).toBe("0 0 24 24");
    expect(head.textContent).toBe("小测");
    expect(card.querySelector(".ew-stem")?.textContent).toBe(check.stem);
    expect(options(el).map((b) => b.textContent)).toEqual(check.options.map((o, i) => `${String.fromCharCode(65 + i)}. ${o}`));
    expect(idk(el).textContent).toBe(WIKI_COPY.idk);
    expect(btns(el, "button").every((b) => !b.disabled && b.type === "button")).toBe(true);
    expect(verdict(el)).toBeNull();
  });

  it("a right answer goes through the controller (POST checks/s5): .ew-right, no sentence, all disabled, no follow-ups", async () => {
    const { api, c, el } = await opened();
    const n = api.calls.length;
    options(el)[1]!.click();
    await until(() => view(c).page.sections.find((x) => x.id === "s5")!.check!.answered !== null && c.state.busy === null);
    expect(api.calls.slice(n).map((x) => x.method)[0]).toBe("answerCheck");
    expect(api.calls[n]?.args).toEqual(["p1", "s5", 1, expect.stringMatching(/^art_[0-9a-f]{24}$/) as string]);
    expect(options(el)[1]!.className).toBe("ew-o ew-right");
    expect(el.querySelectorAll(".ew-wrong")).toHaveLength(0);
    expect(verdict(el)).toBeNull();
    expect(view(c).sectionNotes.s5).toBeUndefined();
    expect(btns(el, "button").every((b) => b.disabled)).toBe(true);
    expect(el.querySelector(".ew-follow")).toBeNull();
  });

  it("a wrong answer: the picked one .ew-wrong, the right one .ew-right, no sentence, then .ew-follow with the three buttons only", async () => {
    const { c, el } = await opened();
    options(el)[0]!.click();
    await until(() => view(c).page.sections.find((x) => x.id === "s5")!.check!.answered !== null && c.state.busy === null);
    expect(options(el)[0]!.className).toBe("ew-o ew-wrong");
    expect(options(el)[1]!.className).toBe("ew-o ew-right");
    expect(verdict(el)).toBeNull();
    const f = el.querySelector(".ew-check > .ew-follow")!;
    expect([...f.children].map((x) => x.className)).toEqual(["ew-row"]);
    expect(btns(f as HTMLElement, ".ew-row > button.ew-btn").map((b) => b.textContent)).toEqual(["粗心答错", "现在会了", "还没掌握"]);
  });

  it("不知道: the dashed button .ew-wrong, the right option green, no sentence; no 粗心答错", async () => {
    const { api, c, el } = await opened();
    const n = api.calls.length;
    idk(el).click();
    await until(() => view(c).page.sections.find((x) => x.id === "s5")!.check!.answered !== null && c.state.busy === null);
    expect(api.calls[n]?.args).toEqual(["p1", "s5", IDK, expect.stringMatching(/^art_[0-9a-f]{24}$/) as string]);
    expect(idk(el).className).toBe("ew-o ew-idk ew-wrong");
    expect(options(el)[1]!.className).toBe("ew-o ew-right");
    expect(verdict(el)).toBeNull();
    expect(btns(el, ".ew-follow .ew-btn").map((b) => b.textContent)).toEqual(["现在会了", "还没掌握"]);
  });

  it("offline: nothing is sent; the controller's offline line shows under the options (.ew-muted), still answerable", async () => {
    const { api, conn, el } = await opened();
    conn.emit("offline");
    const n = api.calls.length;
    options(el)[1]!.click();
    expect(api.calls.length).toBe(n);
    expect(el.querySelector(".ew-check > .ew-muted")?.textContent).toBe(WIKI_COPY.offline);
    expect(options(el).every((b) => !b.disabled)).toBe(true);
  });

  it("while another write is in flight the options are disabled", async () => {
    const { c, el } = await opened();
    const p = c.togglePin("s3");
    expect(c.state.busy).not.toBeNull();
    expect(btns(el, "button").every((b) => b.disabled)).toBe(true);
    await p;
    expect(btns(el, "button").every((b) => !b.disabled)).toBe(true);
  });
});

describe("marks edge cases (a fixed check on the open page)", () => {
  async function withCheck(check: CheckOut) {
    const { c } = setup();
    await firstPage(c);
    const v = view(c);
    const state: WikiState = { ...c.state, page: { kind: "open", ...v, page: { ...v.page,
      sections: v.page.sections.map((x) => (x.id === "s5" ? { ...x, check } : x)) } } };
    return renderCheckBlock(SRC, { state, subscribe: () => () => undefined, answerCheck: () => undefined });
  }
  const base = { stem: "Q", options: ["x", "y", "z"], artifact_id: null };
  const v = (el: HTMLElement) => el.querySelector(":scope > .ew-res")?.textContent ?? null;

  it("wrong without the key's index: only the pick marked, no sentence; 不知道 without it says nothing", async () => {
    const wrong = await withCheck({ ...base, answered: 0, correct_index: null, correct: false });
    expect(v(wrong)).toBeNull();
    expect(wrong.querySelectorAll(".ew-o")[0]!.classList.contains("ew-wrong")).toBe(true);
    expect(wrong.querySelectorAll(".ew-right")).toHaveLength(0);
    const idkEl = await withCheck({ ...base, answered: IDK, correct_index: null, correct: null });
    expect(v(idkEl)).toBeNull();
  });

  it("不知道 as the server answers it (correct null, the key's index): the key green, 不知道 marked red, no sentence", async () => {
    const el = await withCheck({ ...base, answered: IDK, correct_index: 2, correct: null });
    expect(el.querySelectorAll(".ew-o")[2]!.classList.contains("ew-right")).toBe(true);
    expect(el.querySelector(".ew-idk")!.classList.contains("ew-wrong")).toBe(true);
    expect(v(el)).toBeNull();
  });
});

describe("CheckBlockHub / mountCheckBlock", () => {
  it("redraws when the controller arrives, changes or goes away; the cleanup stops redraws", async () => {
    const hub = new CheckBlockHub();
    const el = document.createElement("div");
    el.textContent = "page: p1"; // Obsidian's raw text is replaced
    const stop = mountCheckBlock(el, SRC, hub);
    expect(el.textContent).toBe(WIKI_COPY.checkOpen);
    const { c } = setup();
    hub.set(c);
    expect(el.textContent).toBe(WIKI_COPY.checkOpen);
    await firstPage(c); // the controller notifies → the card appears
    expect(el.querySelector(".ew-check")).not.toBeNull();
    hub.set(null);
    expect(el.textContent).toBe(WIKI_COPY.checkOpen);
    hub.set(c);
    stop();
    hub.set(null);
    expect(el.querySelector(".ew-check")).not.toBeNull(); // no longer redrawn
  });

  it("set(null) unsubscribes from the old controller", async () => {
    const hub = new CheckBlockHub();
    const { c } = setup();
    hub.set(c);
    let n = 0;
    hub.subscribe(() => n++);
    hub.set(null);
    n = 0;
    await firstPage(c);
    expect(n).toBe(0);
  });
});
