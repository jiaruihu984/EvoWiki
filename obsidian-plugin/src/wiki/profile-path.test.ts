import { expect, it, vi } from "vitest";
import { DEFAULT_SETTINGS } from "../settings";
import { resolveProfilePath } from "./profile-path";
it("renames the legacy note through the vault port and transfers its merge version", async () => {
  const data = { settings: DEFAULT_SETTINGS, profileBases: { "EvoWiki/me/我.md": "v42" }, profilePaths: {} };
  const rename = vi.fn(() => Promise.resolve()); const save = vi.fn(() => Promise.resolve());
  const path = await resolveProfilePath(data, "en", { exists: (p) => p === "EvoWiki/me/我.md", rename, save });
  expect(rename).toHaveBeenCalledWith("EvoWiki/me/我.md", "EvoWiki/me/My background.md");
  expect(data.profileBases).toEqual({ "EvoWiki/me/My background.md": "v42" }); expect(save).toHaveBeenCalledOnce();
  expect(await resolveProfilePath(data, "zh", { exists: () => false, rename, save })).toBe(path);
  expect(rename).toHaveBeenCalledOnce();
});
it("a target collision never overwrites either file or moves its version", async () => {
  const data = { settings: DEFAULT_SETTINGS, profileBases: { "EvoWiki/me/我.md": "v42" } };
  const rename = vi.fn(); const save = vi.fn();
  await expect(resolveProfilePath(data, "en", { exists: () => true, rename, save })).rejects.toThrow();
  expect(rename).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(data.profileBases).toEqual({ "EvoWiki/me/我.md": "v42" });
});
