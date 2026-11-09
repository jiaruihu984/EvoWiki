import { expect, it, vi } from "vitest";
import { DEFAULT_SETTINGS } from "../settings";
import { resolveProfilePath } from "./profile-path";
it("creates a background path directly in the account root and preserves its chosen language", async () => {
  const data = { settings: DEFAULT_SETTINGS, profileBases: {}, profilePaths: {} };
  const rename = vi.fn(); const save = vi.fn(() => Promise.resolve());
  const path = await resolveProfilePath(data, "zh", { exists: () => false, rename, save });
  expect(path).toBe("EvoWiki/我的背景.md");
  expect(await resolveProfilePath(data, "en", { exists: () => false, rename, save })).toBe(path);
  expect(save).toHaveBeenCalledOnce(); expect(rename).not.toHaveBeenCalled();
});
it("another account's background uses its independent root", async () => {
  const data = { settings: { ...DEFAULT_SETTINGS, userId: "bob", notesFolder: "EvoWiki (bob)" }, profileBases: {} };
  expect(await resolveProfilePath(data, "en", { exists: () => false, rename: vi.fn(), save: async () => {} })).toBe("EvoWiki (bob)/My background.md");
});
