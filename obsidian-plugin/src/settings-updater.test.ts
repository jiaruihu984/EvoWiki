import { describe, expect, it, vi } from "vitest";
import { DEFAULT_SETTINGS, type EvoWikiSettings } from "./settings";
import { SettingsUpdater } from "./settings-updater";

function harness(initial: Partial<EvoWikiSettings> = {}) {
  let current: EvoWikiSettings = { ...DEFAULT_SETTINGS, userId: "alice", ...initial };
  const log: string[] = [];
  const deps = {
    get: () => current,
    prepare: vi.fn((s: EvoWikiSettings) => Promise.resolve(s)),
    save: vi.fn((s: EvoWikiSettings) => { log.push(`save ${s.serverUrl} ${s.userId}`); current = s; return Promise.resolve(); }),
    reinit: vi.fn(() => { log.push(`reinit ${current.serverUrl} ${current.userId}`); return Promise.resolve(); }),
  };
  return { deps, log, u: new SettingsUpdater(deps), now: () => current };
}

describe("SettingsUpdater", () => {
  it("a new user id: saves, then reinits for the new identity", async () => {
    const { u, log, now } = harness();
    await u.update({ userId: "bob" });
    expect(log).toEqual(["save http://127.0.0.1:8002 bob", "reinit http://127.0.0.1:8002 bob"]);
    expect(now().userId).toBe("bob");
  });

  it("a new server URL is an identity change too", async () => {
    const { u, log } = harness();
    await u.update({ serverUrl: "http://localhost:9000" });
    expect(log).toEqual(["save http://localhost:9000 alice", "reinit http://localhost:9000 alice"]);
  });

  it("overlapping updates both apply, in order, each reading prev when it runs", async () => {
    const { u, deps, log, now } = harness();
    let release!: () => void;
    const save = deps.save.getMockImplementation()!;
    deps.save.mockImplementationOnce((s: EvoWikiSettings) => new Promise<void>((r) => { release = () => { void save(s).then(r); }; }));
    const a = u.update({ serverUrl: "http://localhost:9000" });
    const b = u.update({ userId: "bob" });
    await Promise.resolve(); await Promise.resolve();
    expect(deps.save).toHaveBeenCalledOnce(); // A is blocked in save; B has not started
    release();
    await Promise.all([a, b]);
    expect(now()).toEqual({ ...DEFAULT_SETTINGS, serverUrl: "http://localhost:9000", userId: "bob" });
    expect(log).toEqual([
      "save http://localhost:9000 alice", "reinit http://localhost:9000 alice",
      "save http://localhost:9000 bob", "reinit http://localhost:9000 bob",
    ]);
  });

  it("a failed update does not block later ones", async () => {
    const { u, deps, now } = harness();
    deps.save.mockImplementationOnce(() => Promise.reject(new Error("disk")));
    await expect(u.update({ notesFolder: "Study" })).rejects.toThrow("disk");
    await u.update({ userId: "bob" });
    expect(now().userId).toBe("bob");
  });

  it("a local-only patch (allowSelectionContext, notesFolder) is saved and does not reinit", async () => {
    const { u, deps, now, log } = harness();
    await u.update({ allowSelectionContext: false });
    await u.update({ notesFolder: "Study" });
    expect(deps.reinit).not.toHaveBeenCalled();
    expect(log).toEqual(["save http://127.0.0.1:8002 alice", "save http://127.0.0.1:8002 alice"]);
    expect(now().allowSelectionContext).toBe(false);
    expect(now().notesFolder).toBe("Study");
  });
});


it.each(["D:/outside", "/outside", "../outside", "Study/../outside", ".obsidian", "Study\\outside"])("refuses invalid note directory %s before persistence", async (notesFolder) => {
  const { u, deps, now } = harness({ notesFolder: "Saved/Notes" });
  await expect(u.update({ notesFolder })).rejects.toThrow();
  expect(deps.save).not.toHaveBeenCalled();
  expect(now().notesFolder).toBe("Saved/Notes");
  await u.update({ allowSelectionContext: false });
  expect(now().notesFolder).toBe("Saved/Notes");
});

it("normalizes the directory once for all settings entry points", async () => {
  const { u, now } = harness({ notesFolder: "Saved/Notes" });
  await u.update({ notesFolder: " Study//New/ " });
  expect(now().notesFolder).toBe("Study/New");
});
it("persists the resolved account root and never switches identity when resolution fails", async () => {
  const { u, deps, now } = harness();
  deps.prepare.mockResolvedValueOnce({ ...DEFAULT_SETTINGS, userId: "bob", notesFolder: "EvoWiki (bob)" });
  await u.update({ userId: "bob" });
  expect(now().notesFolder).toBe("EvoWiki (bob)");
  deps.prepare.mockRejectedValueOnce(new Error("account folders overlap"));
  await expect(u.update({ userId: "charlie" })).rejects.toThrow("overlap");
  expect(now().userId).toBe("bob");
  expect(deps.save).toHaveBeenCalledOnce();
});
