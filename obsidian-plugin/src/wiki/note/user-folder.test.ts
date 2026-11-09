import { describe, expect, it } from "vitest";
import { moveIntoUserFolder, type MoveVault } from "./user-folder";

function vault(paths: string[], failOn: string | null = null): MoveVault & { paths: Set<string> } {
  const set = new Set(paths);
  return {
    paths: set,
    exists: (p) => set.has(p),
    ensureFolder: (p) => { set.add(p); return Promise.resolve(); },
    rename: (from, to) => {
      if (from === failOn) return Promise.reject(new Error("busy"));
      for (const p of [...set]) if (p === from || p.startsWith(`${from}/`)) { set.delete(p); set.add(to + p.slice(from.length)); }
      return Promise.resolve();
    },
  };
}

describe("the layout from before per-user folders moves into the current user's folder", () => {
  const s = { notesFolder: "EvoWiki", userId: "me" };

  it("Wiki, 资料 and 我.md move under EvoWiki/<user>; another user's folder is left alone", async () => {
    const v = vault(["EvoWiki/Wiki", "EvoWiki/Wiki/甲/a.md", "EvoWiki/资料", "EvoWiki/资料/x.pdf", "EvoWiki/我.md", "EvoWiki/bob/Wiki/b.md"]);
    expect(await moveIntoUserFolder(v, s)).toEqual([]);
    expect([...v.paths].sort()).toEqual(["EvoWiki/bob/Wiki/b.md", "EvoWiki/me", "EvoWiki/me/Wiki", "EvoWiki/me/Wiki/甲/a.md",
      "EvoWiki/me/我.md", "EvoWiki/me/资料", "EvoWiki/me/资料/x.pdf"].sort());
  });

  it("a name the user's folder already has, or a failed rename, stays where it is and is reported", async () => {
    const v = vault(["EvoWiki/Wiki", "EvoWiki/me/Wiki", "EvoWiki/我.md", "EvoWiki/资料"], "EvoWiki/资料");
    expect(await moveIntoUserFolder(v, s)).toEqual(["EvoWiki/Wiki", "EvoWiki/资料"]);
    expect(v.paths.has("EvoWiki/Wiki")).toBe(true);
    expect(v.paths.has("EvoWiki/me/我.md")).toBe(true);
  });

  it("only Wiki, 资料 and 我.md are ever renamed: the user's own files under EvoWiki/ stay, an existing target is not overwritten", async () => {
    const v = vault(["EvoWiki/Wiki", "EvoWiki/Wiki/a.md", "EvoWiki/我的读书笔记.md", "EvoWiki/Daily", "EvoWiki/Daily/d.md",
      "EvoWiki/我.md", "EvoWiki/me/我.md"]);
    const renames: [string, string][] = [];
    const rename = v.rename.bind(v);
    v.rename = (from, to) => { renames.push([from, to]); return rename(from, to); };
    expect(await moveIntoUserFolder(v, s)).toEqual(["EvoWiki/我.md"]);
    expect(renames).toEqual([["EvoWiki/Wiki", "EvoWiki/me/Wiki"]]);
    for (const p of ["EvoWiki/我的读书笔记.md", "EvoWiki/Daily/d.md", "EvoWiki/我.md", "EvoWiki/me/我.md"]) expect(v.paths.has(p), p).toBe(true);
  });

  it("nothing to move: nothing changes", async () => {
    const v = vault(["EvoWiki/me/Wiki/a.md"]);
    expect(await moveIntoUserFolder(v, s)).toEqual([]);
    expect([...v.paths]).toEqual(["EvoWiki/me/Wiki/a.md"]);
  });
});
