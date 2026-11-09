/**
 * Personal-wiki sidebar contract run against the REAL /wiki backend with a scripted LLM (real HTTP + SSE).
 * Skipped unless EVOWIKI_WIKI_API and EVOWIKI_WIKI_USER are set.
 *
 *   Git Bash, repo root:   uv run python -m tests.e2e.wiki_backend --port 8767
 *   another terminal:      cd obsidian-plugin
 *                          EVOWIKI_WIKI_API=http://127.0.0.1:8767 EVOWIKI_WIKI_USER=e2e-user npm run test:contract -- wiki
 *
 * The backend is single-use (each LLM purpose answers from its own queue, in order): restart it before every run.
 * Its script is E1's z-score → PCA scenario (tests/fixtures/wiki_e2e/script.json) plus tests/e2e/wiki_backend.py's
 * EXTRA entries for the steps after it; the order of the clicks below follows that script.
 *
 * Drives the REAL HttpWikiClient (a small node:http WikiHttp with PUT / DELETE + the node SSE transport) through the
 * REAL WikiController, with E3a's REAL syncWithVault over an in-memory WikiNoteVault (folder <notes folder>/Wiki), so
 * note writes are the plugin's own code. Every response body and every SSE data payload is checked against the
 * pydantic models' JSON Schema (src/generated/wire-schema.json, ./wire-schema.ts; must be []), every click's request sequence is asserted (at most
 * one write per click, except the new-page chain), and at the end the backend must report no unscripted LLM call.
 *
 * The redesigned plugin's reads are part of the sequences: opening the view reads 今天 in one go (the catalog, K, today's
 * review list, 最近为你改了什么, GET /wiki/progress and the teaching spec; every catalog reload reads progress with it), a
 * page the user opens is POST …/open (GET /wiki/pages/{id} only reads), the backend's goals are E1's flat goals (its
 * module doc), 读完了 / the 「⋯」 menu / the feedback dialog send page feedback
 * (POST /wiki/pages/{id}/feedback), the 讲解方式 settings section (TeachModel) reads the teaching spec and writes on its version,
 * a hand-edited note goes up as the page's text, read against the body the plugin wrote (POST
 * …/hand-edit; on a sync and after leaving the note), a second controller (a fresh view) shows that following a note
 * (followPage) reads only the page and opens no note, and the 知识星图 data (K + catalog + paths) goes through the plugin's
 * own buildStarMap / layoutStarMap / frontier. Routes this run does not drive are listed in NOT_DRIVEN, with the reason.
 *
 * The backend sets the test user's RSI cycle to manual, so no background cycle runs and no change applies by itself
 * (undo of a real change is covered by FakeWikiApi + unit tests): this run checks the change list's shape and a 404. Interactives (/wiki/assets/*) answer 503 on this backend and are not driven here.
 *
 * 资料 (/wiki/sources, step 24) goes through the client only (no UI yet): the recorded tests/fixtures/sources/outline.pdf
 * uploaded as multipart, its GET progress stream followed to done, and every other sources route; the backend imports
 * inline without a model.
 */
import { describe, expect, it } from "vitest";
import type { StreamTransport } from "../../src/api/client";
import { SseDecoder } from "../../src/api/sse";
import { nodeStreamTransport } from "../../src/api/transport-node-sse";
import { HttpWikiClient, type WikiHttp } from "../../src/wiki/api";
import { WIKI_COPY } from "../../src/wiki/copy";
import { WikiController } from "../../src/wiki/controller";
import { TeachModel } from "../../src/wiki/teach/model";
import { WIKI_END } from "../../src/wiki/note/format";
import { syncWithVault } from "../../src/wiki/note/sync";
import type { SyncResult, WikiNoteInput } from "../../src/wiki/note-port";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { ApiError } from "../../src/api/errors";
import {
  BYTES, FOLDER, MemVault, ORGANIZE_MODELS, REWRITE_MODELS, ROUTES, SOURCE_MODELS, SOURCE_ROUTES, nodeWikiHttp, isNewPageSyncs, until,
} from "./wiki-kit";
import { loadWireSchema } from "./wire-schema";
import { type CatalogOut, type GoalDraftOut, type PageOut, type SourceEvent } from "../../src/wiki/schema";
import { noteSyncLine, type PageView } from "../../src/wiki/state";
import { buildStarMap, frontier } from "../../src/wiki/starmap";
import { findClaimingNote, wikiHandEdit, wikiNoteEdited } from "../../src/wiki/wiring";

const API = process.env.EVOWIKI_WIKI_API;
const USER = process.env.EVOWIKI_WIKI_USER ?? "";

// E1's scenario (tests/wikiapp/e2e_driver.py) and the key of each scripted question (option 0 before the shuffle).
const Z_GOAL = "我想学 z 分数";
const Z_BACKGROUND = "我会算平均数，方差公式记不清";
const PCA_GOAL = "我想学 PCA";
const PCA_BACKGROUND = "我学过 z 分数，我知道 PCA 用来降维";

/** A section's artifact id (PageOut sections carry one; feedback names it). */
const ARTIFACT: unknown = expect.stringMatching(/^art_/);

/**
 * Routes this run does NOT drive, on purpose (R14.1). They are in ROUTES so that a response from them is still schema
 * checked if a later run drives them, but the scripted backend gives them nothing to answer with (no path page marked
 * done, no review card due, no change waiting) or their final shape is being settled in another window, and covering them here
 * would raise false alarms when that backend work lands. Each is covered by FakeWikiApi and unit tests instead.
 */
const NOT_DRIVEN: readonly string[] = [
  "FeedbackUndoOut",        // POST /wiki/pages/{id}/feedback/{fid}/undo
  "ComponentOut[]",         // GET /wiki/components
  "RsiChangeOut",           // POST /wiki/rsi/patches/{id}/undo (no change exists; the list, RsiChangeOut[], is driven)
  "GuidelinesOut",          // GET /wiki/guidelines (the old rules list; the plugin reads the teaching spec instead)
  "ReviewGradeOut",         // POST /wiki/review/grade (nothing is due on this backend)
  "DigestOut",              // GET /wiki/review/digest
  "PathPageDoneOut",        // POST /wiki/paths/{id}/pages/{n}/done (a path page's 学完了 goes through POST …/done here)
  "GoalMapOut",             // GET /wiki/maps/{id} (big goals: the goals here are small, so no map is made)
];

describe.skipIf(!API || !USER)("personal-wiki contract against tests/e2e/wiki_backend.py (scripted LLM)", () => {
  it("the full sidebar flow over real HTTP / SSE, real note sync, every payload schema-checked", async () => {
    const wire = loadWireSchema();
    const requests: string[] = [];
    const schemaErrs: string[] = [];
    const failures: string[] = [];
    const streams: string[] = [];
    const path = (url: string): string => url.slice(API!.length);
    const seen = new Set<string>();
    const check = (where: string, model: string, value: unknown): void => {
      seen.add(model);
      for (const e of wire.validate(model, value)) schemaErrs.push(`${where}: ${e}`);
    };

    /** Every JSON request body by "METHOD path" (in order), for the steps that assert what was sent. */
    const bodies: [string, unknown][] = [];
    const sentBody = (req: string): unknown => {
      const hit = bodies.filter(([r]) => r === req);
      if (hit.length === 0) throw new Error(`no body sent for ${req}`);
      return hit[hit.length - 1]![1];
    };
    /** Requests still waiting for their answer (click() waits for 0, so reads a click starts in the background count). */
    let inflight = 0;
    const http_: WikiHttp = {
      request: async (req) => {
        const p = path(req.url);
        requests.push(`${req.method} ${p}`);
        if (typeof req.body === "string") bodies.push([`${req.method} ${p}`, JSON.parse(req.body) as unknown]);
        inflight++;
        let res: Awaited<ReturnType<WikiHttp["request"]>>;
        try { res = await nodeWikiHttp.request(req); } finally { inflight--; }
        // this scripted backend has no Bot ledger: the re-read after a hand edit answers 503, as a service without one does
        if (res.status === 503 && p === "/wiki/bot/actions") return res;
        if (res.status < 200 || res.status >= 300) { failures.push(`${req.method} ${p} → ${res.status} ${res.text.slice(0, 300)}`); return res; }
        const route = ROUTES.find(([m, re]) => m === req.method && re.test(p));
        if (!route) schemaErrs.push(`${req.method} ${p}: no model for this route`);
        else if (route[2] === null) { if (res.status !== 204 || res.text !== "") schemaErrs.push(`${req.method} ${p}: expected an empty 204, got ${res.status}`); }
        else if (route[2].endsWith("[]")) {
          const model = route[2].slice(0, -2);
          const list = JSON.parse(res.text) as unknown;
          seen.add(route[2]);
          if (!Array.isArray(list)) schemaErrs.push(`${req.method} ${p}: expected a JSON list`);
          else list.forEach((x: unknown, i) => check(`${req.method} ${p} [${i}]`, model, x));
        }
        else check(`${req.method} ${p}`, route[2], JSON.parse(res.text) as unknown);
        return res;
      },
    };
    const sse: StreamTransport = {
      stream: async (req, onChunk) => {
        const p = path(req.url);
        requests.push(`${req.method} ${p} (SSE)`);
        let raw = "";
        inflight++;
        let res: Awaited<ReturnType<StreamTransport["stream"]>>;
        try { res = await nodeStreamTransport.stream(req, (c) => { raw += c; onChunk(c); }); } finally { inflight--; }
        // this scripted backend has no Bot ledger: the re-read after a hand edit answers 503, as a service without one does
        if (res.status === 503 && p === "/wiki/bot/actions") return res;
        if (res.status < 200 || res.status >= 300) { failures.push(`${req.method} ${p} → ${res.status} ${res.text.slice(0, 300)}`); return res; }
        const models = /\/organize(\?|$)/.test(p) ? ORGANIZE_MODELS : REWRITE_MODELS;   // also 重新组织's ?fresh=true
        const msgs = new SseDecoder().push(raw + "\n\n");
        for (const m of msgs) {
          const model = models[m.event];
          if (model === undefined) schemaErrs.push(`${p}: unknown event ${m.event}`);
          else check(`${p} ${m.event}`, model, m.data);
        }
        streams.push(`${p} → ${msgs.map((m) => m.event).join(",")}`);
        return res;
      },
    };

    const vault = new MemVault();
    let folder = FOLDER;
    const runs: { input: WikiNoteInput; result: SyncResult | "threw" }[] = [];
    const opened: string[] = [];
    let n = 0;
    /** The Notices the controller showed. */
    const notices: string[] = [];
    /** the hand edits' ledger (main.ts persists it per server + user + page; one map for this run's single identity). */
    /** A controller on the real client, the in-memory vault and the shared request log (one per open wiki view). */
    const makeController = (): WikiController => new WikiController({ blockEventStore: null, blockProgressStore: null,
      api: new HttpWikiClient(() => ({ baseUrl: API!, userId: USER }), http_, sse),
      conn: { state: "online", track: (p) => p, onChange: () => () => undefined },
      sync: {
        run: async (input, o) => {
          try {
            const r = await syncWithVault({ store: vault, now: () => new Date(Date.UTC(2026, 9, 8, 8, 0, 0, ++n)), rand: () => `r${n}` },
              folder, input, o?.accepted);
            runs.push({ input, result: r });
            return r;
          } catch (e) { runs.push({ input, result: "threw" }); throw e; }
        },
        find: (pageId) => findClaimingNote(vault, folder, pageId),
        handEdit: (input) => wikiHandEdit(vault, folder, input),
        edited: (p, pageId) => wikiNoteEdited(vault, folder, p, pageId),
        isOpen: () => false,
      },
      openNote: (p) => { const ok = vault.files.has(p); if (ok) opened.push(p); return ok; },
      configured: () => true,
      demo: false,
      notice: (text) => { notices.push(text); },
    });
    const c = makeController();

    /** Waits until no request is out (a click's background reads: 目录's change list, a hand edit after a sync). */
    const settle = async (): Promise<void> => {
      await new Promise((r) => setTimeout(r, 0));
      await until(() => inflight === 0);
    };
    /** Runs one click; returns the requests it made (schema / HTTP failures fail the step). */
    const click = async (label: string, f: () => Promise<void> | void): Promise<string[]> => {
      const r0 = requests.length;
      await f();
      await settle();
      const made = requests.slice(r0);
      console.log(`[wiki-contract] ${label}: ${made.join(" | ") || "(no request)"}`);
      expect(schemaErrs, label).toEqual([]);
      expect(failures, label).toEqual([]);
      expect(c.state.busy, label).toBeNull();
      return made;
    };
    const pv = (): PageView => {
      const p = c.state.page;
      if (p.kind !== "open") throw new Error(`page is ${p.kind}: ${JSON.stringify(p)}`);
      return p;
    };
    const cat = (): CatalogOut => {
      const k = c.state.catalog;
      if (k.kind !== "ready") throw new Error(`catalog is ${k.kind}`);
      return k.value;
    };
    const sec = (page: PageOut, sid: string) => {
      const s = page.sections.find((x) => x.id === sid);
      if (!s) throw new Error(`no section ${sid}`);
      return s;
    };
    /**
     * A goal made as the chat's goal_new makes it: POST /wiki/goals is gone (goals are made only in the 助手), so the
     * backend's TEST ONLY route makes E1's flat goal (tests/e2e/wiki_backend.py, module doc) and answers its round 1.
     */
    const newGoal = async (goal: string, background: string): Promise<GoalDraftOut> => {
      const r = await nodeWikiHttp.request({ method: "POST", url: `${API!}/test/flat-goal`, headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal, words: [background] }) });
      expect(r.status, `flat goal ${goal}`).toBe(201);
      const out = JSON.parse(r.text) as GoalDraftOut;
      check(`flat goal ${goal}`, "GoalDraftOut", out);
      return out;
    };
    /** E1 (tests/wikiapp/e2e_driver) verifies each goal's prerequisites right after creating it (relation layers: only
     *  verified prerequisites feed the diagnosis, K and the curator); the backend's TEST ONLY route does the same here. */
    const verifyPrereqs = async (goal: "z" | "pca"): Promise<void> => {
      const r = await nodeWikiHttp.request({ method: "POST", url: `${API!}/test/verify-prereqs?goal=${goal}`, headers: {} });
      expect(r.status, `verify ${goal}`).toBe(204);
    };
    /** The diagnosis is the chat's (next_check / ask_check: the assistant writes each round's question, one per round), so
     *  each round is answered through the backend's TEST ONLY route the way E1's driver answers it: the scripted
     *  assistant's question on the round's pick, answered with its key (tests/e2e/wiki_backend.py, module doc). Returns
     *  the finished draft (its path_id: the block's path is planned there). */
    const answerRounds = async (label: string, first: GoalDraftOut): Promise<GoalDraftOut> => {
      const asked: string[] = [];
      const draftId = first.draft_id!;
      let d = first;
      for (let r = 1; r <= 10; r++) {
        const res = await nodeWikiHttp.request({ method: "POST", url: `${API!}/test/answer-check?draft_id=${encodeURIComponent(draftId)}`, headers: {} });
        expect(res.status, `${label} round ${r}`).toBe(201);
        const out = JSON.parse(res.text) as { stem: string; draft: GoalDraftOut };
        check(`${label} round ${r}`, "GoalDraftOut", out.draft);
        asked.push(out.stem);
        d = out.draft;
        if (d.draft_id !== null) continue;
        console.log(`[wiki-contract] ${label} asked: ${asked.join(" | ")}`);
        expect(d.path_id).not.toBeNull();
        return d;
      }
      throw new Error(`${label}: the diagnosis did not end in 10 rounds`);
    };

    // ---- 0. open the view: GET /wiki, then today's review list (a read: the 复习 tab's count); change 每天最多 (PUT, only
    // that field); 目录 reads K (掌握情况 is at its bottom)
    // 今天 is read in one go: the catalog, K, today's review list, the recent changes, where learning goes on (progress) and
    // the teaching spec (they run side by side, so any order)
    expect((await click("init", () => c.init())).sort()).toEqual(["GET /wiki", "GET /wiki/k", "GET /wiki/progress", "GET /wiki/review/today",
      "GET /wiki/rsi/changes", "GET /wiki/teach/spec"]);
    expect(cat().learning).toEqual([]);
    expect(c.state.review.today.kind).toBe("ready");
    expect(await click("每天最多", () => c.setReviewPerDay(20))).toEqual(["PUT /wiki/settings", "GET /wiki", "GET /wiki/progress", "GET /wiki/review/today"]);
    expect(sentBody("PUT /wiki/settings")).toEqual({ review_per_day: 20 });
    expect(c.state.review.note?.text).toBe(WIKI_COPY.settingsSaved);
    expect(c.state.k.kind).toBe("ready");
    expect(c.state.rsi).toEqual({ kind: "ready", value: [] });
    /** Back to 今天 from the other tab: nothing to read while 今天's last read is fresh (the old 目录 tab re-read the changes). */
    const toCatalog = async (label: string): Promise<void> => {
      expect(await click(label, () => c.setTab("catalog"))).toEqual([]);
    };

    // ---- 1. first goal (as the chat makes it) → its rounds answered → its page written (the chat's goal run: the new-page chain)
    c.setTab("catalog");
    const zDraft = await newGoal(Z_GOAL, Z_BACKGROUND);
    await verifyPrereqs("z");
    const zDone = await answerRounds("z", zDraft);
    // the chat's goal run writes the path's first page (controller.learnFirstPage: POST next, then the new-page chain)
    const zMade = await click("page z", () => c.learnFirstPage(zDone.path_id!));
    const z = pv().page.page_id;
    expect(zMade).toEqual(expect.arrayContaining([`POST /wiki/paths/${zDone.path_id}/next`, `POST /wiki/pages/${z}/organize (SSE)`,
      `POST /wiki/pages/${z}/open`]));
    expect(z).toBe("pg_0001");
    expect(pv().page.organized).toBe(true);
    expect(pv().page.sections.map((s) => s.id)).toEqual(["s1", "s2", "s3", "s4"]);
    // 「改了」: generating writes no markers.
    expect(pv().touched).toEqual({});
    // old WALKTHROUGH (685f6c2) 24: the note is created at <folder>/Wiki/… and opened.
    // Since 571db1e1 (single-writer contract) the sections stream into the note: a partial write per landed section, then
    // the full sync of the landed page (nothing left to change: "unchanged"). Partial writes that land while one is being
    // written are coalesced, so there are 2 to 4 streamed writes for the 4 sections (streamedSyncs).
    const zSeq = runs.map((r) => r.result);
    expect(isNewPageSyncs(zSeq, 4), zSeq.join()).toBe(true);
    expect(zSeq.length).toBeLessThanOrEqual(5);
    const nZ = runs.length;
    const zNote = vault.claiming(z);
    expect(zNote).toHaveLength(1);
    // Since b40efdf4 (files follow the graph) the note lives in its page's folder (the top-level graph group, PageOut.folder).
    // The streamed writes come before the GET that carries the folder, so the note is written and opened at the Wiki root
    // and the landing full sync moves it into the folder (renameFile: an open tab follows the file).
    // the block's path page covers its three concepts: its title names them
    expect(pv().page.title).toBe("z 分数、均值、方差");
    expect(zNote).toEqual([`${FOLDER}/z 分数/z 分数、均值、方差.md`]);
    expect(pv().page.folder).toBe("z 分数");
    // opened at the Wiki root under its own title before the landing sync moved it into the folder
    const zOpened = `${FOLDER}/z 分数、均值、方差.md`;
    expect(opened).toEqual([zOpened]);
    expect(pv().noteSync).toEqual({ kind: "done", result: zSeq[zSeq.length - 1] }); // the landing full sync follows the streamed writes (571db1e1)

    const zNotePath = zNote[0]!;

    // ---- 2. old WALKTHROUGH (685f6c2) 24: opening the page again (nothing changed) does not sync
    expect(await click("open z (unchanged)", () => c.openWikiPage(z))).toEqual([`POST /wiki/pages/${z}/open?rewrite=1`]);
    expect(pv().page.diff).toBeNull();
    expect(runs).toHaveLength(nZ);
    expect(opened).toEqual([zOpened, zNotePath]);

    // ---- 3. 这部分我懂 on the mean recap (E1 step 2, 0 calls) → 「改了」 folded; note updated, backup written first
    const knowMade = await click("know s2", () => c.know("s2"));
    expect(knowMade).toEqual([`POST /wiki/pages/${z}/sections/s2/feedback`, `POST /wiki/pages/${z}/open`, "GET /wiki", "GET /wiki/progress", "GET /wiki/k"]);
    expect(sec(pv().page, "s2").folded).toBe(true);
    expect(pv().touched).toEqual({ s2: "folded" });
    expect(pv().sectionNotes.s2?.text).toContain("已折叠「均值提醒」");
    expect(runs.map((r) => r.result)).toEqual([...zSeq, "updated"]);
    // Backup, then the written copy of the new body (Q8), then the note.
    const w = vault.writes.slice(-3);
    expect(w[0]).toMatch(/^create \.evowiki\/backups\/.+\.md\.bak$/);
    expect(w[1]).toMatch(/^create \.evowiki\/written\/[0-9a-f]{32}\/[0-9a-f]{64}\.json$/);
    expect(w[2]).toBe(`process ${zNotePath}`);
    expect(runs[nZ]!.input.sections.find((s) => s.id === "s2")!.folded).toBe(true);

    // ---- 4. second goal (PCA): every question answered right; generate → the curator runs (new goal)
    await toCatalog("tab 目录 (before goal pca)");
    const pDraft = await newGoal(PCA_GOAL, PCA_BACKGROUND);
    await verifyPrereqs("pca");
    const pDone = await answerRounds("pca", pDraft);
    const pMade = await click("page pca", () => c.learnFirstPage(pDone.path_id!));

    const pca = pv().page.page_id;
    expect(pca).toBe("pg_0002");
    expect(pMade).toEqual(expect.arrayContaining([`POST /wiki/paths/${pDone.path_id}/next`, `POST /wiki/pages/${pca}/organize (SSE)`,
      `POST /wiki/pages/${pca}/open`]));
    expect(pv().touched).toEqual({});
    const pSeq = runs.map((r) => r.result).slice(nZ + 1);
    console.log(`[wiki-contract] pca note runs: ${pSeq.join(",")}`);
    // as for z: streamed writes, then the landing full sync with nothing left to change (571db1e1)
    expect(isNewPageSyncs(pSeq, 4), pSeq.join()).toBe(true);
    expect(pSeq.length).toBeLessThanOrEqual(5);
    const nP = runs.length;
    const pcaNotePath = vault.claiming(pca)[0]!;
    expect(pcaNotePath.startsWith(`${FOLDER}/`)).toBe(true);
    // 整理 is gone: nothing runs after the new goal but the page itself (no curate run, no drafts to decide on).
    expect(cat().learning.map((x) => [x.page_id, x.stale])).toEqual([[z, true], [pca, false]]);

    // ---- 5. the z page is stale: opening it brings it up to date (lazy update, 0 calls) → diff note + sync. 方差's
    // notation was not quizzed again in the PCA goal (big-goal-reuse: K already had evidence), so it is only likely and
    // the variance recap stays open: no fold, no 「改了」 marker.
    const reopen = await click("open z (stale)", () => c.openWikiPage(z));
    expect(reopen).toEqual([`POST /wiki/pages/${z}/open?rewrite=1`]);
    expect(pv().page.diff).toEqual({ cell_changes: [{ cell: "c3.notation", from: "shaky", to: "likely" }], folded: [], unfolded: [], patched: [] });
    expect(sec(pv().page, "s3").folded).toBe(false);
    expect(pv().page.llm_calls).toBe(0);
    // A3-3 (the GET carries a diff) → the note is updated.
    expect(pv().touched).toEqual({});
    expect(runs.map((r) => r.result).slice(nP)).toEqual(["updated"]);
    expect(last(runs)!.input.diffText).not.toBeNull();
    // Pages never move folders: the z page's file stays where it was written.
    const zMoved = zNotePath;
    expect(vault.claiming(z)).toEqual([zMoved]);
    const nZStale = runs.length;

    // ---- 6. (was 整理: accept / undo the curator's link and hub, 整理我的 Wiki; retired with the curator)
    const n6 = runs.length;
    expect(n6).toBe(nZStale);

    // ---- 8. 钉住 the opener: one POST, written back, no GET, no sync
    expect(await click("pin s1", () => c.togglePin("s1"))).toEqual([`POST /wiki/pages/${z}/sections/s1/pin`]);
    expect(sec(pv().page, "s1").pinned).toBe(true);
    expect(pv().sectionNotes.s1?.text).toBe(WIKI_COPY.pinned("从你会的平均数出发"));
    expect(runs).toHaveLength(n6);

    // ---- 10. a hand edit in the note: on the next sync it becomes the section's text on the server, the section is locked,
    // the note reads what he typed and the sync that follows does not move it
    const edited = vault.files.get(zMoved)!.replace("均值 μ = Σx / N。", "均值 μ = Σx / N。我自己加的一句。");
    expect(edited).not.toBe(vault.files.get(zMoved));
    vault.files.set(zMoved, edited);
    const zEdits = `POST /wiki/pages/${z}/hand-edit`;
    // (a hand edit is a row of 活动记录: its reads follow)
    expect((await click("unfold s2 (note edited)", async () => {
      await c.toggleFold("s2");
      await until(() => requests.includes(zEdits));
    })).slice(0, 2)).toEqual([`POST /wiki/pages/${z}/sections/s2/fold`, zEdits]);
    expect((sentBody(zEdits) as { summary: string }).summary).toContain("我自己加的一句。");
    expect(last(runs)!.result).toBe("updated");
    const merged = vault.files.get(zMoved)!;
    const end = merged.indexOf(WIKI_END);
    expect(end).toBeGreaterThan(0);
    expect(merged.slice(0, end)).toContain("我自己加的一句。");
    expect(merged.slice(end)).not.toContain("我自己加的一句。");
    const editedSec = pv().page.sections.find((x) => x.body.includes("我自己加的一句。"))!;
    expect(editedSec.pinned).toBe(true);
    expect(editedSec.note_body).toContain("我自己加的一句。");
    expect(pv().noteSync).toEqual({ kind: "done", result: "updated" });
    const before = opened.length;
    c.openCurrentNote();
    await until(() => opened.length > before);
    expect(last(opened)).toBe(zMoved);

    // ---- 11. old WALKTHROUGH (685f6c2) 29: two notes claim the page → the sync throws: notice, nothing written, no retry
    vault.files.set(`${FOLDER}/z 分数 副本.md`, vault.files.get(zMoved)!);
    const snapshot = new Map(vault.files);
    const runsBefore = runs.length;
    expect(await click("fold s2 (two claimants)", () => c.toggleFold("s2"))).toEqual([`POST /wiki/pages/${z}/sections/s2/fold`]);
    expect(runs.slice(runsBefore).map((r) => r.result)).toEqual(["threw"]);
    expect(pv().noteSync).toEqual({ kind: "error" });
    expect(noteSyncLine(pv().noteSync)).toEqual({ text: WIKI_COPY.noteSyncFailed, tone: "error" });
    expect(vault.files).toEqual(snapshot);
    await new Promise((r) => setTimeout(r, 200));
    expect(runs).toHaveLength(runsBefore + 1);
    vault.files.delete(`${FOLDER}/z 分数 副本.md`);
    // …and an invalid folder setting throws the same way.
    folder = "../outside";
    expect(await click("unfold s2 (bad folder)", () => c.toggleFold("s2"))).toEqual([`POST /wiki/pages/${z}/sections/s2/fold`]);
    expect(runs.slice(runsBefore + 1).map((r) => r.result)).toEqual(["threw"]);
    expect(noteSyncLine(pv().noteSync)?.text).toBe(WIKI_COPY.noteSyncFailed);
    await new Promise((r) => setTimeout(r, 200));
    expect(runs).toHaveLength(runsBefore + 2);
    folder = FOLDER;

    // ---- 12. old WALKTHROUGH (685f6c2) 31: the note is gone → opening it writes it once (no extra request) and opens it
    vault.files.delete(zMoved);
    expect(await click("open z (note deleted)", () => c.openWikiPage(z))).toEqual([`POST /wiki/pages/${z}/open?rewrite=1`]);
    expect(pv().page.diff).toBeNull();
    expect(runs.slice(runsBefore + 2).map((r) => r.result)).toEqual(["created"]);
    const zNote2 = vault.claiming(z);
    expect(zNote2).toHaveLength(1);
    expect(last(opened)).toBe(zNote2[0]);
    expect(pv().noteSync).toEqual({ kind: "done", result: "created" });

    // ---- 13. the PCA page, nothing changed for it since it was written
    const r13 = runs.length;
    const openPca = await click("open pca", () => c.openWikiPage(pca));
    expect(openPca).toEqual([`POST /wiki/pages/${pca}/open?rewrite=1`]);
    expect(pv().page.diff).toBeNull();
    expect(pv().page.llm_calls).toBe(0);
    expect(runs.slice(r13).map((r) => r.result)).toEqual([]);
    expect(pv().touched).toEqual({});
    // nothing moved the PCA page (step 6's 整理 had nothing to change): its file stays where it was written
    const pcaMoved = pcaNotePath;
    expect([pcaNotePath, vault.claiming(pca)]).toEqual([`${FOLDER}/主成分分析、方差/主成分分析、z 分数、方差.md`, [pcaMoved]]);

    // ---- 14. 按当前状态重新组织 the z page (EXTRA goals.organize with a check + its verify + s1's text)
    expect(await click("open z", () => c.openWikiPage(z))).toEqual([`POST /wiki/pages/${z}/open?rewrite=1`]);
    const r14 = runs.length;
    expect(await click("reorganize z", () => c.organize()))
      .toEqual([`POST /wiki/pages/${z}/organize?fresh=true (SSE)`, `POST /wiki/pages/${z}/open`, "GET /wiki", "GET /wiki/progress", "GET /wiki/k"]);
    // one planning event before the outline (the organizer's separate revise pass is gone: one planning call)
    expect(last(streams)).toBe(`/wiki/pages/${z}/organize?fresh=true → planning,outline,writer,${Array<string>(6).fill("section_delta,section_done").join(",")},done`);
    expect(pv().status).toEqual({ text: WIKI_COPY.organizeDone, tone: "ok" }); // organize + verify + s1 (never a call count)
    expect(pv().page.sections.map((x) => [x.id, x.role, x.changed, x.pinned]))
      // s1 (钉住 in step 8) and s2 (the hand edit of step 10) stay as they are: a reorganize keeps locked sections
      .toEqual([["s1", "line", null, true], ["s2", "recap", null, true], ["s3", "recap", "rewritten", false],
        ["s4", "explain", "rewritten", false], ["s5", "check", null, false]]); // the single writer (3d9ba51c) writes every prose section: no per-section cache
    expect(sec(pv().page, "s5").check).toEqual({ stem: "z = 2 表示什么？", // the inserted check's options are shuffled deterministically (patch._shuffle_inserted / organize), the key is second
      options: ["比均值高 2 分", "比均值高 2 个标准差", "高于 2% 的人", "无法判断"],
      answered: null, correct_index: null, correct: null, artifact_id: ARTIFACT });
    expect(pv().touched).toEqual({}); // 「改了」: a reorganize starts this visit's markers over and writes none
    // the streamed writes (outline, then the writer; coalesced, so their number varies), the landing full sync
    const r14Seq = runs.slice(r14).map((r) => r.result);
    expect(r14Seq[r14Seq.length - 1]).toBe("unchanged");
    expect(r14Seq.slice(0, -1).length).toBeGreaterThan(0);
    expect(r14Seq.slice(0, -1).every((x) => x === "updated"), r14Seq.join()).toBe(true);
    const nReorg = runs.length;
    // The note carries the check's stem and options, never the answer.
    expect(last(runs)!.input.sections.find((x) => x.id === "s5")!.body)
      .toBe("z = 2 表示什么？\n- A. 比均值高 2 分\n- B. 比均值高 2 个标准差\n- C. 高于 2% 的人\n- D. 无法判断");

    // ---- 15. 确认题 (answered right). 方差's notation is only likely since the PCA goal stopped re-asking it
    // (big-goal-reuse; the check's inference on c3.notation keeps it below known), so the variance recap does NOT
    // fold: no section changes, only the other page goes stale; no verdict sentence (user review), no note sync.
    const s5 = sec(pv().page, "s5").check!;
    expect(await click("check s5", () => c.answerCheck("s5", s5.options.indexOf("比均值高 2 个标准差"))))
      .toEqual([`POST /wiki/pages/${z}/checks/s5`, `POST /wiki/pages/${z}/open`, "GET /wiki", "GET /wiki/progress", "GET /wiki/k"]);
    expect(pv().sectionNotes.s5).toBeUndefined();
    expect(sec(pv().page, "s5").check).toMatchObject({ answered: 1, correct_index: 1, correct: true });
    expect(sec(pv().page, "s3").folded).toBe(false);
    expect(pv().touched).toEqual({});
    expect(runs.slice(r14).map((r) => r.result)).toEqual(r14Seq); // the reorganize's runs only (see step 14); answering writes nothing

    // ---- 16. 这一页我学完了
    expect(await click("done", () => c.done())).toEqual([`POST /wiki/pages/${z}/done`, `POST /wiki/pages/${z}/open`, "GET /wiki", "GET /wiki/progress", "GET /wiki/k"]);
    expect(pv().status).toEqual({ text: WIKI_COPY.pageDone, tone: "ok" });
    expect(pv().page.done).toBe(true);
    expect(cat().learning.find((x) => x.page_id === z)?.done).toBe(true);
    expect(runs).toHaveLength(nReorg); // 学完 writes no note

    // ---- 17. K: 我不会 / 清除声明 / 我会 on the PCA cells (the current page is z, so only PCA goes stale)
    await toCatalog("tab 目录 (before K declares)");
    const declared = (cell: string): string | null | undefined => {
      const k = c.state.k;
      if (k.kind !== "ready") throw new Error(`k is ${k.kind}`);
      return k.value.concepts.flatMap((x) => x.cells).find((x) => x.cell === cell)?.declared;
    };
    const r17 = runs.length;
    const declares = [["c4.purpose", "dont", "用途 / 何时用"], ["c4.purpose", null, "用途 / 何时用"], ["c4.purpose", "know", "用途 / 何时用"],
      ["c4.procedure", "know", "计算过程"], ["c4.interpret", "know", "解读结果"]] as const;
    for (const [cell, v, label] of declares) {
      expect(await click(`declare ${cell} ${v}`, () => c.declareCell(cell, v)))
        .toEqual([`POST /wiki/k/cells/${cell}/declare`, `POST /wiki/pages/${z}/open`, "GET /wiki", "GET /wiki/progress", "GET /wiki/k"]);
      expect(c.state.kNote).toEqual({ tone: "ok",
        text: `你修改了对“主成分分析·${label}”的判断；另外 1 个页面标记为“待更新”，下次打开时再处理` });
      expect(declared(cell)).toBe(v);
    }
    expect(runs).toHaveLength(r17); // z did not change

    // ---- 18. 讲解方式 / 样板页 (the settings section reads the spec and deletes a 样板页 with TeachModel; the rest of the
    // teach API is now driven by the assistant on the server, so it is called on the client directly here): add / edit /
    // delete a rule on the shown version; an ordinary vault note as a 样板页 then deleted (TeachModel); the z page as a 样板页 (wiki_page); a rule added
    // through /wiki/guidelines
    const teach = new TeachModel(c.teachPort());
    const tapi = c.teachPort().api;
    const spec = () => {
      const s = teach.state.spec;
      if (s.kind !== "ready") throw new Error(`spec is ${s.kind}: ${JSON.stringify(s)}`);
      return s.spec;
    };
    expect(await click("open 样板页", () => teach.open())).toEqual(["GET /wiki/teach/spec"]);
    let cur = spec();
    const v0 = cur.version;
    expect(await click("add rule", async () => {
      cur = await tapi.addSpecItem({ text: "多举生活里的例子", scope: "any", domain: null }, v0);
    })).toEqual([`POST /wiki/teach/items?base_version=${v0}`]);
    const added = cur.items[cur.items.length - 1]!;
    expect([added.text, added.authority]).toEqual(["多举生活里的例子", "user"]);
    const v1 = cur.version;
    expect(v1).toBeGreaterThan(v0);
    const itemPath = `/wiki/teach/items/${encodeURIComponent(added.item_id)}`;
    expect(await click("edit rule", async () => {
      cur = await tapi.editSpecItem(added.item_id, { text: "多举生活里的例子，少用比喻" }, v1);
    })).toEqual([`PATCH ${itemPath}?base_version=${v1}`]);
    expect(sentBody(`PATCH ${itemPath}?base_version=${v1}`)).toEqual({ text: "多举生活里的例子，少用比喻" });
    expect(cur.items.find((x) => x.item_id === added.item_id)?.text).toBe("多举生活里的例子，少用比喻");
    const v2 = cur.version;
    expect(await click("delete rule", async () => { cur = await tapi.deleteSpecItem(added.item_id, v2); }))
      .toEqual([`DELETE ${itemPath}?base_version=${v2}`]);
    expect(cur.items.map((x) => x.item_id)).not.toContain(added.item_id);

    const plainPath = "读书笔记/我的写法.md";
    const v4 = cur.version;
    expect(await click("样板页: an ordinary note", async () => {
      cur = await tapi.addExemplar({ kind: "vault_note", ref: plainPath, title: "我的写法", text: "先举一个例子，再讲道理。" }, v4);
    })).toEqual([`POST /wiki/teach/exemplars?base_version=${v4}`]);
    expect(await click("样板页: read again", () => teach.open())).toEqual(["GET /wiki/teach/spec"]);
    const plainEx = spec().exemplars.find((x) => x.ref === plainPath);
    expect(plainEx?.kind).toBe("vault_note");
    const v5 = spec().version;
    expect(await click("样板页: delete it", () => teach.deleteExemplar(plainEx!.exemplar_id)))
      .toEqual([`DELETE /wiki/teach/exemplars/${encodeURIComponent(plainEx!.exemplar_id)}?base_version=${v5}`]);
    expect(spec().exemplars).toEqual([]);
    const v6 = spec().version;
    expect(await click("样板页: the z page", async () => { cur = await tapi.addExemplar({ kind: "wiki_page", ref: z }, v6); }))
      .toEqual([`POST /wiki/teach/exemplars?base_version=${v6}`]);
    expect(sentBody(`POST /wiki/teach/exemplars?base_version=${v6}`)).toEqual({ kind: "wiki_page", ref: z });
    expect(cur.exemplars.map((x) => [x.kind, x.ref])).toEqual([["wiki_page", z]]);

    expect(await click("add a rule", async () => {
      await tapi.addGuideline({ text: "先给数值例子，再给公式" });
      cur = await tapi.teachSpec();
    })).toEqual(["POST /wiki/guidelines", "GET /wiki/teach/spec"]);
    expect(cur.items.map((x) => x.text)).toContain("先给数值例子，再给公式");
    teach.dispose();

    // ---- 18b. page feedback on the z page (never a /wiki/guidelines or /wiki/teach request): 读完觉得 太浅了 / 有点难 send
    // one page-level feedback each, 正好 and a repeat send nothing; the 「⋯」 menu's 太浅了 sends one with its section
    const fb = `POST /wiki/pages/${pv().page.page_id}/feedback`;
    expect(await click("feel shallow", () => c.feedback(null, ["too_shallow"]))).toEqual([fb]);
    expect(sentBody(fb)).toEqual({ chips: ["too_shallow"], text: "", section_id: null, apply: false });
    expect(last(notices)).toBe(WIKI_COPY.feelSaved);
    expect(await click("feel hard", () => c.feedback(null, ["too_hard"]))).toEqual([fb]);
    expect(sentBody(fb)).toEqual({ chips: ["too_hard"], text: "", section_id: null, apply: false });
    expect(await click("⋯ 太浅了 on s4", () => c.feedback("s4", ["too_shallow"]))).toEqual([fb]);
    // a section's feedback names the section and the artifact it reads (PageOut's section artifact)
    expect(sentBody(fb)).toEqual({ chips: ["too_shallow"], text: "", section_id: "s4", apply: false, artifact_id: ARTIFACT });
    expect(last(notices)).toBe(WIKI_COPY.feelSaved);

    // ---- 18c. 最近为你改了什么 (目录): the list is empty on this backend (RSI manual, no change) and schema-checked; 撤销 on
    // an unknown change → 404 → the plugin's own sentence, then the list is read again
    expect(await click("tab 本页", () => c.setTab("page"))).toEqual([]);
    await toCatalog("tab 目录 again (changes)");
    expect(c.state.rsi).toEqual({ kind: "ready", value: [] });
    expect(await click("undo an unknown change", async () => {
      const f0 = failures.length;
      await c.undoPatch("nope");
      expect(failures.splice(f0).map((x) => x.slice(0, x.indexOf(" → ") + 6)))
        .toEqual(["POST /wiki/rsi/patches/nope/undo → 404"]);
    })).toEqual(["POST /wiki/rsi/patches/nope/undo", "GET /wiki/rsi/changes"]);
    expect(c.state.rsiNote).toEqual({ patchId: "nope", note: { text: WIKI_COPY.rsiGone, tone: "error" } });

    // ---- 19. 收为参考卡 (the learner's own; 整理 no longer collapses pages): offered only while the page's goal is all
    // 确定会 (PageOut.can_shelve). When offered: POST collapse → the note is synced as a reference card; 恢复成完整页 →
    // POST expand → it unfolds again. Not offered: the plugin sends nothing.
    const open19 = await click("open pca", () => c.openWikiPage(pca));
    expect(open19[0]).toMatch(new RegExp(`^POST /wiki/pages/${pca}/open`));
    expect(pv().page.page_id).toBe(pca);
    expect(last(opened)).toBe(pcaMoved);
    const reads19 = [`POST /wiki/pages/${pca}/open`, "GET /wiki", "GET /wiki/progress", "GET /wiki/k"];
    if (pv().page.can_shelve) {
      expect(await click("收为参考卡 pca", () => c.collapse())).toEqual([`POST /wiki/pages/${pca}/collapse`, ...reads19]);
      expect(pv().page.collapsed).toBe(true);
      expect(last(runs)!.input).toMatchObject({ pageId: pca, collapsed: true });
      expect(cat().shelf.pages.map((x) => x.page_id)).toEqual([pca]);
      expect(await click("expand pca", () => c.expand())).toEqual([`POST /wiki/pages/${pca}/expand`, ...reads19]);
      expect(pv().page.collapsed).toBe(false);
      expect(last(runs)!.input).toMatchObject({ pageId: pca, collapsed: false });
    } else {
      expect(await click("收为参考卡 (not offered)", () => c.collapse())).toEqual([]);
    }

    // ---- 19b. a hand edit: the user edits one line of the PCA note's text. The edit is read against the
    // body the plugin last wrote (its copy under .evowiki/written/, named by the note's evowiki_hash), so a sync after
    // the page changed on the server (fold) still takes it, once (POST …/hand-edit). The same
    // edit again (leaving the note) is not sent twice; a further edit, on leaving the note, is.
    const pcaNow = vault.claiming(pca)[0]!;
    const fit = (l: string): boolean => l.trim().length > 4 && l.length < 150 && !l.startsWith(">");
    const pcaRun = [...runs].reverse().find((r) => r.input.pageId === pca)!;
    const shownSec = pcaRun.input.sections.find((x) => !x.folded && x.role !== "check" && x.body.split("\n").some(fit));
    if (!shownSec) throw new Error(`no editable section: ${JSON.stringify(pcaRun.input.sections.map((x) => [x.id, x.role, x.folded]))}`);
    const editLine = shownSec.body.split("\n").find(fit)!;
    // A section the 折叠 button works on (foldable, not the edited one): toggling it changes the page on the server (a shown
    // one is folded; when every other one is folded already, one is unfolded), and the next toggle turns it back.
    const foldSid = pv().page.sections.find((x) => x.foldable && !x.folded && x.id !== shownSec.id)?.id
      ?? pv().page.sections.find((x) => x.foldable && x.id !== shownSec.id)!.id;
    const pcaText = vault.files.get(pcaNow)!;
    expect(pcaText.split(editLine)).toHaveLength(2);
    // the note line that holds it (a one-line section is written as 「- **heading**：text」): the diff is by note lines
    const noteLine = pcaText.split("\n").find((l) => l.includes(editLine))!.trim();
    vault.files.set(pcaNow, pcaText.replace(editLine, `${editLine}我自己补一句。`));
    const edits = `POST /wiki/pages/${pca}/hand-edit`;
    const r19b = runs.length;
    expect((await click("fold (note edited, page changed on the server: one report)", async () => {
      await c.toggleFold(foldSid);
      await until(() => requests.includes(edits));
    })).slice(0, 2)).toEqual([`POST /wiki/pages/${pca}/sections/${foldSid}/fold`, edits]);
    // the edit is on the server, the note reads what he typed
    expect(runs.slice(r19b).map((r) => r.result)).toEqual(["updated"]);
    const editBody = sentBody(edits) as { summary: string; sections: { section_id: string; body: string; note_body: string }[]; removed: string[] };
    expect(editBody.summary).toContain(`删：${noteLine}`);
    expect(editBody.summary).toContain(`加：${noteLine}我自己补一句。`);
    expect(editBody.sections.map((x) => x.section_id)).toEqual([shownSec.id]);
    expect(editBody.removed).toEqual([]);
    const kept = vault.files.get(pcaNow)!;
    expect(kept.slice(0, kept.indexOf(WIKI_END))).toContain(`${editLine}我自己补一句。`);
    // nothing new to send: leaving the note costs no request
    expect(await click("leave the note (nothing new)", () => c.noteLeft(pcaNow, pca))).toEqual([]);
    // a further edit: one GET and one more hand edit
    vault.files.set(pcaNow, kept.replace("我自己补一句。", "我自己补一句。再补一句。"));
    expect((await click("leave the note (a further edit)", async () => {
      await c.noteLeft(pcaNow, pca);
      await until(() => requests.filter((x) => x === edits).length === 2);
    })).slice(0, 2)).toEqual([`GET /wiki/pages/${pca}`, edits]);
    expect((sentBody(edits) as { summary: string }).summary).toContain("再补一句。");

    // ---- 20. (was 每隔几天, retired with 整理): the settings are only 每天最多 / 每页最多花 / 语言
    expect(cat().settings).toEqual({ review_per_day: 20, page_limit_usd: null, language: "zh" });

    // ---- 21. a second view (fresh controller): init reads 今天 (catalog, K, review list, recent changes); following the PCA
    // note loads its page (K is already there) and opens no note; opening z afterwards is one GET
    const c2 = makeController();
    const click2 = async (label: string, f: () => Promise<void> | void): Promise<string[]> => {
      const r0 = requests.length;
      await f();
      await until(() => c2.state.k.kind !== "loading");
      await settle();
      const made = requests.slice(r0);
      console.log(`[wiki-contract] (view 2) ${label}: ${made.join(" | ") || "(no request)"}`);
      expect(schemaErrs, label).toEqual([]);
      expect(failures, label).toEqual([]);
      expect(c2.state.busy, label).toBeNull();
      return made;
    };
    expect((await click2("init", () => c2.init())).sort()).toEqual(["GET /wiki", "GET /wiki/k", "GET /wiki/progress", "GET /wiki/review/today",
      "GET /wiki/rsi/changes", "GET /wiki/teach/spec"]);
    expect(c2.state.k.kind).toBe("ready");
    const openedBefore = opened.length;
    expect(await click2("follow pca", () => c2.followPage(pca))).toEqual([`POST /wiki/pages/${pca}/open?rewrite=1`]);
    expect(c2.state.page.kind === "open" && c2.state.page.page.page_id).toBe(pca);
    expect(c2.state.tab).toBe("today");
    expect(c2.state.k.kind).toBe("ready");
    expect(opened).toHaveLength(openedBefore); // following never opens a note
    expect(await click2("follow pca again", () => c2.followPage(pca))).toEqual([`POST /wiki/pages/${pca}/open?rewrite=1`]);
    expect(await click2("open z", () => c2.openWikiPage(z))).toEqual([`POST /wiki/pages/${z}/open?rewrite=1`]);
    expect(last(opened)).toBe(vault.claiming(z)[0]);
    c2.dispose();

    // ---- 22. 生成内容的语言 (the settings row): PUT /wiki/settings with exactly {language} (never
    // review_per_day), then the catalog again; back to 简体中文 the same way
    for (const language of ["en", "zh"] as const) {
      expect(await click(`language ${language}`, async () => { expect(await c.setLanguage(language)).toBeNull(); }))
        .toEqual(["PUT /wiki/settings", "GET /wiki", "GET /wiki/progress"]);
      expect(sentBody("PUT /wiki/settings")).toEqual({ language });
      expect(cat().settings.language).toBe(language);
    }

    // ---- 23. 知识星图: what 今天's small map and the big map read (K, the catalog, the paths the catalog lists), drawn by the
    // plugin's own model / frontier code. No request beyond those reads; every K concept is a node (the two goals' paths
    // give goal membership).
    expect(await click("star map source", async () => {
      const data = await c.starMapSource().load();
      const map = buildStarMap(data.k, data.goals);
      expect(data.k.concepts.length).toBeGreaterThanOrEqual(2);     // the flow above learned z 分数 and PCA
      expect(map.nodes.map((x) => x.id).sort()).toEqual(data.k.concepts.map((x) => x.id).sort());
      const rec = frontier(map);
      expect(rec.length).toBeLessThanOrEqual(3);
      for (const r of rec) expect(map.byId.has(r.conceptId)).toBe(true);
    })).toEqual(["GET /wiki/k", "GET /wiki", `GET /wiki/paths/${zDone.path_id}`, `GET /wiki/paths/${pDone.path_id}`]);
    // (the catalog read directly, not the 今天 reload: no progress; then the paths it lists, for goal membership)

    // Every vault write is a note under <notes folder>/Wiki, a backup made before an update, or a written copy (Q8).
    expect(vault.writes.filter((x) => !/^(create|process) (EvoWiki\/Wiki\/|\.evowiki\/backups\/)/.test(x)
      && !/^create \.evowiki\/written\/[0-9a-f]{32}\/[0-9a-f]{64}\.json$/.test(x))).toEqual([]);
    expect(vault.writes.filter((x) => x.startsWith("process ")).length)
      .toBe(vault.writes.filter((x) => x.startsWith("create .evowiki/backups/")).length);
    expect(vault.listMarkdown(FOLDER).map((p) => [vault.claiming(z).includes(p), vault.claiming(pca).includes(p)]).sort())
      .toEqual([[false, true], [true, false]]);

    // ---- 基本合适: one signal, no model call
    expect((await click("基本合适", async () => {
      c.pickEndChip(pca, "right");
      await until(() => requests.includes(`POST /wiki/pages/${pca}/fit`));
    }))).toEqual([`POST /wiki/pages/${pca}/fit`]);

    // ---- the backend served the whole script and refused nothing
    const status = JSON.parse((await nodeWikiHttp.request({ method: "GET", url: `${API!}/test/wiki-script`, headers: {} })).text) as
      { served: string[]; unscripted: string[]; pending: Record<string, number> };
    console.log(`[wiki-contract] LLM calls served: ${status.served.length}`);
    for (const x of streams) console.log(`[wiki-contract] SSE ${x}`);
    console.log(`[wiki-contract] models checked: ${[...seen].sort().join(", ")}`);
    // Every /wiki response model and SSE payload the plugin reads, except ErrorEvent (no stream fails in this script)
    // and RsiChangeOut (no change exists on this backend: the list is empty and the one undo is a 404). ShelveOut is
    // seen only when step 19's page was offered 收为参考卡 (`can_shelve`), so it is left out of the comparison.
    expect([...seen].filter((m) => m !== "ShelveOut").sort()).toEqual([
      "ActivityOut[]", "CatalogOut", "CheckAnswerOut", "DeclareOut", "DoneOut",
      "FeedbackOut", "GoalDraftOut", "GuidelineOut", "HandEditOut", "KGridOut", "OrganizeDoneEvent",
      "OutlineEvent", "PageFeedbackOut", "PageOut", "PathNextOut", "PathOut", "PlanningEvent", "ProgressOut", "ReviewReminderOut", "ReviewTodayOut", "RsiChangeOut[]", "SectionDeltaEvent",
      "SectionDoneEvent", "SectionOut", "SettingsIO", "SignalCreatedOut", "TeachSpecOut", "WriterEvent"]);
    // every model a ROUTES entry names was either driven above or is listed in NOT_DRIVEN (and nothing listed there was driven);
    // the /wiki/sources routes are driven by the sources test below
    const routeModels = [...new Set(ROUTES.filter((r) => !SOURCE_ROUTES.includes(r)).flatMap(([, , m]) => (m === null ? [] : [m])))];
    expect(routeModels.filter((m) => !seen.has(m) && !NOT_DRIVEN.includes(m) && m !== "ShelveOut").sort()).toEqual([]);
    expect(NOT_DRIVEN.filter((m) => seen.has(m))).toEqual([]);
    expect(status.unscripted).toEqual([]);
    expect(status.pending).toEqual({});
  }, 120_000);

  /**
   * 资料 (/wiki/sources), on the client only (the UI is not built yet). Independent of the scripted flow above (no model:
   * the backend imports inline with the hashing embedder), so it runs on the same backend in either order. The REAL
   * HttpWikiClient over node:http + the node SSE transport: every JSON body and every progress payload is checked against
   * source_schemas.py / sources/importer.py, and every SOURCE_ROUTES entry must be driven here (none is in NOT_DRIVEN).
   */
  it("资料: upload outline.pdf (multipart), follow its progress (GET SSE) to done, list / get / file / uses / rename / delete", async () => {
    const wire = loadWireSchema();
    const requests: string[] = [];
    const schemaErrs: string[] = [];
    const failures: string[] = [];
    const seen = new Set<string>();
    /** Indexes of the SOURCE_ROUTES entries a 2xx answered; SSE_ROUTE = the progress stream. */
    const driven = new Set<number>();
    const SSE_ROUTE = -1;
    const path = (url: string): string => url.slice(API!.length);
    const check = (where: string, model: string, value: unknown): void => {
      seen.add(model);
      for (const e of wire.validate(model, value)) schemaErrs.push(`${where}: ${e}`);
    };
    const http_: WikiHttp = {
      request: async (req) => {
        const p = path(req.url);
        requests.push(`${req.method} ${p}`);
        const res = await nodeWikiHttp.request(req);
        // this scripted backend has no Bot ledger: the re-read after a hand edit answers 503, as a service without one does
        if (res.status === 503 && p === "/wiki/bot/actions") return res;
        if (res.status < 200 || res.status >= 300) { failures.push(`${req.method} ${p} → ${res.status} ${res.text.slice(0, 300)}`); return res; }
        const route = SOURCE_ROUTES.find(([m, re]) => m === req.method && re.test(p));
        if (route) driven.add(SOURCE_ROUTES.indexOf(route));
        if (!route) schemaErrs.push(`${req.method} ${p}: not a sources route`);
        else if (route[2] === null) {
          if (res.status !== 204 || res.text !== "") schemaErrs.push(`${req.method} ${p}: expected an empty 204, got ${res.status}`);
        } else if (route[2] === BYTES) {
          if (!req.binary || res.bytes === undefined || res.bytes.byteLength === 0) schemaErrs.push(`${req.method} ${p}: expected the file bytes`);
        } else if (route[2].endsWith("[]")) {
          const list = JSON.parse(res.text) as unknown;
          if (!Array.isArray(list)) schemaErrs.push(`${req.method} ${p}: expected a JSON list`);
          else list.forEach((x: unknown, i) => check(`${req.method} ${p} [${i}]`, route[2]!.slice(0, -2), x));
        } else check(`${req.method} ${p}`, route[2], JSON.parse(res.text) as unknown);
        return res;
      },
    };
    const sse: StreamTransport = {
      stream: async (req, onChunk) => {
        const p = path(req.url);
        requests.push(`${req.method} ${p} (SSE)`);
        let raw = "";
        const res = await nodeStreamTransport.stream(req, (c) => { raw += c; onChunk(c); });
        // this scripted backend has no Bot ledger: the re-read after a hand edit answers 503, as a service without one does
        if (res.status === 503 && p === "/wiki/bot/actions") return res;
        if (res.status < 200 || res.status >= 300) { failures.push(`${req.method} ${p} → ${res.status} ${res.text.slice(0, 300)}`); return res; }
        driven.add(SSE_ROUTE);
        for (const m of new SseDecoder().push(raw + "\n\n")) {
          const model = SOURCE_MODELS[m.event];
          if (model === undefined) schemaErrs.push(`${p}: unknown event ${m.event}`);
          else check(`${p} ${m.event}`, model, m.data);
        }
        return res;
      },
    };
    const click = async (label: string, f: () => Promise<void>): Promise<string[]> => {
      const r0 = requests.length;
      await f();
      const made = requests.slice(r0);
      console.log(`[wiki-contract] ${label}: ${made.join(" | ") || "(no request)"}`);
      expect(schemaErrs, label).toEqual([]);
      expect(failures, label).toEqual([]);
      return made;
    };

    // Upload the recorded outline.pdf under a Chinese file name (multipart, the plugin's own buildMultipart), follow its progress stream (GET SSE) to done, then the
    // list, the source, its original, a use, a rename, the same file again, pasted text, a delete, and the 404s after it.
    // The backend imports inline with the hashing embedder and no model (no summaries), so every step is deterministic.
    const src = new HttpWikiClient(() => ({ baseUrl: API!, userId: USER }), http_, sse);
    const pdf = readFileSync(join(__dirname, "..", "..", "..", "tests", "fixtures", "sources", "outline.pdf"));
    const pdfBytes = pdf.buffer.slice(pdf.byteOffset, pdf.byteOffset + pdf.byteLength);
    let sid = "";
    expect(await click("sources: list (empty)", async () => { expect(await src.listSources()).toEqual([]); })).toEqual(["GET /wiki/sources"]);
    expect(await click("sources: upload outline.pdf", async () => {
      const s = await src.uploadSource({ file: { name: "线代大纲.pdf", data: pdfBytes, type: "application/pdf" },
        origin: "资料/线代大纲.pdf", use: "learn" });
      sid = s.id;
      // an upload without a title takes the PDF's own title (metadata Title, else a title-like first bookmark), not the file name
      expect(s).toMatchObject({ title: "学习方法 Learning Methods", kind: "pdf", origin: "资料/线代大纲.pdf", deleted: false, error: null });
    })).toEqual(["POST /wiki/sources"]);
    const evs: SourceEvent[] = [];
    expect(await click("sources: follow progress", () => src.followSource(sid, (e) => evs.push(e))))
      .toEqual([`GET /wiki/sources/${sid}/progress (SSE)`]);
    const end = last(evs)!;
    // progress events, one `usable` once the text can be read (before the vectors; c60e596e), then `done`
    expect(evs.map((e) => e.event).filter((e) => e !== "progress")).toEqual(["usable", "done"]);
    expect(evs.length).toBeGreaterThan(2);
    if (end.event !== "done") throw new Error(`import ended with ${JSON.stringify(end)}`);
    const usable = evs.find((e) => e.event === "usable");
    if (usable?.event !== "usable") throw new Error("no usable event");
    expect([usable.data.status, usable.data.sections]).toEqual([end.data.status, end.data.sections]);
    expect(["ready", "partial"]).toContain(end.data.status);
    expect(end.data.sections).toBeGreaterThan(0);
    expect(await click("sources: list + detail", async () => {
      const list = await src.listSources();
      expect(list.map((x) => [x.id, x.status])).toEqual([[sid, end.data.status]]);
      const d = await src.getSource(sid);
      expect(d.sections).toHaveLength(end.data.sections);
      expect(d.pages).toBe(end.data.pages);
      expect(d.uses.map((u) => u.use)).toEqual(["learn"]);
    })).toEqual(["GET /wiki/sources", `GET /wiki/sources/${sid}`]);
    expect(await click("sources: the original", async () => {
      expect(Buffer.from(await src.sourceFile(sid)).equals(pdf)).toBe(true);
    })).toEqual([`GET /wiki/sources/${sid}/file`]);
    expect(await click("sources: a use, a rename, the same file again", async () => {
      expect(await src.addSourceUse(sid, { use: "exam", goal_id: "g-exam" })).toMatchObject({ use: "exam", goal_id: "g-exam", page_id: null });
      expect((await src.renameSource(sid, "  线性代数大纲 ")).title).toBe("线性代数大纲");
      expect((await src.uploadSource({ file: { name: "copy.pdf", data: pdfBytes } })).id).toBe(sid);
    })).toEqual(["POST /wiki/sources/" + sid + "/uses", `PATCH /wiki/sources/${sid}`, "POST /wiki/sources"]);
    let tid = "";
    expect(await click("sources: pasted text", async () => {
      const t = await src.uploadSource({ text: "# 特征值\n\nA v = λ v：v 方向不变，只被拉长 λ 倍。", title: "特征值笔记" });
      tid = t.id;
      expect(t).toMatchObject({ kind: "text", title: "特征值笔记" });
      expect((await src.listSources()).map((x) => x.id).sort()).toEqual([sid, tid].sort());
    })).toEqual(["POST /wiki/sources", "GET /wiki/sources"]);
    expect(await click("sources: delete, then 404", async () => {
      await src.deleteSource(sid);
      const f0 = failures.length;
      await expect(src.getSource(sid)).rejects.toEqual(expect.objectContaining({ status: 404 }));
      await expect(src.sourceFile(sid)).rejects.toBeInstanceOf(ApiError);
      await expect(src.followSource(sid, () => { throw new Error("no event after a delete"); })).rejects.toMatchObject({ status: 404 });
      expect(failures.splice(f0).map((x) => x.slice(0, x.indexOf(" → ") + 6))).toEqual([
        `GET /wiki/sources/${sid} → 404`, `GET /wiki/sources/${sid}/file → 404`, `GET /wiki/sources/${sid}/progress → 404`]);
      expect((await src.listSources()).map((x) => x.id)).toEqual([tid]);
    })).toEqual([`DELETE /wiki/sources/${sid}`, `GET /wiki/sources/${sid}`, `GET /wiki/sources/${sid}/file`,
      `GET /wiki/sources/${sid}/progress (SSE)`, "GET /wiki/sources"]);

    // every sources route answered a 2xx here (the 7 of SOURCE_ROUTES and the progress stream), and every model was checked
    expect(SOURCE_ROUTES.map(([m, re], i) => [m, re.source, driven.has(i)]).filter(([, , d]) => !d)).toEqual([]);
    expect(driven.has(SSE_ROUTE)).toBe(true);
    expect([...seen].sort()).toEqual(["SourceDetail", "SourceDone", "SourceOut", "SourceProgress", "SourceUseOut"]);
  }, 60_000);
});

function last<T>(a: readonly T[]): T | undefined { return a[a.length - 1]; }
