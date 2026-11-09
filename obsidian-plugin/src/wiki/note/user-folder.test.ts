import { describe, expect, it } from "vitest";
import { DEFAULT_SETTINGS, loadPluginData, type EvoWikiSettings, type PluginData } from "../../settings";
import { accountFolder, accountIdentity, userFolder } from "./user-folder";
function data(): PluginData { return loadPluginData(null); }
function claim(d: PluginData, patch: Partial<EvoWikiSettings> = {}, occupied?: (path: string) => boolean): string {
  d.settings = accountFolder(d, { ...d.settings, ...patch }, occupied);
  return d.settings.notesFolder;
}
describe("independent flat account roots", () => {
  it("the first account uses the chosen root without user or Wiki directories", () => {
    const d = data(); expect(claim(d)).toBe("EvoWiki"); expect(userFolder(d.settings)).toBe("EvoWiki");
    expect(d.noteFolders).toEqual({ [accountIdentity(DEFAULT_SETTINGS)]: "EvoWiki" });
  });
  it("switching accounts restores each root and keeps new ones beside the original root", () => {
    const d = data(); claim(d);
    expect(claim(d, { userId: "bob" })).toBe("EvoWiki (bob)");
    expect(claim(d, { userId: "charlie" })).toBe("EvoWiki (charlie)");
    expect(claim(d, { userId: "me" })).toBe("EvoWiki");
    expect(claim(d, { userId: "bob" })).toBe("EvoWiki (bob)");
  });
  it("the same username on another server gets a different root; existing files are never claimed", () => {
    const d = data(); claim(d);
    expect(claim(d, { serverUrl: "https://second.example" }, p => p === "EvoWiki (me)")).toBe("EvoWiki (me) 2");
    const reloaded = loadPluginData(d); expect(claim(reloaded)).toBe("EvoWiki (me) 2"); expect(Object.keys(reloaded.noteFolders!)).toHaveLength(2);
  });
  it("folder edits cannot put either account inside the other account's scan", () => {
    const d = data(); claim(d); claim(d, { userId: "bob" });
    for (const notesFolder of ["EvoWiki", "evowiki/sub", "EvoWiki (bob)/../../bad"]) expect(() => claim(d, { notesFolder })).toThrow();
    expect(d.settings.notesFolder).toBe("EvoWiki (bob)");
  });
  it("a malformed stored mapping fails closed instead of combining identities", () => {
    const d = data(); claim(d); claim(d, { userId: "bob" }); d.noteFolders![accountIdentity(DEFAULT_SETTINGS)] = "EvoWiki (bob)/nested";
    expect(() => claim(d, { userId: "me" })).toThrow("overlap");
  });
});
