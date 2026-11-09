import { expect, it } from "vitest";
import { backupProfile, type BackupStore } from "./profile-backup";

function store(): BackupStore & { files: Map<string, string>; folders: Set<string>; removed: string[]; rmdirs: string[] } {
  const files = new Map<string, string>(), folders = new Set<string>(), removed: string[] = [], rmdirs: string[] = [];
  const direct = (p: string, set: Iterable<string>): string[] => [...set].filter((f) => f.startsWith(`${p}/`) && !f.slice(p.length + 1).includes("/"));
  return {
    files, folders, removed, rmdirs,
    occupied: (p) => Promise.resolve(files.has(p) || folders.has(p)),
    ensureFolder: (p) => { folders.add(p); return Promise.resolve(); },
    create: (p, t) => { files.set(p, t); return Promise.resolve(); },
    list: (p) => Promise.resolve({ files: direct(p, files.keys()), folders: direct(p, folders) }),
    removeFile: (p) => { removed.push(p); files.delete(p); return Promise.resolve(); },
    removeEmptyDir: (p) => { rmdirs.push(p); folders.delete(p); return Promise.resolve(); },
  };
}

const at = (i: number): Date => new Date(Date.UTC(2026, 10, 5, 0, 0, i));

it("keeps the note as .evowiki/backups/<stamp>-<rand>/我的背景.profile.bak and only the latest 20 of them", async () => {
  const s = store();
  for (let i = 0; i < 23; i++) await backupProfile(s, "EvoWiki/我的背景.md", `v${i}`, at(i), "r");
  const mine = [...s.files.keys()].filter((f) => f.endsWith("/我的背景.profile.bak")).sort();
  expect(mine).toHaveLength(20);
  expect(s.files.get(mine[0]!)).toBe("v3");
  expect(s.files.get(mine[19]!)).toBe("v22");
  expect(s.rmdirs).toHaveLength(3);                                 // each emptied folder goes, non-recursively
});

it("removes nothing but its own backup files: other notes' backups, other names, odd folders and traversal stay", async () => {
  const s = store();
  const page = ".evowiki/backups/20250101T000000000Z-page";
  s.folders.add(page); s.files.set(`${page}/我.md.bak`, "a page named 我");
  s.files.set(`${page}/我的背景.profile.bak`, "same folder as a page backup");
  for (const odd of [".evowiki/backups/../victim", ".evowiki/backups/notes", ".evowiki/backups/2025/nested", "other/20250101T000000000Z-x", ".evowiki/backups/20250101T000000000Z-X"]) {
    s.folders.add(odd); s.files.set(`${odd}/我的背景.profile.bak`, "keep");
  }
  for (let i = 0; i < 3; i++) await backupProfile(s, "EvoWiki/我的背景.md", `v${i}`, at(i), "r", 1);
  for (const f of s.removed) expect(f).toMatch(/^\.evowiki\/backups\/\d{8}T\d{9}Z-[a-z0-9]+\/我的背景\.profile\.bak$/);
  expect(s.files.get(`${page}/我.md.bak`)).toBe("a page named 我");
  for (const odd of [".evowiki/backups/../victim", ".evowiki/backups/notes", ".evowiki/backups/2025/nested", "other/20250101T000000000Z-x", ".evowiki/backups/20250101T000000000Z-X"]) {
    expect(s.files.get(`${odd}/我的背景.profile.bak`)).toBe("keep");
  }
  expect(s.rmdirs.every((d) => /^\.evowiki\/backups\/\d{8}T\d{9}Z-[a-z0-9]+$/.test(d) && d !== page)).toBe(true);
  expect(s.folders.has(page)).toBe(true);                           // it still holds a file of another kind, so it is not removed
});

it("a note path with traversal only names its own file", async () => {
  const s = store();
  await backupProfile(s, "EvoWiki/../../我的背景.md", "x", at(0), "r");
  expect([...s.files.keys()]).toEqual([".evowiki/backups/20261105T000000000Z-r/我的背景.profile.bak"]);
});

it("throws when the copy cannot be written; a rand that would leave the backup root is refused", async () => {
  const s = store();
  s.create = () => Promise.reject(new Error("disk full"));
  await expect(backupProfile(s, "EvoWiki/我的背景.md", "x", at(0), "r")).rejects.toThrow("disk full");
  await expect(backupProfile(store(), "EvoWiki/我的背景.md", "x", at(0), "../x")).rejects.toThrow("not a backup folder name");
});
