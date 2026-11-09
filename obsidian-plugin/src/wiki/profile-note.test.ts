import { expect, it } from "vitest";
import { ProfileClient, type WikiHttp, type WikiHttpRequest } from "./api";
import { PROFILE_SAVE_MS, ProfileNote, profileNoteName, type ProfileVault } from "./profile-note";

const PATH = "EvoWiki/我的背景.md";
it("new background file names follow the initial language", () => {
  expect(profileNoteName("zh")).toBe("我的背景.md"); expect(profileNoteName("en")).toBe("My background.md");
});

/** The server: GET answers its note; PUT merges (his text plus a server line) and keeps the result. */
function server(note: string): WikiHttp & { calls: WikiHttpRequest[] } {
  const calls: WikiHttpRequest[] = [];
  return {
    calls,
    request: (r) => {
      calls.push(r);
      if (r.method === "PUT") note = `${(JSON.parse(r.body as string) as { text: string }).text}- 服务器补的一行\n`;
      return Promise.resolve({ status: 200, text: JSON.stringify({ text: note, version: `v${note.length}`, kept: [] }) });
    },
  };
}

function vault(initial: string | null): ProfileVault & { text: string | null; typed?: string } {
  const v: ProfileVault & { text: string | null; typed?: string } = {
    text: initial,
    read: () => Promise.resolve(v.text),
    create: (_p, text) => { v.text = text; return Promise.resolve(); },
    backup: () => Promise.resolve(),
    replace: (_p, expected, text) => {
      if (v.typed !== undefined) v.text = v.typed;             // he typed while the request was out
      if (v.text !== expected) return Promise.resolve(false);
      v.text = text;
      return Promise.resolve(true);
    },
  };
  return v;
}

function timers() {
  const due: (() => void)[] = [];
  return { due, set: (fn: () => void, _ms: number) => due.push(fn), clear: (_id: number) => undefined };
}

const client = (h: WikiHttp) => new ProfileClient(() => ({ baseUrl: "http://h:1", userId: "jerry" }), h);

it("writes the server's note when there is none, then only takes news without sending it back", async () => {
  const h = server("## 我是谁\n");
  const v = vault(null);
  const p = new ProfileNote(client(h), v, () => PATH, timers());
  await p.sync();
  expect(v.text).toBe("## 我是谁\n");
  await p.sync();                                            // its own write is not his edit: GET again, no PUT
  expect(h.calls.map((c) => c.method)).toEqual(["GET", "GET"]);
});

it("sends his saved note once typing pauses and writes the merged text back", async () => {
  const h = server("## 我是谁\n");
  const v = vault(null);
  const t = timers();
  const p = new ProfileNote(client(h), v, () => PATH, t);
  await p.sync();
  v.text = "## 我是谁\n- 零基础\n";
  p.edited("其他/笔记.md");
  p.edited(PATH);
  expect(t.due).toHaveLength(1);
  expect(PROFILE_SAVE_MS).toBeGreaterThan(0);
  t.due[0]!();
  await p.sync();
  const put = h.calls.find((c) => c.method === "PUT")!;
  expect(JSON.parse(put.body as string)).toEqual({ text: "## 我是谁\n- 零基础\n", base: "v7" });
  expect(v.text).toBe("## 我是谁\n- 零基础\n- 服务器补的一行\n");
});

it("never overwrites what he typed while the merge was on its way", async () => {
  const h = server("");
  const v = vault("## 我是谁\n- 零基础\n");
  v.typed = "## 我是谁\n- 零基础，学过一点 Python\n";
  await new ProfileNote(client(h), v, () => PATH, timers()).sync();
  expect(v.text).toBe("## 我是谁\n- 零基础，学过一点 Python\n");
});

const BARE = "## 我是谁\n\n## 想学什么\n";
const NOTE = "## 我是谁\n- 护士\n\n## 想学什么\n";

it("an empty server never replaces a note with lines: the note is sent with its version", async () => {
  let server_ = NOTE;
  const calls: WikiHttpRequest[] = [];
  const h: WikiHttp = { request: (r) => {
    calls.push(r);
    if (r.method === "PUT") server_ = (JSON.parse(r.body as string) as { text: string }).text;
    return Promise.resolve({ status: 200, text: JSON.stringify({ text: server_, version: `v${server_.length}`, kept: [] }) });
  } };
  const v = vault(null);
  const p = new ProfileNote(client(h), v, () => PATH, timers());
  await p.sync();
  expect(v.text).toBe(NOTE);
  server_ = BARE;                                            // something emptied the server's copy
  await p.sync();
  expect(v.text).toBe(NOTE);
  expect(calls.map((c) => c.method)).toEqual(["GET", "GET", "PUT"]);
  expect(JSON.parse(calls[2]!.body as string)).toEqual({ text: NOTE, base: `v${NOTE.length}` });
  expect(server_).toBe(NOTE);
});

it("a bare note of his own still takes the server's bare copy", async () => {
  let server_ = BARE;
  const h: WikiHttp = { request: () => Promise.resolve({ status: 200, text: JSON.stringify({ text: server_, version: "v", kept: [] }) }) };
  const v = vault(null);
  const p = new ProfileNote(client(h), v, () => PATH, timers());
  await p.sync();
  server_ = "## 我是谁\n\n## 想学什么\n\n";
  await p.sync();
  expect(v.text).toBe(server_);
});

it("the version survives a restart and what the server kept is told", async () => {
  const saved = new Map<string, string>([[PATH, "v-old"]]);
  const bases = { get: (k: string) => saved.get(k) ?? null, set: (k: string, x: string) => { saved.set(k, x); return Promise.resolve(); } };
  const calls: WikiHttpRequest[] = [];
  const h: WikiHttp = { request: (r) => { calls.push(r); return Promise.resolve({ status: 200, text: JSON.stringify({ text: NOTE, version: "v-new", kept: ["护士"] }) }); } };
  const v = vault(BARE);
  const told: number[] = [];
  await new ProfileNote(client(h), v, () => PATH, timers(), bases, (n) => told.push(n)).sync();
  expect(JSON.parse(calls[0]!.body as string)).toEqual({ text: BARE, base: "v-old" });
  expect(told).toEqual([1]);
  expect(v.text).toBe(NOTE);
  expect(saved.get(PATH)).toBe("v-new");
});

it("keeps his note before the server's merge rewrites it, and does not rewrite it when the copy cannot be kept", async () => {
  const h = server("## 我是谁\n");
  const kept: string[] = [];
  let failing = false;
  const v = vault(null);
  v.backup = (_p, text) => { if (failing) return Promise.reject(new Error("disk full")); kept.push(text); return Promise.resolve(); };
  const p = new ProfileNote(client(h), v, () => PATH, timers());
  await p.sync();
  v.text = "## 我是谁\n- 零基础\n";
  failing = true;
  await p.sync();                                            // the copy fails: the note stays as he typed it
  expect(v.text).toBe("## 我是谁\n- 零基础\n");
  failing = false;
  await p.sync();
  expect(kept).toEqual(["## 我是谁\n- 零基础\n"]);
  expect(v.text).toContain("服务器补的一行");
  await p.sync();                                            // only the server's news now: nothing of his to keep
  expect(kept).toHaveLength(1);
});

it("a deleted line of 已经会的 K took back: told once, with an undo that calls the ledger and syncs", async () => {
  const calls: WikiHttpRequest[] = [];
  const http: WikiHttp = { request: (r) => {
    calls.push(r);
    const took = r.method === "PUT" ? { lines: ["z 分数：会怎么算"], action_id: "act_1" } : null;
    return Promise.resolve({ status: 200, text: JSON.stringify({ text: "## 已经会的\n", version: "v2", kept: [], retracted: took }) });
  } };
  const told: { lines: string[]; undo: (() => Promise<void>) | null }[] = [];
  const v = vault("## 已经会的\n");
  const note = new ProfileNote(new ProfileClient(() => ({ baseUrl: "http://h", userId: "u" }), http), v, () => PATH, undefined, undefined, undefined,
    (lines, undo) => { told.push({ lines, undo }); });
  await note.sync();
  expect(told.map((t) => t.lines)).toEqual([["z 分数：会怎么算"]]);
  await told[0]!.undo!();
  expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/wiki/bot/actions/act_1/undo"))).toBe(true);
});

it("changing display language fetches a rendering without submitting a user edit", async () => {
  const h = server("## My background\n- Original user content\n");
  const v = vault(null); const note = new ProfileNote(client(h), v, () => PATH, timers());
  await note.sync(); await note.refreshLanguage();
  expect(h.calls.map((call) => call.method)).toEqual(["GET", "GET"]);
  v.text = "## My background\n- Unsaved user edit\n";
  await note.refreshLanguage();
  expect(h.calls.map((call) => call.method)).toEqual(["GET", "GET"]);
  expect(v.text).toContain("Unsaved user edit");
});
