import { describe, expect, it, vi } from "vitest";
import type { HttpRequest, StreamTransport } from "../api/client";
import { ApiError, OfflineError } from "../api/errors";
import { HttpWikiClient, type WikiApi, type WikiHttp, type WikiHttpRequest } from "./api";
import { DEFAULT_BOT_SETTINGS } from "./fixtures";
import type { ExemplarIn, OrganizeEvent, SettingsPut } from "./schema";

const cfg = () => ({ baseUrl: "http://h:1", userId: "jerry" });

function http(status = 200, text = "{}"): WikiHttp & { calls: WikiHttpRequest[] } {
  const calls: WikiHttpRequest[] = [];
  return { calls, request: vi.fn((r: WikiHttpRequest) => { calls.push(r); return Promise.resolve({ status, text }); }) };
}
const noSse: StreamTransport = { stream: vi.fn() };

/** Feeds chunks; anything thrown out of onChunk is recorded (production runs onChunk inside res.on("data")). */
function sse(chunks: string[], status = 200): StreamTransport & { reqs: HttpRequest[]; escaped: unknown[]; fed: number } {
  const out = {
    reqs: [] as HttpRequest[], escaped: [] as unknown[], fed: 0,
    stream: vi.fn<StreamTransport["stream"]>((r, onChunk) => {
      out.reqs.push(r);
      if (status >= 200 && status < 300) {
        for (const c of chunks) { out.fed++; try { onChunk(c); } catch (e) { out.escaped.push(e); } }
      }
      return Promise.resolve({ status, text: status >= 200 && status < 300 ? "" : '{"detail":"boom"}' });
    }),
  };
  return out;
}

const EV = { type: "item" as const, block: "a/1", session: "0123456789abcdef", node: "quiz#0", kind: "quiz", item: 0, attempt: 1,
  revision: 3, response: [1, 2], correct: true, hintUsed: false, latencyMs: 900 };
type Case = [string, (c: WikiApi) => Promise<unknown>, WikiHttpRequest["method"], string, unknown];
const cases: Case[] = [
  ["answerDraft answers", (c) => c.answerDraft("d1", { answers: [0, -1, 2], skip: false }), "POST", "/wiki/goals/d1/answers",
    { answers: [0, -1, 2] }],
  ["answerDraft skip", (c) => c.answerDraft("d1", { answers: null, skip: true }), "POST", "/wiki/goals/d1/answers", { skip: true }],
  ["openPage", (c) => c.openPage("p1"), "POST", "/wiki/pages/p1/open", undefined],
  ["openPage encodes /", (c) => c.openPage("a/b"), "POST", "/wiki/pages/a%2Fb/open", undefined],
  ["openPage from the page view", (c) => c.openPage("a/b", { rewrite: true }), "POST", "/wiki/pages/a%2Fb/open?rewrite=1", undefined],
  ["openPage re-open (rewrite false)", (c) => c.openPage("p1", { rewrite: false }), "POST", "/wiki/pages/p1/open", undefined],
  ["getPage: read only", (c) => c.getPage("a/b"), "GET", "/wiki/pages/a%2Fb", undefined],
  ["fold", (c) => c.fold("p1", "s1", true), "POST", "/wiki/pages/p1/sections/s1/fold", { folded: true }],
  ["pin", (c) => c.pin("p1", "s/2", false), "POST", "/wiki/pages/p1/sections/s%2F2/pin", { pinned: false }],
  ["feedbackKnow", (c) => c.feedbackKnow("p1", "s1"), "POST", "/wiki/pages/p1/sections/s1/feedback", { kind: "know" }],
  ["answerCheck", (c) => c.answerCheck("p1", "s1", 2), "POST", "/wiki/pages/p1/checks/s1", { choice: 2 }],
  ["feedbackKnow with the shown artifact_id", (c) => c.feedbackKnow("p1", "s1", "art_0123456789abcdef01234567"), "POST", "/wiki/pages/p1/sections/s1/feedback",
    { kind: "know", artifact_id: "art_0123456789abcdef01234567" }],
  ["feedbackKnow null artifact_id sends nothing", (c) => c.feedbackKnow("p1", "s1", null), "POST", "/wiki/pages/p1/sections/s1/feedback", { kind: "know" }],
  ["answerCheck with artifact_id", (c) => c.answerCheck("p1", "s1", 2, "art_0123456789abcdef01234567"), "POST", "/wiki/pages/p1/checks/s1", { choice: 2, artifact_id: "art_0123456789abcdef01234567" }],
  ["pageFeedback with artifact_id", (c) => c.pageFeedback("p1", { chips: ["too_hard"], text: "", section_id: "s2", apply: false, artifact_id: "art_0123456789abcdef01234567" }),
    "POST", "/wiki/pages/p1/feedback", { chips: ["too_hard"], text: "", section_id: "s2", apply: false, artifact_id: "art_0123456789abcdef01234567" }],
  ["gradeReview with artifact_id", (c) => c.gradeReview({ cell: "c1.term", choice: 1, page_id: "p1", section_id: "s5", artifact_id: "art_0123456789abcdef01234567" }),
    "POST", "/wiki/review/grade", { cell: "c1.term", choice: 1, page_id: "p1", section_id: "s5", artifact_id: "art_0123456789abcdef01234567" }],
  ["followup with artifact_id", (c) => c.followup("p1", "s1", { choice: "slip", artifact_id: "art_0123456789abcdef01234567" }),
    "POST", "/wiki/pages/p1/checks/s1/followup", { choice: "slip", artifact_id: "art_0123456789abcdef01234567" }],
  ["confirmAnswer with artifact_id", (c) => c.confirmAnswer("f1", { choice: 0, artifact_id: "art_0123456789abcdef01234567" }),
    "POST", "/wiki/review/confirms/f1", { choice: 0, artifact_id: "art_0123456789abcdef01234567" }],
  ["done", (c) => c.done("p1"), "POST", "/wiki/pages/p1/done", undefined],
  ["expand", (c) => c.expand("p1"), "POST", "/wiki/pages/p1/expand", undefined],
  ["collapse", (c) => c.collapse("p/1"), "POST", "/wiki/pages/p%2F1/collapse", undefined],
  ["catalog", (c) => c.catalog(), "GET", "/wiki", undefined],
  ["kGrid", (c) => c.kGrid(), "GET", "/wiki/k", undefined],
  ["declare null", (c) => c.declare("c1.term", null), "POST", "/wiki/k/cells/c1.term/declare", { value: null }],
  ["declare know", (c) => c.declare("c1.term", "know"), "POST", "/wiki/k/cells/c1.term/declare", { value: "know" }],
  ["cancelRun", (c) => c.cancelRun("p/1"), "POST", "/wiki/pages/p%2F1/run/cancel", undefined],
  ["getSettings", (c) => c.getSettings(), "GET", "/wiki/settings", undefined],
  ["getPath", (c) => c.getPath("path/1"), "GET", "/wiki/paths/path%2F1", undefined],
  ["pathNext", (c) => c.pathNext("path1"), "POST", "/wiki/paths/path1/next", undefined],
  ["pathPageDone", (c) => c.pathPageDone("path1", 2), "POST", "/wiki/paths/path1/pages/2/done", undefined],
  ["reviewToday", (c) => c.reviewToday(), "GET", "/wiki/review/today", undefined],
  ["reviewToday more", (c) => c.reviewToday(true), "GET", "/wiki/review/today?more=true", undefined],
  ["gradeReview grade", (c) => c.gradeReview({ cell: "c1.term", grade: "fuzzy", page_id: "p1", section_id: "s2" }),
    "POST", "/wiki/review/grade", { cell: "c1.term", grade: "fuzzy", page_id: "p1", section_id: "s2" }],
  ["gradeReview grade of a card on two cells", (c) => c.gradeReview({ cell: "c1.term", cells: ["c1.term", "c1.notation"], grade: "recalled", page_id: "p1", section_id: "s2" }),
    "POST", "/wiki/review/grade", { cell: "c1.term", cells: ["c1.term", "c1.notation"], grade: "recalled", page_id: "p1", section_id: "s2" }],
  ["gradeReview in a page's round", (c) => c.gradeReview({ cell: "c1.term", grade: "fuzzy", page_id: "p1", section_id: "s2", page_round: true }),
    "POST", "/wiki/review/grade", { cell: "c1.term", grade: "fuzzy", page_id: "p1", section_id: "s2", page_round: true }],
  ["gradeReview grade, no section", (c) => c.gradeReview({ cell: "c1.term", grade: "recalled", page_id: null, section_id: null }),
    "POST", "/wiki/review/grade", { cell: "c1.term", grade: "recalled" }],
  ["gradeReview check", (c) => c.gradeReview({ cell: "c1.term", choice: -1, page_id: "p1", section_id: "s5" }),
    "POST", "/wiki/review/grade", { cell: "c1.term", choice: -1, page_id: "p1", section_id: "s5" }],
  ["putSettings", (c) => c.putSettings({ review_per_day: 5 }), "PUT", "/wiki/settings", { review_per_day: 5 }],
  ["putSettings: no keys", (c) => c.putSettings({}), "PUT", "/wiki/settings", {}],
  ["putSettings: language only", (c) => c.putSettings({ language: "en" }), "PUT", "/wiki/settings", { language: "en" }],
  ["putSettings: page_limit_usd", (c) => c.putSettings({ page_limit_usd: 0.5 }), "PUT", "/wiki/settings", { page_limit_usd: 0.5 }],
  ["putSettings: page_limit_usd null (no cap) is sent", (c) => c.putSettings({ page_limit_usd: null }), "PUT",
    "/wiki/settings", { page_limit_usd: null }],
  ["putSettings: curate / days (gone with 整理), refine and other extra keys are not sent",
    (c) => c.putSettings({ curate: "goal", days: 7, review_per_day: 10, language: "zh",
      refine: { auto: "all", per_page_limit_usd: 1 } } as SettingsPut),
    "PUT", "/wiki/settings", { review_per_day: 10, language: "zh" }],
  // feedback signals
  ["pageFeedback", (c) => c.pageFeedback("p/1", { chips: ["too_shallow", "too_hard"], text: "再多讲点", section_id: null }),
    "POST", "/wiki/pages/p%2F1/feedback", { chips: ["too_shallow", "too_hard"], text: "再多讲点", section_id: null }],
  ["pageFeedback section", (c) => c.pageFeedback("p1", { chips: [], text: "看不懂", section_id: "s2" }),
    "POST", "/wiki/pages/p1/feedback", { chips: [], text: "看不懂", section_id: "s2" }],
  ["handEdit", (c) => c.handEdit("p1", { sections: [{ section_id: "s1", body: "b", note_body: "n" }], removed: [{ section_id: "s2", fingerprint: "fp2" }], summary: "改了一句" }),
    "POST", "/wiki/pages/p1/hand-edit", { sections: [{ section_id: "s1", body: "b", note_body: "n" }], removed: [{ section_id: "s2", fingerprint: "fp2" }], summary: "改了一句" }],
  ["undoHandEdit", (c) => c.undoHandEdit("p1", "he1"), "POST", "/wiki/pages/p1/hand-edit/he1/undo", undefined],
  // RSI changes
  ["rsiChanges", (c) => c.rsiChanges(), "GET", "/wiki/rsi/changes", undefined],
  ["engine", (c) => c.engine(), "GET", "/wiki/engine", undefined],
  ["undoPatch", (c) => c.undoPatch("pt/1"), "POST", "/wiki/rsi/patches/pt%2F1/undo", undefined],
  ["approvePatch", (c) => c.approvePatch("forgep:c1"), "POST", "/wiki/rsi/patches/forgep%3Ac1/approve", undefined],
  ["discardPatch", (c) => c.discardPatch("forgep:c1"), "POST", "/wiki/rsi/patches/forgep%3Ac1/discard", undefined],
  // Bot
  ["botSuggestions today", (c) => c.botSuggestions("today"), "GET", "/wiki/bot/suggestions?placement=today", undefined],
  ["botSuggestions page", (c) => c.botSuggestions("page", "p/1"), "GET", "/wiki/bot/suggestions?placement=page&page_id=p%2F1", undefined],
  ["botReact", (c) => c.botReact("sg/1", "not_interested"), "POST", "/wiki/bot/suggestions/sg%2F1/react", { reaction: "not_interested" }],
  ["botReviewReminder", (c) => c.botReviewReminder(), "GET", "/wiki/bot/review-reminder", undefined],
  ["botReviewReact", (c) => c.botReviewReact("not_interested"), "POST", "/wiki/bot/review-reminder", { reaction: "not_interested" }],
  ["botActions", (c) => c.botActions(), "GET", "/wiki/bot/actions", undefined],
  ["botActions status", (c) => c.botActions("proposed"), "GET", "/wiki/bot/actions?status=proposed", undefined],
  ["botDecide approve", (c) => c.botDecide("act/1", "approve"), "POST", "/wiki/bot/actions/act%2F1/approve", undefined],
  ["botDecide reject", (c) => c.botDecide("a1", "reject"), "POST", "/wiki/bot/actions/a1/reject", undefined],
  ["botDecide undo", (c) => c.botDecide("a1", "undo"), "POST", "/wiki/bot/actions/a1/undo", undefined],
  ["botWhy trigger", (c) => c.botWhy({ triggerId: "tr/1" }), "GET", "/wiki/bot/why?trigger_id=tr%2F1", undefined],
  ["botWhy suggestion", (c) => c.botWhy({ suggestionId: "sg1" }), "GET", "/wiki/bot/why?suggestion_id=sg1", undefined],
  ["botRecent", (c) => c.botRecent(false), "GET", "/wiki/bot/recent?days=14&skipped=false", undefined],
  ["botRecent skipped", (c) => c.botRecent(true), "GET", "/wiki/bot/recent?days=14&skipped=true", undefined],
  ["botBadge", (c) => c.botBadge(), "GET", "/wiki/bot/badge", undefined],
  ["botSettings", (c) => c.botSettings(), "GET", "/wiki/bot/settings", undefined],
  ["putBotSettings", (c) => c.putBotSettings(DEFAULT_BOT_SETTINGS), "PUT", "/wiki/bot/settings", DEFAULT_BOT_SETTINGS],
  ["botPause", (c) => c.botPause("tomorrow"), "POST", "/wiki/bot/pause", { until: "tomorrow" }],
  // feedback loop (B1-B5)
  ["pageFeedback apply false", (c) => c.pageFeedback("p/1", { chips: ["too_hard"], text: "太难", section_id: null, apply: false }),
    "POST", "/wiki/pages/p%2F1/feedback", { chips: ["too_hard"], text: "太难", section_id: null, apply: false }],
  ["undoFeedback", (c) => c.undoFeedback("p/1", "fb/1"), "POST", "/wiki/pages/p%2F1/feedback/fb%2F1/undo", undefined],
  ["components", (c) => c.components(), "GET", "/wiki/components", undefined],
  ["graph", (c) => c.graph(), "GET", "/wiki/graph", undefined],
  ["splitNode", (c) => c.splitNode("a/b"), "POST", "/wiki/nodes/a%2Fb/split", undefined],
  ["wantNode", (c) => c.wantNode("n1", "m1"), "POST", "/wiki/nodes/n1/want", { map_id: "m1" }],
  ["editPath move", (c) => c.editPath("p1", { op: "move", from: 2, to: 0 }), "POST", "/wiki/paths/p1/edits", { op: "move", from: 2, to: 0 }],
  ["editPath remove", (c) => c.editPath("p1", { op: "remove", page_key: "key1" }), "POST", "/wiki/paths/p1/edits", { op: "remove", page_key: "key1" }],
  ["editPath add", (c) => c.editPath("p1", { op: "add", request_id: "test-intent", title: "交叉验证" }), "POST", "/wiki/paths/p1/edits", { op: "add", request_id: "test-intent", title: "交叉验证" }],
  ["editPath add at", (c) => c.editPath("p1", { op: "add", request_id: "test-intent", title: "SVM", node_id: "svm", to_index: 2 }), "POST", "/wiki/paths/p1/edits",
    { op: "add", request_id: "test-intent", title: "SVM", node_id: "svm", to_index: 2 }],
  ["editPath unremove", (c) => c.editPath("p1", { op: "unremove", page_key: "k1" }), "POST", "/wiki/paths/p1/edits", { op: "unremove", page_key: "k1" }],
  ["splitRow", (c) => c.splitRow("p/1", "k 1"), "POST", "/wiki/paths/p%2F1/rows/k%201/split", undefined],
  ["undoSplitRow", (c) => c.undoSplitRow("p1", "k1", "chg"), "POST", "/wiki/paths/p1/rows/k1/split/undo", { change_id: "chg" }],
  ["editPath add node", (c) => c.editPath("p1", { op: "add", request_id: "test-intent", title: "SVM", node_id: "svm" }), "POST", "/wiki/paths/p1/edits",
    { op: "add", request_id: "test-intent", title: "SVM", node_id: "svm" }],
  // teaching spec
  ["teachSpec", (c) => c.teachSpec(), "GET", "/wiki/teach/spec", undefined],
  ["addSpecItem", (c) => c.addSpecItem({ text: "先举例", scope: "any", domain: null }), "POST", "/wiki/teach/items",
    { text: "先举例", scope: "any", domain: null }],
  ["addSpecItem base", (c) => c.addSpecItem({ text: "先举例", scope: "concept", domain: "统计" }, 3), "POST",
    "/wiki/teach/items?base_version=3", { text: "先举例", scope: "concept", domain: "统计" }],
  ["editSpecItem text only", (c) => c.editSpecItem("i/1", { text: "新的" }), "PATCH", "/wiki/teach/items/i%2F1", { text: "新的" }],
  ["editSpecItem clear domain, base", (c) => c.editSpecItem("i1", { domain: null }, 2), "PATCH",
    "/wiki/teach/items/i1?base_version=2", { domain: null }],
  ["editSpecItem all keys", (c) => c.editSpecItem("i1", { text: "t", scope: "quick", domain: "d" }), "PATCH",
    "/wiki/teach/items/i1", { text: "t", scope: "quick", domain: "d" }],
  ["deleteSpecItem", (c) => c.deleteSpecItem("i1"), "DELETE", "/wiki/teach/items/i1", undefined],
  ["deleteSpecItem base", (c) => c.deleteSpecItem("i1", 4), "DELETE", "/wiki/teach/items/i1?base_version=4", undefined],
  ["takeOverSpecItem", (c) => c.takeOverSpecItem("i1"), "POST", "/wiki/teach/items/i1/take_over", undefined],
  ["addExemplar wiki_page", (c) => c.addExemplar({ kind: "wiki_page", ref: "p1" }), "POST", "/wiki/teach/exemplars",
    { kind: "wiki_page", ref: "p1" }],
  ["addExemplar vault_note, base", (c) => c.addExemplar({ kind: "vault_note", ref: "a.md", title: "A", text: "正文" }, 5),
    "POST", "/wiki/teach/exemplars?base_version=5", { kind: "vault_note", ref: "a.md", title: "A", text: "正文" }],
  ["addExemplar wiki_page drops stray keys",
    (c) => c.addExemplar({ kind: "wiki_page", ref: "p1", title: "x", text: "y" } as ExemplarIn), "POST", "/wiki/teach/exemplars",
    { kind: "wiki_page", ref: "p1" }],
  ["deleteExemplar", (c) => c.deleteExemplar("e/1"), "DELETE", "/wiki/teach/exemplars/e%2F1", undefined],
  ["deleteExemplar base", (c) => c.deleteExemplar("e1", 1), "DELETE", "/wiki/teach/exemplars/e1?base_version=1", undefined],
  // interactives
  ["editPath retitle", (c) => c.editPath("p1", { op: "retitle", page_key: "k", title: "新" }), "POST", "/wiki/paths/p1/edits", { op: "retitle", page_key: "k", title: "新" }],
  ["assetMeta", (c) => c.assetMeta("a/1"), "GET", "/wiki/assets/a%2F1/meta", undefined],
  ["guidelines", (c) => c.guidelines(), "GET", "/wiki/guidelines", undefined],
  ["addGuideline text", (c) => c.addGuideline({ text: "短一点" }), "POST", "/wiki/guidelines", { text: "短一点" }],
  ["deleteGuideline", (c) => c.deleteGuideline("g1"), "DELETE", "/wiki/guidelines/g1", undefined],
];

describe("blockEvents", () => {
  it("posts the events and says whether any went into K", async () => {
    const out = (status: string) => JSON.stringify({ events: [{ index: 0, status: "score" }, { index: 1, status }] });
    const t = http(200, out("k"));
    expect(await new HttpWikiClient(cfg, t, noSse).blockEvents("a/1", [EV])).toEqual({ inK: true });
    expect(t.calls[0]!.url).toBe("http://h:1/wiki/assets/a%2F1/results");
    expect(JSON.parse(t.calls[0]!.body as string)).toEqual({ events: [EV] });
    expect(await new HttpWikiClient(cfg, http(200, out("signal")), noSse).blockEvents("a/1", [EV])).toEqual({ inK: false });
  });
});

describe("JSON calls", () => {
  it.each(cases)("%s", async (_name, run, method, path, body) => {
    const t = http();
    await run(new HttpWikiClient(cfg, t, noSse));
    expect(t.calls).toHaveLength(1);
    const req = t.calls[0]!;
    expect(req.method).toBe(method);
    expect(req.url).toBe("http://h:1" + path);
    expect(req.headers["X-EvoWiki-User"]).toBe("jerry");
    if (body === undefined) {
      expect(req.body).toBeUndefined();
      expect(req.headers["Content-Type"]).toBeUndefined();
    } else {
      expect(JSON.parse(req.body as string)).toEqual(body);
      expect(req.headers["Content-Type"]).toBe("application/json");
    }
  });

  it("sends only the populated GuidelineIn key even when extra keys are present", async () => {
    const t = http();
    const sloppy = { text: "x", proposal_id: undefined };
    await new HttpWikiClient(cfg, t, noSse).addGuideline(sloppy);
    expect(JSON.parse(t.calls[0]!.body as string)).toEqual({ text: "x" });
  });

  it("maps 409 to ApiError with status and detail", async () => {
    const p = new HttpWikiClient(cfg, http(409, '{"detail":"page changed"}'), noSse).fold("p1", "s1", true);
    await expect(p).rejects.toBeInstanceOf(ApiError);
    await expect(p).rejects.toMatchObject({ status: 409, detail: "page changed" });
  });

  it("resolves undefined on 204 without parsing", async () => {
    await expect(new HttpWikiClient(cfg, http(204, ""), noSse).deleteGuideline("g1")).resolves.toBeUndefined();
  });

  it("maps a non-JSON 2xx to ApiError", async () => {
    const p = new HttpWikiClient(cfg, http(200, "<html>"), noSse).catalog();
    await expect(p).rejects.toBeInstanceOf(ApiError);
    await expect(p).rejects.toThrow("invalid JSON response: <html>");
  });

  it("deleteSpecItem / deleteExemplar parse the 200 body (TeachSpecOut, not 204)", async () => {
    const spec = { version: 3, stored: true, items: [], exemplars: [], params: {}, evidence: {}, max_items: 30, max_exemplars: 3 };
    const c = new HttpWikiClient(cfg, http(200, JSON.stringify(spec)), noSse);
    await expect(c.deleteSpecItem("i1", 2)).resolves.toEqual(spec);
    await expect(c.deleteExemplar("e1")).resolves.toEqual(spec);
  });

  it("rsiChanges returns the list as sent (newest first)", async () => {
    const list = [{ patch_id: "b" }, { patch_id: "a" }];
    await expect(new HttpWikiClient(cfg, http(200, JSON.stringify(list)), noSse).rsiChanges()).resolves.toEqual(list);
  });

  it("pageFeedback takes the 201 body as sent; undo errors are ApiError (409 = the page changed since)", async () => {
    const out = { signal_id: "s", understood: false, reason: "页面还没生成", reading: null, dropped: [], feedback_id: null, summary: "",
      sections: [], component: null, missing: [], unfolded: [], rsi: "not_understood", llm_calls: 0 };
    await expect(new HttpWikiClient(cfg, http(201, JSON.stringify(out)), noSse)
      .pageFeedback("p1", { chips: [], text: "x", section_id: null })).resolves.toEqual(out);
    await expect(new HttpWikiClient(cfg, http(409, '{"detail":"d"}'), noSse).undoFeedback("p1", "f"))
      .rejects.toMatchObject({ status: 409 });
  });

  it("assetHtml: GET with the user header and no-cache, no Content-Type, returns the text as-is (not JSON)", async () => {
    const html = "<!doctype html><p>不是 JSON</p>";
    const t = http(200, html);
    await expect(new HttpWikiClient(cfg, t, noSse).assetHtml("a/1")).resolves.toBe(html);
    expect(t.calls).toHaveLength(1);
    const req = t.calls[0]!;
    expect(req).toMatchObject({ method: "GET", url: "http://h:1/wiki/assets/a%2F1" });
    expect(req.headers).toEqual({ "X-EvoWiki-User": "jerry", "Cache-Control": "no-cache" });
    expect(req.body).toBeUndefined();
  });

  it("assetHtml: non-2xx → ApiError (same detail mapping as JSON calls)", async () => {
    const p = new HttpWikiClient(cfg, http(404, '{"detail":"no such asset"}'), noSse).assetHtml("a1");
    await expect(p).rejects.toBeInstanceOf(ApiError);
    await expect(p).rejects.toMatchObject({ status: 404, detail: "no such asset" });
  });

  it("assetHtml: OfflineError passes through", async () => {
    const t: WikiHttp = { request: vi.fn(() => Promise.reject(new OfflineError())) };
    await expect(new HttpWikiClient(cfg, t, noSse).assetHtml("a1")).rejects.toBeInstanceOf(OfflineError);
  });

  it("lets OfflineError through unchanged", async () => {
    const t: WikiHttp = { request: vi.fn(() => Promise.reject(new OfflineError())) };
    await expect(new HttpWikiClient(cfg, t, noSse).catalog()).rejects.toBeInstanceOf(OfflineError);
  });

  it("reads config on every call", async () => {
    let user = "a";
    const t = http();
    const c = new HttpWikiClient(() => ({ baseUrl: "http://h:1", userId: user }), t, noSse);
    await c.catalog();
    user = "b";
    await c.catalog();
    expect(t.calls.map((r) => r.headers["X-EvoWiki-User"])).toEqual(["a", "b"]);
  });
});

const OUTLINE = 'event: outline\ndata: {"page_id":"p1","source":"organizer","profile":"","assumed":[],"sections":[]}\n\n';
const DELTA = 'event: section_delta\ndata: {"section_id":"s1","cue":"c","body":"b"}\n\n';
const DONE = 'event: done\ndata: {"page_id":"p1","llm_calls":1,"curated":null}\n\n';

describe("organize (SSE)", () => {
  it("posts to the organize URL and delivers events in order across chunk boundaries", async () => {
    const half = Math.floor(DELTA.length / 2);
    const s = sse([OUTLINE, DELTA.slice(0, half), DELTA.slice(half), DONE]);
    const seen: OrganizeEvent[] = [];
    await new HttpWikiClient(cfg, http(), s).organize("p/1", (e) => seen.push(e));
    expect(s.reqs[0]).toMatchObject({ method: "POST", url: "http://h:1/wiki/pages/p%2F1/organize",
      headers: { "X-EvoWiki-User": "jerry" } });
    expect(s.reqs[0]!.body).toBeUndefined();
    expect(seen.map((e) => e.event)).toEqual(["outline", "section_delta", "done"]);
    expect(seen[1]!.data).toEqual({ section_id: "s1", cue: "c", body: "b" });
  });

  it("passes the abort signal to the transport", async () => {
    const s = sse([DONE]);
    const ac = new AbortController();
    await new HttpWikiClient(cfg, http(), s).organize("p1", () => {}, { signal: ac.signal });
    expect((s.reqs[0] as HttpRequest & { signal?: AbortSignal }).signal).toBe(ac.signal);
  });

  it("drops unknown event names", async () => {
    const seen: string[] = [];
    await new HttpWikiClient(cfg, http(), sse(["event: ping\ndata: {}\n\n", "event: change\ndata: {}\n\n", DONE]))
      .organize("p1", (e) => seen.push(e.event));
    expect(seen).toEqual(["done"]);
  });

  it("skips the server's keep-alive comment frames during a long write", async () => {
    const seen: string[] = [];
    await new HttpWikiClient(cfg, http(), sse([": ping\n\n", OUTLINE, ": ping\n\n: ping\n\n", DONE]))
      .organize("p1", (e) => seen.push(e.event));
    expect(seen).toEqual(["outline", "done"]);
  });

  it("delivers nothing after done", async () => {
    const seen: string[] = [];
    await new HttpWikiClient(cfg, http(), sse([OUTLINE, DONE + DELTA, DELTA]))
      .organize("p1", (e) => seen.push(e.event));
    expect(seen).toEqual(["outline", "done"]);
  });

  it("delivers nothing after error", async () => {
    const seen: string[] = [];
    await new HttpWikiClient(cfg, http(), sse([OUTLINE,
      'event: error\ndata: {"code":"llm_failed","retryable":true,"detail":"x"}\n\n', DELTA, DONE]))
      .organize("p1", (e) => seen.push(e.event));
    expect(seen).toEqual(["outline", "error"]);
  });

  it("rejects with 'malformed event stream' after the transport ends, delivering only earlier events", async () => {
    const s = sse([OUTLINE, "event: section_delta\ndata: {not json\n\n", DONE]);
    const seen: string[] = [];
    await expect(new HttpWikiClient(cfg, http(), s).organize("p1", (e) => seen.push(e.event)))
      .rejects.toThrow("malformed event stream");
    expect(seen).toEqual(["outline"]);
    expect(s.escaped).toEqual([]);
    expect(s.fed).toBe(3);
  });

  it("rejects with the callback's error and stops processing later chunks", async () => {
    const boom = new Error("handler failed");
    const s = sse([OUTLINE, DELTA, DONE]);
    const seen: string[] = [];
    await expect(new HttpWikiClient(cfg, http(), s).organize("p1", (e) => { seen.push(e.event); throw boom; }))
      .rejects.toBe(boom);
    expect(seen).toEqual(["outline"]);
    expect(s.escaped).toEqual([]);
  });

  it("maps a non-2xx stream response to ApiError", async () => {
    const p = new HttpWikiClient(cfg, http(), sse([], 409)).organize("p1", () => {});
    await expect(p).rejects.toEqual(new ApiError(409, "boom"));
  });
});

describe("Task 13 reads: files, writes without ids, run stream", () => {
  it("writes() sends no page_ids; writes(ids) sends them", async () => {
    const h = http(200, "[]");
    const c = new HttpWikiClient(cfg, h, noSse);
    await c.writes();
    await c.writes(["a", "b"]);
    expect(h.calls.map((r) => r.url)).toEqual(["http://h:1/wiki/writes", "http://h:1/wiki/writes?page_ids=a%2Cb"]);
  });
  it("files() is GET /wiki/files and checks each entry", async () => {
    const f = { page_id: "p1", title: "T", folder: "", parent_name: "", trashed: false, organized: true };
    const h = http(200, JSON.stringify([f]));
    expect(await new HttpWikiClient(cfg, h, noSse).files()).toEqual([f]);
    expect(h.calls[0]).toMatchObject({ method: "GET", url: "http://h:1/wiki/files" });
    await expect(new HttpWikiClient(cfg, http(200, JSON.stringify([{ ...f, trashed: "no" }])), noSse).files()).rejects.toThrow();
  });
  it("runStream() is a GET SSE of the page's run; a 404 is an ApiError", async () => {
    const t = sse(['event: done\ndata: {"page_id":"p1","llm_calls":0,"curated":null,"curate_queued":false,"cost_usd":0,"model":"","fallback":false,"prefetched":false}\n\n']);
    const got: string[] = [];
    await new HttpWikiClient(cfg, http(), t).runStream("p1", "r1", (e) => { got.push(e.event); });
    expect(got).toEqual(["done"]);
    expect(t.reqs[0]).toMatchObject({ method: "GET", url: "http://h:1/wiki/pages/p1/run/stream?run_id=r1" });
    await expect(new HttpWikiClient(cfg, http(), sse([], 404)).runStream("p1", "r1", () => undefined)).rejects.toBeInstanceOf(ApiError);
  });
});
