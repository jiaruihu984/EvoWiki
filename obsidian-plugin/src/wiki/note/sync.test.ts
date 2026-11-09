import { describe, expect, it, vi } from "vitest";
import { TFile } from "obsidian";
import { createHash } from "node:crypto";
import { MINE_HINT, normalizeBody, parseWikiFile, renderWikiBody, renderWikiNote, TRASHED_LINE, WIKI_END, WIKI_START } from "./format";
import { writtenFrom } from "./edit-diff";
import { obsidianWikiNoteVault, serialized, syncWithVault, type WikiNoteVault } from "./sync";
import { handEditFor, loadWritten, writtenPath } from "./written";
import type { WikiNoteInput, WikiNoteSection } from "./types";

const sec = (id: string, role: WikiNoteSection["role"], over: Partial<WikiNoteSection> = {}): WikiNoteSection =>
  ({ id, heading: `标题${id}`, role, cue: `提示${id}`, body: `正文${id}`, folded: false, ...over });
const page = (over: Partial<WikiNoteInput> = {}): WikiNoteInput => ({
  pageId: "p_zscore", title: "z-score 标准化", goal: "看懂 z-score", diffText: null,
  collapsed: false, sections: [sec("o", "organizer"), sec("e", "explain")], ...over,
});

class FakeVault implements WikiNoteVault {
  files = new Map<string, string>();
  folders = new Set<string>();
  list(p: string) {
    const direct = (paths: Iterable<string>) => [...paths].filter((f) => f.startsWith(`${p}/`) && !f.slice(p.length + 1).includes("/"));
    return Promise.resolve({ files: direct(this.files.keys()), folders: direct(this.folders) });
  }
  removeFile(p: string) { this.files.delete(p); return Promise.resolve(); }
  removeEmptyDir(p: string) { this.folders.delete(p); return Promise.resolve(); }
  writes: string[] = [];
  failCreate: RegExp | null = null;
  /** Simulates an edit that lands between the backup and process. */
  beforeProcess: ((path: string) => void) | null = null;
  occupied(path: string) {
    const l = path.toLowerCase();
    return Promise.resolve([...this.files.keys(), ...this.folders].some((p) => p.toLowerCase() === l));
  }
  read(path: string) {
    const t = this.files.get(path);
    return t === undefined ? Promise.reject(new Error(`missing ${path}`)) : Promise.resolve(t);
  }
  readHidden(path: string) { return Promise.resolve(this.files.get(path) ?? null); }
  create(path: string, text: string) {
    this.writes.push(`create ${path}`);
    if (this.failCreate?.test(path)) return Promise.reject(new Error("create failed"));
    if (this.files.has(path)) return Promise.reject(new Error("exists"));
    this.files.set(path, text);
    return Promise.resolve();
  }
  process(path: string, fn: (d: string) => string) {
    this.writes.push(`process ${path}`);
    this.beforeProcess?.(path);
    const t = this.files.get(path);
    if (t === undefined) return Promise.reject(new Error(`missing ${path}`));
    const out = fn(t);
    this.files.set(path, out);
    return Promise.resolve(out);
  }
  rename(from: string, to: string) { const t = this.files.get(from); if (t === undefined || this.files.has(to)) return Promise.reject(new Error("cannot rename")); this.files.delete(from); this.files.set(to, t); return Promise.resolve(); }
  trash(p: string) { this.files.delete(p); return Promise.resolve(); }
  ensureFolder(path: string) {
    const parts = path.split("/");
    for (let i = 1; i <= parts.length; i++) {
      const seg = parts.slice(0, i).join("/");
      if (!this.folders.has(seg)) { this.folders.add(seg); this.writes.push(`folder ${seg}`); }
    }
    return Promise.resolve();
  }
  listMarkdown(folder: string) {
    const pre = folder.toLowerCase() + "/";
    return [...this.files.keys()].filter((p) => p.toLowerCase().startsWith(pre) && p.endsWith(".md")).sort();
  }
}

const NOW = new Date("2026-10-08T08:00:00.123Z");
const deps = (v: FakeVault) => ({ store: v, now: () => NOW, rand: () => "abcd1234" });
/** A page whose body differs from page() (the goal alone is a property and does not change the hash, R1.2). */
const changed = (over: Partial<WikiNoteInput> = {}) => page({ sections: [sec("o", "organizer"), sec("e", "explain", { body: "新正文" })], ...over });
/** A v1 note exactly as the E3a writer produced it. */
const v1Note = (body = "# z-score 标准化\n\n*目标：看懂 z-score*\n\n正文o\n\n## 标题e\n\n正文e\n\n*EvoWiki 生成的内容。*\n") => {
  const h = createHash("sha256").update(JSON.stringify(["p_zscore", "evowiki-wiki-note/1", normalizeBody(body)]), "utf8").digest("hex");
  return `<!-- EVOWIKI:WIKI:START\npage_id=p_zscore\nformat=evowiki-wiki-note/1\ngenerated_hash=sha256:${h}\n-->\n${body}<!-- EVOWIKI:WIKI:END -->\n\n## 我的笔记\n\n`;
};
const BAK = ".evowiki/backups/20261008T080000123Z-abcd1234";
/** The written copy (Q8) of `input`: its folder and file under .evowiki/written/. */
const kept = (input: WikiNoteInput) => writtenPath(input.pageId, writtenFrom(input).hash)!;

describe("syncWithVault", () => {
  it("rejects an old run at the actual file update callback after its queued sync started", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "Wiki", page());
    const path = v.listMarkdown("Wiki")[0]!;
    const original = v.files.get(path);
    let current = true;
    v.beforeProcess = () => { current = false; };
    const result = await syncWithVault({ ...deps(v), valid: () => current }, "Wiki", changed());
    expect(result).toBe("superseded");
    expect(v.files.get(path)).toBe(original);
  });

  it("rejects an old run after waiting for the per-page write queue", async () => {
    const v = new FakeVault();
    let release!: () => void;
    const first = serialized("identity-test", () => new Promise<void>((resolve) => { release = resolve; }));
    let current = true;
    const stale = serialized("identity-test", () => syncWithVault({ ...deps(v), valid: () => current }, "Wiki", page()));
    await Promise.resolve();
    current = false;
    release();
    await first;
    expect(await stale).toBe("superseded");
    expect(v.files.size).toBe(0);
  });

  it("keeps only 20 page backups, leaving other pages, profile backups, nested and odd folders alone", async () => {
    const v = new FakeVault();
    const old = ".evowiki/backups/20250101T000000000Z-r";
    const odd = ".evowiki/backups/manual";
    const name = "z-score 标准化.md.bak";
    const untouched = [`${old}/other.md.bak`, `${old}/我.profile.bak`, `${old}/nested/${name}`, `${odd}/${name}`];
    v.folders.add(old); v.folders.add(`${old}/nested`); v.folders.add(odd);
    v.files.set(`${old}/${name}`, renderWikiNote(page(), NOW).markdown);
    for (const p of untouched) v.files.set(p, "keep");
    const other = ".evowiki/backups/20250102T000000000Z-r";
    v.folders.add(other);
    const otherText = renderWikiNote(page({ pageId: "p_other" }), NOW).markdown;
    v.files.set(`${other}/${name}`, otherText); // same filename, different page
    v.files.set(`${old}/old-title.md.bak`, renderWikiNote(page(), NOW).markdown);
    await syncWithVault(deps(v), "EvoWiki", page());
    for (let i = 0; i < 23; i++) {
      const d = { store: v, now: () => new Date(NOW.getTime() + i * 1000), rand: () => "abcd1234" };
      expect(await syncWithVault(d, "EvoWiki", changed({ sections: [sec("e", "explain", { body: `v${i}` })] }))).toBe("updated");
    }
    const backups = [...v.files.keys()].filter((p) => /^\.evowiki\/backups\/\d{8}T\d{9}Z-[a-z0-9]+\/z-score 标准化\.md\.bak$/.test(p)).sort();
    expect(backups.filter((p) => p !== `${other}/${name}`)).toHaveLength(20);
    expect(v.files.get(backups[1]!)).toContain("v2");
    expect(v.files.has(`${old}/old-title.md.bak`)).toBe(false);
    expect(v.files.get(`${other}/${name}`)).toBe(otherText);
    for (const p of untouched) expect(v.files.get(p)).toBe("keep");
    expect(v.folders.has(old)).toBe(true);
    expect(v.folders.has(BAK)).toBe(false);
  });
  it("creates EvoWiki/<title>.md with exactly the rendered note, and reads back identical", async () => {
    const v = new FakeVault();
    expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("created");
    expect(v.files.get("EvoWiki/z-score 标准化.md")).toBe(renderWikiNote(page(), NOW).markdown);
    // The written copy first (Q8), then the note.
    const k = kept(page());
    expect(v.writes).toEqual(["folder .evowiki", "folder .evowiki/written", `folder ${k.dir}`, `create ${k.path}`,
      "folder EvoWiki", "create EvoWiki/z-score 标准化.md"]);
  });

  it("same input again → unchanged, zero writes", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    v.writes = [];
    expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("unchanged");
    expect(v.writes).toEqual([]);
  });

  it("changed page → backup first, then only the managed properties and the block change; the user's keys and text stay", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    const path = "EvoWiki/z-score 标准化.md";
    const mine = v.files.get(path)!.replace("---\n目标:", "---\ntags: [mine]\n目标:").replace(`---\n${WIKI_START}`, `---\n我的开头\r\n${WIKI_START}`)
      + "我的笔记内容 [[均值]]\r\n";
    v.files.set(path, mine);
    v.writes = [];
    const next = page({ sections: [sec("o", "organizer"), sec("e", "explain", { folded: true })] });
    expect(await syncWithVault(deps(v), "EvoWiki", next)).toBe("updated");
    const k = kept(next);
    expect(v.writes).toEqual([`folder .evowiki/backups`, `folder ${BAK}`, `create ${BAK}/z-score 标准化.md.bak`,
      `create ${k.path}`, `process ${path}`]); // the page's copy folder is there since the create
    expect(v.files.get(`${BAK}/z-score 标准化.md.bak`)).toBe(mine);
    const out = v.files.get(path)!;
    expect(out.startsWith("---\ntags: [mine]\n目标: ")).toBe(true);
    expect(out).toContain("\n折起: \"1 段已掌握的\"\n");
    expect(out).toContain(`\n---\n我的开头\r\n${WIKI_START}\n${renderWikiBody(next)}${WIKI_END}\n`);
    expect(out.endsWith(`${WIKI_END}\n\n## 我的笔记\n\n${MINE_HINT}\n\n我的笔记内容 [[均值]]\r\n`)).toBe(true);
    expect(parseWikiFile(out)).toMatchObject({ kind: "one", version: 2, edited: false, hash: renderWikiNote(next).hash });
  });

  it("an unedited v1 note → backed up and migrated to v2 in place; the next sync is unchanged", async () => {
    const v = new FakeVault();
    const path = "EvoWiki/z-score 标准化.md";
    v.files.set(path, v1Note());
    expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("updated");
    expect(v.files.get(`${BAK}/z-score 标准化.md.bak`)).toBe(v1Note());
    expect(v.files.get(path)).toBe(renderWikiNote(page(), NOW).markdown);
    v.writes = [];
    expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("unchanged");
    expect(v.writes).toEqual([]);
  });

  it("a goal-only change → updated (properties rewritten after a backup); the same input again → unchanged", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    const path = "EvoWiki/z-score 标准化.md";
    const before = v.files.get(path)!;
    v.writes = [];
    expect(await syncWithVault(deps(v), "EvoWiki", page({ goal: "新目标" }))).toBe("updated");
    expect(v.files.get(`${BAK}/z-score 标准化.md.bak`)).toBe(before);
    expect(v.files.get(path)).toBe(renderWikiNote(page({ goal: "新目标" }), NOW).markdown);
    v.writes = [];
    expect(await syncWithVault(deps(v), "EvoWiki", page({ goal: "新目标" }))).toBe("unchanged");
    expect(v.writes).toEqual([]);
  });

  it("an edited v1 note → user_edited, zero writes", async () => {
    const v = new FakeVault();
    const path = "EvoWiki/z-score 标准化.md";
    const edited = v1Note().replace("正文e", "我改的");
    v.files.set(path, edited);
    expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("user_edited");
    expect(v.writes).toEqual([]);
    expect(v.files.get(path)).toBe(edited);
  });

  it("an edit inside the markers stays as typed whatever the page did (user_edited, zero writes), until the server took it", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    const path = "EvoWiki/z-score 标准化.md";
    const edited = v.files.get(path)!.replace("正文e", "我改的正文");
    v.files.set(path, edited);
    const n = v.writes.length;
    expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("user_edited");
    expect(await syncWithVault(deps(v), "EvoWiki", changed())).toBe("user_edited");
    expect(await syncWithVault(deps(v), "EvoWiki", changed(), "some other text")).toBe("user_edited");
    expect(v.writes).toHaveLength(n);
    expect(v.files.get(path)).toBe(edited);
    // the server took this exact text: written from the page, backed up first
    expect(await syncWithVault(deps(v), "EvoWiki", changed(), edited)).toBe("updated");
    const p = parseWikiFile(v.files.get(path)!);
    expect(p.kind === "one" && !p.edited && p.body.includes("新正文")).toBe(true);
    expect(v.files.get(`${BAK}/z-score 标准化.md.bak`)).toBe(edited);
  });

  it("a broken block (marker removed) for this page → user_edited, zero writes", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    const path = "EvoWiki/z-score 标准化.md";
    v.files.set(path, v.files.get(path)!.replace(WIKI_END, ""));
    v.writes = [];
    expect(await syncWithVault(deps(v), "EvoWiki", changed())).toBe("user_edited");
    expect(v.writes).toEqual([]);
  });

  it("opening or folding a callout is not an edit: unchanged when the page is the same, updated when it changed", async () => {
    const folded = page({ sections: [sec("e", "explain", { folded: true })] });
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", folded);
    const path = "EvoWiki/z-score 标准化.md";
    const toggled = v.files.get(path)!.replace("[!evowiki-known]- 提示e", "[!evowiki-known]+ 提示e");
    v.files.set(path, toggled);
    v.writes = [];
    expect(await syncWithVault(deps(v), "EvoWiki", folded)).toBe("unchanged");
    expect(v.files.get(path)).toBe(toggled); // the user's sign is left alone
    expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("updated");
  });

  it("backup failure → backup_failed and nothing else is written; the note is byte-identical", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    const path = "EvoWiki/z-score 标准化.md";
    const before = v.files.get(path)!;
    v.failCreate = /\.md\.bak$/;
    v.writes = [];
    expect(await syncWithVault(deps(v), "EvoWiki", changed())).toBe("backup_failed");
    expect(v.writes.some((w) => w.startsWith("process"))).toBe(false);
    expect(v.files.get(path)).toBe(before);
    expect([...v.files.keys()].some((p) => p.endsWith(".bak"))).toBe(false);
  });

  it("an occupied backup path → backup_failed, nothing written", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    v.files.set(`${BAK}/z-score 标准化.md.bak`, "older backup");
    v.writes = [];
    expect(await syncWithVault(deps(v), "EvoWiki", changed())).toBe("backup_failed");
    expect(v.writes).toEqual([]);
    expect(v.files.get(`${BAK}/z-score 标准化.md.bak`)).toBe("older backup");
  });

  it("a change between backup and write aborts: user_edited, the user's text wins, the backup stays", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    const path = "EvoWiki/z-score 标准化.md";
    v.beforeProcess = (p) => v.files.set(p, v.files.get(p)! + "刚写的一行\n");
    const typed = v.files.get(path)! + "刚写的一行\n";
    expect(await syncWithVault(deps(v), "EvoWiki", changed())).toBe("user_edited");
    expect(v.files.get(path)).toBe(typed);
    expect(v.files.has(`${BAK}/z-score 标准化.md.bak`)).toBe(true);
  });

  it("finds the page's note by page_id whatever its file name, updates it and renames it to the page's title", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    const text = v.files.get("EvoWiki/z-score 标准化.md")!;
    v.files.delete("EvoWiki/z-score 标准化.md");
    v.files.set("EvoWiki/sub/我改的名字.md", text);
    expect(await syncWithVault(deps(v), "EvoWiki", changed({ title: "标准分数" }))).toBe("updated");
    expect([...v.files.keys()].filter((p) => p.endsWith(".md")).sort()).toEqual(["EvoWiki/标准分数.md"]);
    expect(v.files.get("EvoWiki/标准分数.md")).toContain("\n新正文\n");
  });

  it("the title's file name taken by someone else's file -> 「标题（上级名）」, then 「（2）」; that file untouched", async () => {
    const v = new FakeVault();
    v.files.set("EvoWiki/Z-Score 标准化.md", "我自己的笔记\n"); // case-insensitive collision
    expect(await syncWithVault(deps(v), "EvoWiki", page({ parentName: "统计" }))).toBe("created");
    expect(v.files.get("EvoWiki/Z-Score 标准化.md")).toBe("我自己的笔记\n");
    expect(v.files.has("EvoWiki/z-score 标准化（统计）.md")).toBe(true);
    expect(await syncWithVault(deps(v), "EvoWiki", page({ pageId: "p_b", parentName: "统计" }))).toBe("created");
    expect(v.files.has("EvoWiki/z-score 标准化（统计）（2）.md")).toBe(true);
    // the next sync finds the first by page_id and leaves it where it is
    expect(await syncWithVault(deps(v), "EvoWiki", page({ parentName: "统计" }))).toBe("unchanged");
    expect(v.files.has("EvoWiki/z-score 标准化（统计）.md")).toBe(true);
  });

  it("no parent known -> the counter follows the title", async () => {
    const v = new FakeVault();
    v.files.set("EvoWiki/Z-Score 标准化.md", "x\n");
    await syncWithVault(deps(v), "EvoWiki", page());
    expect(v.files.has("EvoWiki/z-score 标准化（2）.md")).toBe(true);
  });

  it("a write still streaming leaves its file in the goal's folder, it does not move it back to the root", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page({ folder: "统计" }));
    v.writes = [];
    await syncWithVault(deps(v), "EvoWiki", page({ folder: "", writing: true, sections: [sec("o", "organizer"), sec("e", "explain", { body: "写到一半" })] }));
    expect(v.writes.filter((w) => w.startsWith("rename"))).toEqual([]);
    expect([...v.files.keys()].filter((f) => f.endsWith("z-score 标准化.md"))).toEqual(["EvoWiki/统计/z-score 标准化.md"]);
  });

  it("the file goes in the page's folder; a new folder moves it with renameFile", async () => {
    const v = new FakeVault();
    expect(await syncWithVault(deps(v), "EvoWiki", page({ folder: "统计" }))).toBe("created");
    expect(v.files.has("EvoWiki/统计/z-score 标准化.md")).toBe(true);
    expect(await syncWithVault(deps(v), "EvoWiki", page({ folder: "线性代数" }))).toBe("unchanged");
    expect([...v.files.keys()].filter((p) => p.endsWith(".md") && !p.startsWith(".evowiki/"))).toEqual(["EvoWiki/线性代数/z-score 标准化.md"]);
  });

  it("a rename that fails -> a notice with the reason, the note stays where it is and is still updated", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    v.rename = () => Promise.reject(new Error("文件被占用"));
    const told: string[] = [];
    expect(await syncWithVault({ ...deps(v), notify: (t) => told.push(t) }, "EvoWiki", changed({ title: "新名字" }))).toBe("updated");
    expect(v.files.has("EvoWiki/z-score 标准化.md")).toBe(true);
    expect(told).toHaveLength(1);
    expect(told[0]).toContain("文件被占用");
  });

  it("a trashed page's unedited note goes to the trash; an edited one stays; a page with no file does nothing", async () => {
    const v = new FakeVault();
    expect(await syncWithVault(deps(v), "EvoWiki", page({ trashed: true }))).toBe("unchanged");
    await syncWithVault(deps(v), "EvoWiki", page());
    const path = "EvoWiki/z-score 标准化.md";
    const text = v.files.get(path)!;
    v.files.set(path, text.replace("正文e", "我改了"));
    expect(await syncWithVault(deps(v), "EvoWiki", page({ trashed: true }))).toBe("user_edited");
    expect(v.files.has(path)).toBe(true);
    v.files.set(path, text);
    expect(await syncWithVault(deps(v), "EvoWiki", page({ trashed: true }))).toBe("trashed");
    expect(v.files.has(path)).toBe(false);
  });

  it("a trashed page's note with the learner's own 我的笔记 stays: only their text and properties, said why", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    const path = "EvoWiki/z-score 标准化.md";
    const mine = "我自己的例子：身高 z=1.2。";
    v.files.set(path, v.files.get(path)!.replace("---\n", "---\ntags:\n  - 统计\n") + mine + "\n");
    expect(await syncWithVault(deps(v), "EvoWiki", page({ trashed: true }))).toBe("kept_mine");
    const kept = v.files.get(path)!;
    expect(kept).toContain(mine);
    expect(kept).toContain(TRASHED_LINE);
    expect(kept).toContain("  - 统计");
    expect(kept).not.toContain(WIKI_START);
    expect(kept).not.toContain("正文e");
    expect(kept).not.toContain("evowiki_page_id");
    expect(parseWikiFile(kept).kind).toBe("none");           // it no longer claims the page
    expect(await syncWithVault(deps(v), "EvoWiki", page({ trashed: true }))).toBe("unchanged");
  });

  it("streaming: frontmatter evowiki-status: writing until the full sync takes it off", async () => {
    const v = new FakeVault();
    let n = 0;
    const d = () => ({ ...deps(v), rand: () => `r${n++}` });
    await syncWithVault(d(), "EvoWiki", page({ writing: true, sections: [sec("o", "organizer")] }));
    const path = "EvoWiki/z-score 标准化.md";
    expect(v.files.get(path)).toContain("evowiki-status: \"writing\"");
    await syncWithVault(d(), "EvoWiki", page({ writing: true }));
    expect(v.files.get(path)).toContain("evowiki-status: \"writing\"");
    expect(await syncWithVault(d(), "EvoWiki", page())).toBe("updated");
    expect(v.files.get(path)).not.toContain("evowiki-status");
    // same body, only the mark to remove
    await syncWithVault(d(), "EvoWiki", page({ writing: true }));
    expect(await syncWithVault(d(), "EvoWiki", page())).toBe("updated");
    expect(v.files.get(path)).not.toContain("evowiki-status");
  });

  it("a restarted session finalizes a partial note while preserving personal notes outside the generated region", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page({ writing: true, sections: [sec("o", "organizer")] }));
    const path = "EvoWiki/z-score 标准化.md";
    const personal = "我的笔记：均值是 5，中位数是 3。";
    v.files.set(path, `${v.files.get(path)!}\n${personal}\n`);
    expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("updated");
    const finished = v.files.get(path)!;
    expect(finished).toContain("正文e");
    expect(finished).toContain(personal);
    expect(finished).not.toContain("evowiki-status");
  });

  it("terminal recovery preserves a user's edit inside a partial note instead of forcing the completed server body", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page({ writing: true, sections: [sec("o", "organizer")] }));
    const path = "EvoWiki/z-score 标准化.md";
    const edited = v.files.get(path)!.replace("正文o", "我修改的正文");
    v.files.set(path, edited);
    expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("user_edited");
    expect(v.files.get(path)).toBe(edited);
  });

  it("a file at ANY valid candidate name stays (no flip between names), and onPath says where the file is", async () => {
    const v = new FakeVault();
    v.files.set("EvoWiki/Z-Score 标准化.md", "x\n");
    const got: [string, string][] = [];
    const d = { ...deps(v), onPath: (p: string, m: "no" | "yes" | "failed") => { got.push([p, m]); } };
    await syncWithVault(d, "EvoWiki", page({ parentName: "统计" }));
    expect(got[got.length - 1]).toEqual(["EvoWiki/z-score 标准化（统计）.md", "no"]);
    v.files.delete("EvoWiki/Z-Score 标准化.md");   // the clash is gone: the file still keeps its name
    await syncWithVault(d, "EvoWiki", page({ parentName: "统计" }));
    expect(v.files.has("EvoWiki/z-score 标准化（统计）.md")).toBe(true);
    expect(v.files.has("EvoWiki/z-score 标准化.md")).toBe(false);
    // a title change moves it, onPath says so
    await syncWithVault(d, "EvoWiki", changed({ title: "标准分数" }));
    expect(got[got.length - 1]).toEqual(["EvoWiki/标准分数.md", "yes"]);
  });

  it("placeOnly moves the file and leaves its text (no backup); a failed move says move_failed", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    const text = v.files.get("EvoWiki/z-score 标准化.md");
    v.writes = [];
    expect(await syncWithVault(deps(v), "EvoWiki", page({ placeOnly: true, folder: "统计", sections: [] }))).toBe("unchanged");
    expect(v.files.get("EvoWiki/统计/z-score 标准化.md")).toBe(text);
    expect(v.writes.some((w) => w.startsWith("process"))).toBe(false);
    v.rename = () => Promise.reject(new Error("busy"));
    expect(await syncWithVault({ ...deps(v), notify: () => undefined }, "EvoWiki", page({ placeOnly: true, folder: "线性代数" }))).toBe("move_failed");
    // a page with no file: nothing is created
    expect(await syncWithVault(deps(v), "EvoWiki", page({ pageId: "p_none", placeOnly: true }))).toBe("unchanged");
    expect([...v.files.keys()].filter((p) => p.endsWith(".md") && !p.startsWith(".evowiki/"))).toEqual(["EvoWiki/统计/z-score 标准化.md"]);
  });

  it("another page's generated note on the title path is never touched", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page({ pageId: "p_other" }));
    const other = v.files.get("EvoWiki/z-score 标准化.md")!;
    expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("created");
    expect(v.files.get("EvoWiki/z-score 标准化.md")).toBe(other);
  });

  it("two notes claiming the same page → rejects, nothing written", async () => {
    const v = new FakeVault();
    const md = renderWikiNote(page()).markdown;
    v.files.set("EvoWiki/a.md", md);
    v.files.set("EvoWiki/b.md", md);
    await expect(syncWithVault(deps(v), "EvoWiki", changed())).rejects.toThrow(/2 notes/);
    expect(v.writes).toEqual([]);
  });

  it("an invalid folder or page id → rejects, nothing written", async () => {
    const v = new FakeVault();
    for (const folder of ["../x", ".obsidian", ".evowiki/backups", "/abs"]) {
      await expect(syncWithVault(deps(v), folder, page())).rejects.toThrow();
    }
    await expect(syncWithVault(deps(v), "EvoWiki", page({ pageId: "a b" }))).rejects.toThrow();
    expect(v.writes).toEqual([]);
  });

  it("a page_id line in another page's note does not claim this page: that file is untouched, this page is created", async () => {
    const v = new FakeVault();
    const other = renderWikiNote(page({ pageId: "p_other", sections: [sec("o", "organizer", { body: "evowiki_page_id: p_zscore\npage_id=p_zscore" })] })).markdown
      + "evowiki_page_id: p_zscore\npage_id=p_zscore\n";
    v.files.set("EvoWiki/z-score 标准化.md", other);
    expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("created");
    expect(v.files.get("EvoWiki/z-score 标准化.md")).toBe(other);
    expect([...v.files.keys()].filter((p) => p.endsWith(".md"))).toHaveLength(2);
  });

  it("only searches inside the folder", async () => {
    const v = new FakeVault();
    v.files.set("Elsewhere/z.md", renderWikiNote(page()).markdown);
    expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("created");
    expect(v.files.get("Elsewhere/z.md")).toBe(renderWikiNote(page()).markdown);
  });
});

describe("serialized", () => {
  it("runs calls for the same key one after another, other keys in parallel", async () => {
    const log: string[] = [];
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const a = serialized("p1", async () => { log.push("a start"); await gate; log.push("a end"); return 1; });
    const b = serialized("p1", () => { log.push("b"); return Promise.resolve(2); });
    const c = serialized("p2", () => { log.push("c"); return Promise.resolve(3); });
    await c;
    expect(log).toEqual(["a start", "c"]);
    release();
    expect(await Promise.all([a, b])).toEqual([1, 2]);
    expect(log).toEqual(["a start", "c", "a end", "b"]);
  });

  it("a rejected call does not block the next one", async () => {
    await expect(serialized("p3", () => Promise.reject(new Error("x")))).rejects.toThrow("x");
    expect(await serialized("p3", () => Promise.resolve("ok"))).toBe("ok");
  });
});

describe("obsidianWikiNoteVault", () => {
  it("lists Markdown files under the folder (case-insensitive prefix), sorted; delegates reads", async () => {
    const mk = (p: string) => { const f = new TFile(); f.path = p; return f; };
    const files = [mk("evowiki/b.md"), mk("EvoWiki/a.md"), mk("EvoWikiX/c.md"), mk("Other/d.md")];
    const app = { vault: {
      getMarkdownFiles: () => files,
      getAllLoadedFiles: () => files,
      getAbstractFileByPath: (p: string) => files.find((f) => f.path === p) ?? null,
      read: (f: TFile) => Promise.resolve(`text ${f.path}`),
      adapter: { exists: () => Promise.resolve(false) },
    } };
    const v = obsidianWikiNoteVault(app as never);
    expect(v.listMarkdown("EvoWiki")).toEqual(["EvoWiki/a.md", "evowiki/b.md"]);
    expect(await v.read("EvoWiki/a.md")).toBe("text EvoWiki/a.md");
    expect(await v.occupied("EVOWIKI/A.md")).toBe(true);
  });
});

describe("the written copy (Q8, revised)", () => {
  const NOTE = "EvoWiki/z-score 标准化.md";
  /** The note as synced for page(), with one line of its generated text changed by the user. */
  async function editedNote(v: FakeVault): Promise<string> {
    await syncWithVault(deps(v), "EvoWiki", page());
    const text = v.files.get(NOTE)!.replace("正文e", "我改的正文");
    v.files.set(NOTE, text);
    return text;
  }

  it("created / updated: the copy holds exactly the written body and its layout, and loads back checked", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    expect(await loadWritten(v, "p_zscore", writtenFrom(page()).hash)).toEqual(writtenFrom(page()));
    await syncWithVault(deps(v), "EvoWiki", changed());
    expect(await loadWritten(v, "p_zscore", writtenFrom(changed()).hash)).toEqual(writtenFrom(changed()));
    expect(await loadWritten(v, "p_zscore", writtenFrom(page()).hash)).toEqual(writtenFrom(page())); // never overwritten
  });

  it("unchanged: a missing copy is kept then (notes written before the copies); an existing one is not touched", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    const k = kept(page());
    v.files.delete(k.path);
    v.writes = [];
    expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("unchanged");
    expect(v.writes).toEqual([`create ${k.path}`]);
    v.writes = [];
    expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("unchanged");
    expect(v.writes).toEqual([]);
  });

  it("a copy that cannot be written never changes the sync result (logged only)", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      const v = new FakeVault();
      v.failCreate = /^\.evowiki\/written\//;
      expect(await syncWithVault(deps(v), "EvoWiki", page())).toBe("created");
      expect(await syncWithVault(deps(v), "EvoWiki", changed())).toBe("updated");
      expect(warn).toHaveBeenCalled();
      expect(v.files.get(NOTE)).toContain("新正文");
    } finally { warn.mockRestore(); }
  });

  it("loadWritten fails closed: tampered body, another hash or page, bad layout, not JSON", async () => {
    const v = new FakeVault();
    await syncWithVault(deps(v), "EvoWiki", page());
    const { path } = kept(page());
    const hash = writtenFrom(page()).hash;
    const good = JSON.parse(v.files.get(path)!) as Record<string, unknown>;
    const put = (o: unknown) => { v.files.set(path, typeof o === "string" ? o : JSON.stringify(o)); };
    const layout = good.layout as { sections: { from: number; to: number }[] };
    for (const bad of [
      { ...good, body: `${String(good.body)}多一行\n` },
      { ...good, page_id: "other" },
      { ...good, hash: writtenFrom(changed()).hash },
      { ...good, v: 2 },
      { ...good, layout: { ...layout, sections: [...layout.sections].reverse() } },
      { ...good, layout: { ...layout, sections: [{ ...layout.sections[0]!, from: -1 }] } },
      { ...good, layout: null },
      "not json",
    ]) {
      put(bad);
      expect(await loadWritten(v, "p_zscore", hash)).toBeNull();
    }
    expect(await loadWritten(v, "p_zscore", "sha256:nothex")).toBeNull();
    v.files.delete(path);
    expect(await loadWritten(v, "p_zscore", hash)).toBeNull();
  });

  it("handEditFor: the page changed on the server since → still read against what was written", async () => {
    const v = new FakeVault();
    const text = await editedNote(v);
    const r = await handEditFor(v, text, changed());
    expect(r).toMatchObject({ kind: "ok", edit: { sections: [{ section_id: "e", body: "我改的正文" }], summary: "【标题e】\n删：正文e\n加：我改的正文" } });
  });

  it("handEditFor without a copy: the current render only when it is what was written; else it fails closed", async () => {
    const v = new FakeVault();
    const text = await editedNote(v);
    v.files.delete(kept(page()).path);
    expect(await handEditFor(v, text, page())).toMatchObject({ kind: "ok" });
    expect(await handEditFor(v, text, changed())).toMatchObject({ kind: "unsafe", why: "source" });
    expect(await handEditFor(v, text, { ...page(), pageId: "other" })).toEqual({ kind: "none" });
  });

});
