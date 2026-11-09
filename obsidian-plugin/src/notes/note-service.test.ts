import { TFile } from "obsidian";
import { describe, expect, it } from "vitest";
import { ObsidianNoteVault } from "./note-service";

describe("ObsidianNoteVault", () => {
  function fakeObsidianVault(paths: string[], hidden: string[] = []) {
    const calls: string[] = [];
    const touched = new Set<string>();
    const files = new Map<string, TFile>();
    for (const p of paths) { const f = new TFile(); f.path = p; files.set(p, f); }
    const folder = { path: "Folder" };
    const raw = {
      getAllLoadedFiles: () => [...files.values(), folder],
      getAbstractFileByPath: (p: string) => files.get(p) ?? (p === "Folder" ? folder : null),
      read: (f: TFile) => { calls.push(`read ${f.path}`); return Promise.resolve(`text of ${f.path}`); },
      create: (p: string, t: string) => { calls.push(`create ${p} ${t}`); return Promise.resolve(new TFile()); },
      process: (f: TFile, fn: (d: string) => string) => { calls.push(`process ${f.path}`); return Promise.resolve(fn("old")); },
      createFolder: (p: string) => { calls.push(`createFolder ${p}`); return Promise.resolve({ path: p }); },
      adapter: new Proxy({ exists: (p: string) => Promise.resolve(hidden.includes(p)) }, {
        get(t, k) { touched.add(`adapter.${String(k)}`); return (t as Record<string | symbol, unknown>)[k]; },
      }),
    };
    const vault = new Proxy(raw, { get(t, k) { touched.add(String(k)); return (t as Record<string | symbol, unknown>)[k]; } });
    return { vault, calls, touched };
  }

  it("occupied is case-insensitive over loaded files and folders, and also asks the adapter (unindexed dot folders)", async () => {
    const f = fakeObsidianVault(["EvoWiki/PCA.md"], [".evowiki/backups/x/PCA.md.bak"]);
    const nv = new ObsidianNoteVault(f.vault as never);
    expect(await nv.occupied("evowiki/pca.md")).toBe(true);
    expect(await nv.occupied("folder")).toBe(true);
    expect(await nv.occupied(".evowiki/backups/x/PCA.md.bak")).toBe(true);
    expect(await nv.occupied("EvoWiki/SVD.md")).toBe(false);
  });

  it("read/process need a TFile (a folder or missing path rejects); create delegates", async () => {
    const f = fakeObsidianVault(["EvoWiki/PCA.md"]);
    const nv = new ObsidianNoteVault(f.vault as never);
    expect(await nv.read("EvoWiki/PCA.md")).toBe("text of EvoWiki/PCA.md");
    await expect(nv.read("Folder")).rejects.toThrow();
    await expect(nv.read("missing.md")).rejects.toThrow();
    expect(await nv.process("EvoWiki/PCA.md", (d) => d + "!")).toBe("old!");
    await expect(nv.process("Folder", (d) => d)).rejects.toThrow();
    await nv.create("EvoWiki/New.md", "hello");
    expect(f.calls).toEqual(["read EvoWiki/PCA.md", "process EvoWiki/PCA.md", "create EvoWiki/New.md hello"]);
  });

  it("ensureFolder creates only the missing segments, in order", async () => {
    const f = fakeObsidianVault([], [".evowiki"]);
    const nv = new ObsidianNoteVault(f.vault as never);
    await nv.ensureFolder("Folder/A/B");
    await nv.ensureFolder(".evowiki/backups/t-1");
    expect(f.calls).toEqual(["createFolder Folder/A", "createFolder Folder/A/B", "createFolder .evowiki/backups", "createFolder .evowiki/backups/t-1"]);
  });

  it("uses only the allowed Vault members; the adapter only for exists", async () => {
    const f = fakeObsidianVault(["EvoWiki/PCA.md"]);
    const nv = new ObsidianNoteVault(f.vault as never);
    await nv.occupied("x"); await nv.read("EvoWiki/PCA.md"); await nv.create("y.md", "t");
    await nv.process("EvoWiki/PCA.md", (d) => d); await nv.ensureFolder("A/B");
    const allowed = new Set(["getAllLoadedFiles", "getAbstractFileByPath", "read", "create", "process", "createFolder", "adapter", "adapter.exists"]);
    expect([...f.touched].filter((k) => !allowed.has(k))).toEqual([]);
  });
});
