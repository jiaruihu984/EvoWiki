import { type App, TFile } from "obsidian";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StreamTransport } from "../api/client";
import type { WikiActions } from "./actions";
import { HttpWikiClient, type WikiHttp, type WikiHttpRequest } from "./api";
import type { WikiController, WikiControllerDeps } from "./controller";
import { FakeConn } from "./controller-test-kit";
import { FakeWikiApi } from "./fake";
import { createDemoFixture } from "./fixtures-demo";
import type { WikiNoteInput } from "./note";
import { renderWikiNote, WIKI_END } from "./note/format";
import {
  findClaimingNote, findWikiNote, makeAssetFetcher, makeWikiController, type WikiWiringDeps, wikiActions, wikiHandEdit, wikiNoteEdited, openEvoWikiSettings, OPEN_SETTINGS_COMMAND,
} from "./wiring";
import { syncWithVault, type WikiNoteVault } from "./note/sync";

const claim = (id: string) => `intro\n<!-- EVOWIKI:WIKI:START\npage_id=${id}\nformat=evowiki-wiki-note/1\n-->\nbody\n<!-- EVOWIKI:WIKI:END -->\n`;
/** v2 claim: the frontmatter evowiki_page_id property. */
const claim2 = (id: string) => `---\nevowiki_page_id: "${id}"\n---\n%% evowiki:start %%\nbody\n%% evowiki:end %%\n`;
const tfile = (path: string) => Object.assign(new TFile(), { path });

/** A read-only fake of Obsidian's App: markdown files with contents. Any write would throw (no such method). */
function fakeApp(files: Record<string, string>): App {
  const vault = {
    getMarkdownFiles: () => Object.keys(files).map(tfile),
    getAbstractFileByPath: (p: string) => (p in files ? tfile(p) : null),
    read: (f: TFile) => Promise.resolve(files[f.path] ?? ""),
  };
  return { vault } as unknown as App;
}

const emptyCatalog = { learning: [], shelf: { pages: [] },
  settings: { review_per_day: 10, page_limit_usd: null, language: "zh" }, current_page: null, names: {}, paths: [], maps: [] };

function rig(o: { settings?: Partial<{ serverUrl: string; userId: string; notesFolder: string }>; files?: Record<string, string> } = {}) {
  const settings = { serverUrl: "http://srv:9", userId: "jerry", notesFolder: "EvoWiki", ...o.settings };
  const requests: WikiHttpRequest[] = [];
  const http: WikiHttp = { request: (r) => { requests.push(r); return Promise.resolve({ status: 200, text: JSON.stringify(emptyCatalog) }); } };
  const sse: StreamTransport = { stream: vi.fn(() => Promise.resolve()) } as unknown as StreamTransport;
  const syncWikiNote = vi.fn((_app: App, folder: string, i: WikiNoteInput, onPath?: (path: string, moved: "no" | "yes" | "failed") => void) => {
    onPath?.(`${folder}/${i.title}.md`, "no");
    return Promise.resolve("created" as const);
  });
  const opened: string[] = [];
  const d: WikiWiringDeps = {
    app: fakeApp(o.files ?? {}), settings: () => settings, conn: new FakeConn(), http, sse, syncWikiNote,
    openExisting: (p) => { opened.push(p); return true; },
  };
  return { d, settings, requests, syncWikiNote, opened };
}
/** An in-memory WikiNoteVault (notes, folders and the hidden written copies) for the note-edit readers. */
class MemWritten implements WikiNoteVault {
  readonly files = new Map<string, string>();
  readonly folders = new Set<string>();
  list(p: string) {
    const direct = (paths: Iterable<string>) => [...paths].filter((f) => f.startsWith(`${p}/`) && !f.slice(p.length + 1).includes("/"));
    return Promise.resolve({ files: direct(this.files.keys()), folders: direct(this.folders) });
  }
  removeFile(p: string) { this.files.delete(p); return Promise.resolve(); }
  removeEmptyDir(p: string) { this.folders.delete(p); return Promise.resolve(); }

  occupied(p: string) { return Promise.resolve(this.files.has(p) || this.folders.has(p)); }
  read(p: string) { return Promise.resolve(this.files.get(p)!); }
  readHidden(p: string) { return Promise.resolve(this.files.get(p) ?? null); }
  create(p: string, t: string) { if (this.files.has(p)) return Promise.reject(new Error("exists")); this.files.set(p, t); return Promise.resolve(); }
  process(p: string, fn: (d: string) => string) { const out = fn(this.files.get(p)!); this.files.set(p, out); return Promise.resolve(out); }
  rename(from: string, to: string) { const t = this.files.get(from); if (t === undefined || this.files.has(to)) return Promise.reject(new Error("cannot rename")); this.files.delete(from); this.files.set(to, t); return Promise.resolve(); }
  trash(p: string) { this.files.delete(p); return Promise.resolve(); }
  ensureFolder(p: string) { this.folders.add(p); return Promise.resolve(); }
  listMarkdown(folder: string) { return [...this.files.keys()].filter((p) => p.startsWith(`${folder}/`) && p.endsWith(".md")).sort(); }
}
const depsOf = (c: WikiController) => (c as unknown as { deps: WikiControllerDeps }).deps;
const INPUT: WikiNoteInput = { pageId: "p1", title: "T", goal: "g", diffText: null, collapsed: false, sections: [] };

describe("the note's tree row", () => {
  afterEach(() => { vi.useRealTimers(); });

  it("a streamed sync carries writing: true and marks the file's tree row; the full sync clears it", async () => {
    document.body.replaceChildren();
    const row = document.createElement("div");
    row.className = "nav-file-title";
    row.dataset.path = "EvoWiki/T.md";
    document.body.append(row);
    const r = rig({ files: { "EvoWiki/T.md": "---\nevowiki_page_id: p1\n---\n" } });
    const c = makeWikiController(r.d, false);
    const sync = depsOf(c).sync!;
    const run = (i: WikiNoteInput, o?: { partial?: boolean }) => sync.run(i, o);
    await run(INPUT, { partial: true });
    expect(r.syncWikiNote.mock.calls[0]![2].writing).toBe(true);
    expect(row.querySelector(".ew-writing-mark")?.textContent).toBe("生成中");
    await run(INPUT);
    expect(r.syncWikiNote.mock.calls[1]![2].writing).toBeUndefined();
    expect(row.querySelector(".ew-writing-mark")).toBeNull();
    document.body.replaceChildren();
  });

});

describe("makeAssetFetcher", () => {
  it("writes under <resolved account root>/assets (read per call); the real controller gets it, the demo never does", () => {
    const r = rig();
    const fetcher = makeAssetFetcher(r.d, () => undefined);
    expect(fetcher.pathFor("a1")).toBe("EvoWiki/assets/a1.html");
    r.settings.notesFolder = "Notes";
    expect(fetcher.pathFor("a1")).toBe("Notes/assets/a1.html");
    expect(fetcher.pathFor("../x")).toBeNull();
    expect(depsOf(makeWikiController({ ...r.d, assets: fetcher }, false)).assets).toBe(fetcher);
    expect(depsOf(makeWikiController({ ...r.d, assets: fetcher }, true)).assets).toBeUndefined();
    expect(depsOf(makeWikiController(r.d, false)).assets).toBeUndefined();
  });

  it("the request goes to the configured server with the user id; nothing is written when the meta request fails", async () => {
    const r = rig();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const create = vi.fn();
    Object.assign(r.d.app.vault, { getAllLoadedFiles: () => [], adapter: { exists: () => Promise.resolve(false) }, create });
    expect(await makeAssetFetcher(r.d, () => undefined).retrieve("a1")).toBe("failed"); // the catalog JSON is not meta
    expect(r.requests[0]?.url).toBe("http://srv:9/wiki/assets/a1/meta");
    expect(r.requests[0]?.headers?.["X-EvoWiki-User"]).toBe("jerry");
    expect(create).not.toHaveBeenCalled();
    warn.mockRestore();
  });
});

describe("makeWikiController", () => {
  it("demo: the fake client, no note sync, always configured, demo flag set", () => {
    const r = rig({ settings: { userId: "", serverUrl: "" } });
    const c = makeWikiController(r.d, true);
    const deps = depsOf(c);
    expect(deps.api).toBeInstanceOf(FakeWikiApi);
    expect(deps.sync).toBeNull();
    expect(deps.configured()).toBe(true);
    expect(deps.demo).toBe(true);
    expect(c.state.demo).toBe(true);
  });

  it("demo: never offline because of the real server's connection state (it does not talk to the server)", async () => {
    const r = rig();
    (r.d.conn as FakeConn).emit("offline");
    const c = makeWikiController(r.d, true);
    expect(c.state.conn).not.toBe("offline");
    await c.init();
    expect(r.requests).toEqual([]);
    expect(c.state.catalog.kind).toBe("ready");
  });

  it("real: the HTTP client; the first request uses settings().serverUrl and carries the user id header", async () => {
    const r = rig();
    const c = makeWikiController(r.d, false);
    expect(depsOf(c).api).toBeInstanceOf(HttpWikiClient);
    expect(depsOf(c).demo).toBe(false);
    expect(depsOf(c).conn).toBe(r.d.conn);
    await c.init();
    expect(r.requests[0]!.url).toBe("http://srv:9/wiki");
    expect(r.requests[0]!.headers["X-EvoWiki-User"]).toBe("jerry");
    // settings are read per call: a changed identity is used by the next request
    r.settings.serverUrl = "http://other:1"; r.settings.userId = "amy";
    await c.retryConnection(); c.setTab("k");
    await vi.waitFor(() => expect(r.requests.length).toBeGreaterThan(1));
    expect(r.requests[r.requests.length - 1]!.url.startsWith("http://other:1/wiki")).toBe(true);
    expect(r.requests[r.requests.length - 1]!.headers["X-EvoWiki-User"]).toBe("amy");
  });

  it("demo fixture: the real controller (not the demo) on the fixture's client, always online and configured, no HTTP", async () => {
    const r = rig({ settings: { serverUrl: "", userId: "" } });
    (r.d.conn as FakeConn).emit("offline");
    const fx = createDemoFixture();
    const c = makeWikiController({ ...r.d, fixture: { api: fx.wiki, recommendations: fx.recommendations } }, false);
    const deps = depsOf(c);
    expect(deps.api).toBe(fx.wiki);
    expect(deps.demo).toBe(false);
    expect(c.state.demo).toBe(false);
    expect(deps.sync).not.toBeNull();
    expect(deps.configured()).toBe(true);
    expect(c.state.conn).not.toBe("offline");
    expect(c.state.recommendations).toBe(fx.recommendations);
    await c.init();
    expect(r.requests).toEqual([]);
    expect(c.state.review.today.kind).toBe("ready");
    // The demo mode ignores the fixture: it stays the plain fake.
    expect(depsOf(makeWikiController({ ...r.d, fixture: { api: fx.wiki, recommendations: [] } }, true)).api).toBeInstanceOf(FakeWikiApi);
  });

  it("without the fixture nothing changes: the HTTP client, the real connection, no recommendation override", () => {
    const r = rig();
    const c = makeWikiController(r.d, false);
    expect(depsOf(c).api).toBeInstanceOf(HttpWikiClient);
    expect(depsOf(c).conn).toBe(r.d.conn);
    expect(depsOf(c).recommendations).toBeUndefined();
    expect(c.state.recommendations).toBeNull();
  });

  it("demo fixture: the asset fetcher reads from the fixture's client", async () => {
    const r = rig();
    const fx = createDemoFixture();
    const create = vi.fn(() => Promise.resolve());
    Object.assign(r.d.app.vault, { getAllLoadedFiles: () => [], getAbstractFileByPath: () => null, adapter: { exists: () => Promise.resolve(false) },
      create, createFolder: () => Promise.resolve() });
    const out = await makeAssetFetcher({ ...r.d, fixtureApi: fx.wiki }, () => undefined).retrieve("demo-ix");
    expect(r.requests).toEqual([]);
    expect(fx.wiki.calls.map((x) => x.method)).toEqual(["assetMeta", "assetHtml"]);
    expect(out).toBe("written");
  });

  it("real: configured() needs both a server URL and a user id", () => {
    expect(depsOf(makeWikiController(rig().d, false)).configured()).toBe(true);
    expect(depsOf(makeWikiController(rig({ settings: { userId: "" } }).d, false)).configured()).toBe(false);
    expect(depsOf(makeWikiController(rig({ settings: { serverUrl: "" } }).d, false)).configured()).toBe(false);
    const r = rig({ settings: { userId: "" } });
    const c = makeWikiController(r.d, false);
    r.settings.userId = "late";
    expect(depsOf(c).configured()).toBe(true); // read per call
  });

  it("real: sync.run calls the injected syncWikiNote with the app and folder = <resolved account root>", async () => {
    const r = rig({ settings: { notesFolder: "Notes/Evo" } });
    const sync = depsOf(makeWikiController(r.d, false)).sync!;
    await expect(sync.run(INPUT)).resolves.toBe("created");
    expect(r.syncWikiNote).toHaveBeenCalledWith(r.d.app, "Notes/Evo", INPUT, expect.any(Function), undefined, undefined);
    r.settings.notesFolder = "Later";
    r.settings.userId = "amy";
    await sync.run(INPUT);
    expect(r.syncWikiNote).toHaveBeenLastCalledWith(r.d.app, "Later", INPUT, expect.any(Function), undefined, undefined);
  });

  it("real: no note is read or written before the vault is indexed (folderReady)", async () => {
    const r = rig();
    let ready!: () => void;
    const sync = depsOf(makeWikiController({ ...r.d, folderReady: () => new Promise<void>((res) => { ready = res; }) }, false)).sync!;
    const run = sync.run(INPUT);
    await new Promise((res) => setTimeout(res, 0));
    expect(r.syncWikiNote).not.toHaveBeenCalled();
    ready();
    await run;
    expect(r.syncWikiNote).toHaveBeenCalledWith(r.d.app, "EvoWiki", INPUT, expect.any(Function), undefined, undefined);
  });

  it("real: sync.find looks in <resolved account root> and returns the one note claiming the page", async () => {
    const r = rig({ files: {
      "EvoWiki/A.md": claim("p1"), "EvoWiki/B.md": claim("p2"), "EvoWiki (bob)/Other.md": claim("p3"),
      "EvoWiki/C.md": "page_id=p4\n", "EvoWiki/D.md": claim("p4x"),
    } });
    const sync = depsOf(makeWikiController(r.d, false)).sync!;
    await expect(sync.find("p1")).resolves.toBe("EvoWiki/A.md");
    await expect(sync.find("p2")).resolves.toBe("EvoWiki/B.md");
    await expect(sync.find("p3")).resolves.toBeNull(); // outside the wiki folder
    await expect(sync.find("p4")).resolves.toBeNull(); // a page_id line that is not a claim
    expect(r.syncWikiNote).not.toHaveBeenCalled();
  });

  it("real: sync.handEdit reads the one note claiming the page in <resolved account root>", async () => {
    const input: WikiNoteInput = { ...INPUT, sections: [{ id: "s1", heading: "公式", role: "explain", cue: "", body: "原文。", folded: false }] };
    const note = renderWikiNote(input).markdown.replace(`原文。\n${WIKI_END}`, `我改的。\n${WIKI_END}`);
    const r = rig({ files: { "EvoWiki/T.md": note, "EvoWiki (bob)/T.md": note.replace("我改的", "别处") } });
    const sync = depsOf(makeWikiController(r.d, false)).sync!;
    const got = await sync.handEdit(input);
    expect(got).toMatchObject({ path: "EvoWiki/T.md", text: note, plan: { kind: "ok" } });
    await expect(sync.handEdit({ ...input, pageId: "zz" })).resolves.toBeNull(); // no note claims it
    await expect(sync.edited("EvoWiki/T.md", input.pageId)).resolves.toBe(true);
    await expect(sync.edited("EvoWiki (bob)/T.md", input.pageId)).resolves.toBe(false);
    expect(r.syncWikiNote).not.toHaveBeenCalled();
  });

  it("teachPort says whether it is the demo (the settings and the file menu then write nothing, finding 5)", () => {
    expect(makeWikiController(rig().d, true).teachPort().demo).toBe(true);
    expect(makeWikiController(rig().d, false).teachPort().demo).toBe(false);
  });

  it("real: openNote is the injected openExisting", () => {
    const r = rig();
    expect(depsOf(makeWikiController(r.d, false)).openNote("X.md")).toBe(true);
    expect(r.opened).toEqual(["X.md"]);
  });
});

describe("findWikiNote / findClaimingNote", () => {
  it("exactly one claimant → its path; none or two → null", async () => {
    const files: Record<string, string> = { "F/a.md": claim("p1"), "F/b.md": claim("p2"), "F/c.md": claim2("p2"), "F/d.md": "plain",
      "F/e.md": claim2("p5"), "F/f.md": "evowiki_page_id: p6\n" };
    const store = { listMarkdown: vi.fn((folder: string) => Object.keys(files).filter((p) => p.startsWith(folder + "/"))),
      read: vi.fn((p: string) => Promise.resolve(files[p]!)) };
    await expect(findClaimingNote(store, "F", "p1")).resolves.toBe("F/a.md");
    await expect(findClaimingNote(store, "F", "p2")).resolves.toBeNull();
    await expect(findClaimingNote(store, "F", "zz")).resolves.toBeNull();
    await expect(findClaimingNote(store, "F", "p5")).resolves.toBe("F/e.md"); // v2 claim
    await expect(findClaimingNote(store, "F", "p6")).resolves.toBeNull(); // not in frontmatter
    expect(store.listMarkdown).toHaveBeenCalledWith("F");
  });

  it("an invalid folder → null without listing or reading anything", async () => {
    const store = { listMarkdown: vi.fn(() => ["x.md"]), read: vi.fn(() => Promise.resolve(claim("p1"))) };
    for (const f of ["/abs/Wiki", "C:/x/Wiki", "../up/Wiki", ".obsidian/Wiki", "a\\b/Wiki"]) {
      await expect(findClaimingNote(store, f, "p1")).resolves.toBeNull();
    }
    expect(store.listMarkdown).not.toHaveBeenCalled();
    expect(store.read).not.toHaveBeenCalled();
  });

  it("the folder is normalized before listing (trailing slash, empty segments)", async () => {
    const store = { listMarkdown: vi.fn(() => ["A/Wiki/n.md"]), read: vi.fn(() => Promise.resolve(claim("p1"))) };
    await expect(findClaimingNote(store, "A//Wiki/", "p1")).resolves.toBe("A/Wiki/n.md");
    expect(store.listMarkdown).toHaveBeenCalledWith("A/Wiki");
  });

  it("wikiHandEdit reads each note once, and the claiming note's text is the one read back", async () => {
    const md = renderWikiNote(INPUT).markdown;
    const files: Record<string, string> = { "F/a.md": md, "F/b.md": "# other" };
    const read = vi.fn((p: string) => Promise.resolve(files[p]!));
    const hit = await wikiHandEdit({ listMarkdown: () => Object.keys(files), read, readHidden: () => Promise.resolve(null) }, "F", INPUT);
    expect(read.mock.calls.map((c) => c[0]).sort()).toEqual(["F/a.md", "F/b.md"]);
    expect(hit).toMatchObject({ path: "F/a.md", text: md, plan: { kind: "none" } });
    files["F/b.md"] = md;
    expect(await wikiHandEdit({ listMarkdown: () => Object.keys(files), read, readHidden: () => Promise.resolve(null) }, "F", INPUT)).toBeNull();
  });

  it("wikiNoteEdited: only the one note claiming the page at that path, and only when its block changed", async () => {
    const input: WikiNoteInput = { ...INPUT, sections: [{ id: "s1", heading: "公式", role: "explain", cue: "", body: "原文。", folded: false }] };
    const v = new MemWritten();
    await syncWithVault({ store: v, now: () => new Date(0), rand: () => "r" }, "F", input);
    const path = v.listMarkdown("F")[0]!;
    expect(await wikiNoteEdited(v, "F", path, input.pageId)).toBe(false);
    v.files.set(path, v.files.get(path)!.replace("原文。", "我改的。"));
    expect(await wikiNoteEdited(v, "F", path, input.pageId)).toBe(true);
    expect(await wikiNoteEdited(v, "F", "F/elsewhere.md", input.pageId)).toBe(false);
    expect(await wikiNoteEdited(v, "F", path, "p2")).toBe(false);
  });

  it("findWikiNote reads through the app's vault (read-only)", async () => {
    const app = fakeApp({ "W/Wiki/x.md": claim("p9"), "W/y.md": claim("p9") });
    await expect(findWikiNote(app, "W/Wiki", "p9")).resolves.toBe("W/Wiki/x.md");
    await expect(findWikiNote(app, "W", "p9")).resolves.toBeNull(); // both claim it
  });
});

describe("openEvoWikiSettings (个性化 · 去设置里改)", () => {
  it("runs the evowiki:open-settings command when it is registered", () => {
    const executeCommandById = vi.fn(() => true);
    const setting = { open: vi.fn(), openTabById: vi.fn() };
    openEvoWikiSettings({ commands: { commands: { [OPEN_SETTINGS_COMMAND]: {} }, executeCommandById }, setting } as unknown as App);
    expect(executeCommandById).toHaveBeenCalledWith("evowiki:open-settings");
    expect(setting.open).not.toHaveBeenCalled();
  });

  it("otherwise opens Obsidian's settings on the EvoWiki tab", () => {
    const executeCommandById = vi.fn(() => true);
    const setting = { open: vi.fn(), openTabById: vi.fn() };
    openEvoWikiSettings({ commands: { commands: {}, executeCommandById }, setting } as unknown as App);
    expect(executeCommandById).not.toHaveBeenCalled();
    expect(setting.open).toHaveBeenCalledOnce();
    expect(setting.openTabById).toHaveBeenCalledWith("evowiki");
    expect(() => openEvoWikiSettings({} as unknown as App)).not.toThrow();
  });
});

describe("wikiActions", () => {
  const ARGS: Record<keyof WikiActions, unknown[]> = {
    followup: [{ kind: "check", pageId: "p", pageTitle: "t", sid: "s", idk: false }, "slip"],
    answerConfirm: ["fu1", 2], reteach: ["fu1"], confirmUnclear: ["fu1"],
    setTab: ["k"], retryConnection: [], continueLearning: [], answerReviewFirst: [{ node_id: "n", name: "x", page_id: null, page_title: "" }, "skip"], setLearnTopic: ["t"], openLearnNew: ["y"], openWikiPage: ["p1"], openRevise: ["p1"], goToChange: ["p1", "s2"], openPath: ["path1"], pathNext: [], pathDone: [2], revealCue: ["c|a"], gradeReview: ["c|a", "fuzzy"],
    answerReview: ["c|a", 1], nextReview: ["c|a"], startReview: [], pauseReview: [], markBar: [2], refreshToday: [], askAboutCard: ["c|a"], setReviewPerDay: [5], reloadReview: [], reloadCatalog: [], organize: [], stop: [], openCurrentNote: [], know: ["s1"],
    toggleFold: ["s1"], togglePin: ["s1"], answerCheck: ["s1", 0], done: [], expand: [],
    retry: [{ kind: "organize" }], jumpToSection: ["s1"], setRsiSwitch: [true], reloadRsi: [], undoPatch: ["pb3"], approvePatch: ["forgep:c1"], discardPatch: ["forgep:c1"],selectCell: ["c|a"], declareCell: ["c|a", "know"],
    sourcesAdd: [[]], loadEngine: [], openSettings: [], openPrompt: ["rules"], openProfile: [], moreReview: [],
    botStart: ["review"], botNotInterested: ["sg1"], botGoConfirm: ["sg1"], botDecide: ["act1", "approve"], botToggleWhy: ["s:sg1"],
    botSaveSettings: [{ today_cap: 3 }], botPause: ["tomorrow"], botChip: ["sg_chat"], closeCelebrate: [],
    sourceStartRename: ["s1"], sourceSetRenameDraft: ["x"], sourceCommitRename: [],
    sourceCancelRename: [], sourceAskDelete: ["s1"], sourceCancelDelete: [], sourceConfirmDelete: ["s1"], sourceOpen: ["s1"], sourceRetryFollow: ["s1"], sourceRetryImport: ["s1"], sourcesReload: [],
    personalView: [{ log: true }], prefDraft: ["p:q1", "x"], prefAdopt: ["q1", "x"], prefReject: ["q1"], prefSave: ["a1", "y"], prefRemove: ["a1"], prefRestore: ["z"], resolveConflicts: [], reloadConflicts: [],
  };

  it("every action calls the controller method of the same name with the same arguments, on the current controller", () => {
    const mk = () => Object.fromEntries(Object.keys(ARGS).map((k) => [k, vi.fn(() => Promise.resolve())])) as unknown as
      Record<keyof WikiActions, ReturnType<typeof vi.fn>>;
    let ctrl = mk();
    const a = wikiActions(() => ctrl as unknown as WikiController);
    const first = ctrl;
    ctrl = mk(); // the controller is read at call time (demo/real switch replaces it)
    for (const [k, args] of Object.entries(ARGS) as [keyof WikiActions, unknown[]][]) {
      (a[k] as (...x: unknown[]) => void)(...args);
      expect(ctrl[k], k).toHaveBeenCalledWith(...args);
      expect(first[k], k).not.toHaveBeenCalled();
    }
    expect(Object.keys(a).sort()).toEqual(Object.keys(ARGS).sort());
  });

  it("a self-grade moves on to the next card once it is recorded", async () => {
    const ctrl = { gradeReview: vi.fn(() => Promise.resolve()), nextReview: vi.fn() };
    wikiActions(() => ctrl as unknown as WikiController).gradeReview("c|a", "fuzzy");
    expect(ctrl.nextReview).not.toHaveBeenCalled();
    await Promise.resolve();
    expect(ctrl.nextReview).toHaveBeenCalledWith("c|a");
  });

  it("retryConnection with reconnect: conn.retryNow runs first, then the controller retry, even with nothing in error", async () => {
    const order: string[] = [];
    const retryNow = vi.fn(() => { order.push("retryNow"); return Promise.resolve(); });
    const r = rig();
    const c = makeWikiController(r.d, false);
    await c.init(); // catalog ready: nothing is in error
    expect(c.state.catalog.kind).toBe("ready");
    const spy = vi.spyOn(c, "retryConnection").mockImplementation(() => { order.push("controller"); return Promise.resolve(); });
    wikiActions(() => c, retryNow).retryConnection();
    expect(retryNow).toHaveBeenCalledOnce();
    await vi.waitFor(() => expect(spy).toHaveBeenCalledOnce());
    expect(order).toEqual(["retryNow", "controller"]);
  });

  it("retryConnection: a rejected reconnect still re-runs the controller retry; without reconnect it calls the controller directly", async () => {
    const ctrl = { retryConnection: vi.fn(() => Promise.resolve()) };
    wikiActions(() => ctrl as unknown as WikiController, () => Promise.reject(new Error("x"))).retryConnection();
    await vi.waitFor(() => expect(ctrl.retryConnection).toHaveBeenCalledOnce());
    wikiActions(() => ctrl as unknown as WikiController).retryConnection();
    expect(ctrl.retryConnection).toHaveBeenCalledTimes(2);
  });

  it("actions return nothing (fire and forget)", () => {
    const ctrl = { openWikiPage: vi.fn(() => Promise.resolve()) };
    expect(wikiActions(() => ctrl as unknown as WikiController).openWikiPage("p")).toBeUndefined();
  });
});
