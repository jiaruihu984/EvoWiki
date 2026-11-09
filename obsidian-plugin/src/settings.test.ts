import { expect, it } from "vitest";
import { DEFAULT_SETTINGS, isValidUserId, loadPluginData, MAX_ASSISTANT_SESSIONS, parsePluginData, normalizeServerUrl } from "./settings";

it("defaults to loopback, user me (a valid id the account check registers), selection context allowed, notes folder EvoWiki", () => {
  expect(DEFAULT_SETTINGS).toEqual({ serverUrl: "http://127.0.0.1:8002", userId: "me", allowSelectionContext: true, notesFolder: "EvoWiki" });
});

it("the retired concept-era keys (showLegacy, viewSource / showDemoView, session ids) are dropped on load", () => {
  expect(loadPluginData({ settings: { showLegacy: true, viewSource: "demo", showDemoView: true }, sessions: { jerry: "ses_1" } }))
    .toEqual({ settings: DEFAULT_SETTINGS, assistantSessions: {}, goalRuns: {}, profileBases: {}, seenDots: [] });
});

it("keeps a stored notes folder only if validateNotesFolder accepts it (normalised), else the default", () => {
  const nf = (v: unknown) => loadPluginData({ settings: { notesFolder: v } }).settings.notesFolder;
  expect(nf("Study//Concepts/")).toBe("Study/Concepts");
  expect(nf("Notes")).toBe("Notes");
  for (const bad of ["../x", "/abs", "C:/x", "a\\b", ".obsidian", ".evowiki/backups", ".EvoWiki", 5, null]) expect(nf(bad), String(bad)).toBe("EvoWiki");
  expect(nf("")).toBe("EvoWiki");
});

it("loads stored data defensively and drops unknown or mistyped fields", () => {
  expect(loadPluginData(null)).toEqual({ settings: DEFAULT_SETTINGS, assistantSessions: {}, goalRuns: {}, profileBases: {}, seenDots: [] });
  expect(loadPluginData({ settings: { serverUrl: "http://localhost:9000", userId: 7, allowSelectionContext: "no", mastery: 1 } }))
    .toEqual({ settings: { serverUrl: "http://localhost:9000", userId: "me", allowSelectionContext: true, notesFolder: "EvoWiki" },
      assistantSessions: {}, goalRuns: {}, profileBases: {}, seenDots: [] });
  expect(loadPluginData({ settings: { allowSelectionContext: false } }).settings.allowSelectionContext).toBe(false);
  expect(loadPluginData({ settings: { allowSelectionContext: 0 } }).settings.allowSelectionContext).toBe(true);
});

it("normalises server URLs and flags non-loopback hosts", () => {
  expect(normalizeServerUrl(" http://127.0.0.1:8000/ ")).toEqual({ ok: true, url: "http://127.0.0.1:8000", loopback: true });
  expect(normalizeServerUrl("http://[::1]:8000")).toEqual({ ok: true, url: "http://[::1]:8000", loopback: true });
  expect(normalizeServerUrl("https://evo.example.com")).toEqual({ ok: true, url: "https://evo.example.com", loopback: false });
  expect(normalizeServerUrl("ftp://x")).toMatchObject({ ok: false });
  expect(normalizeServerUrl("not a url")).toMatchObject({ ok: false });
});

it("accepts the server's user id pattern only", () => {
  expect(isValidUserId("jerry_1.x-y")).toBe(true);
  expect(isValidUserId("")).toBe(false);
  expect(isValidUserId("a:b")).toBe(false);
  expect(isValidUserId("x".repeat(65))).toBe(false);
});

it("assistantSessions: ids only, mistyped entries dropped, missing field → {}", () => {
  expect(loadPluginData(null).assistantSessions).toEqual({});
  expect(loadPluginData({ assistantSessions: "x" }).assistantSessions).toEqual({});
  expect(loadPluginData({ assistantSessions: { a: "ses_a", b: 3, c: null, d: { x: 1 } } }).assistantSessions).toEqual({ a: "ses_a" });
});

it("assistantSessions keeps the 200 most recent entries in order (oldest first dropped)", () => {
  const raw: Record<string, string> = {};
  for (let i = 0; i < 250; i++) raw[`k${i}`] = `s${i}`;
  const got = Object.keys(loadPluginData({ assistantSessions: raw }).assistantSessions);
  expect(MAX_ASSISTANT_SESSIONS).toBe(200);
  expect(got).toHaveLength(200);
  expect(got[0]).toBe("k50");
  expect(got[199]).toBe("k249");
});

it("assistantSessions keeps a __proto__ key as an own property", () => {
  const raw = JSON.parse('{"assistantSessions":{"__proto__":"ses_p"}}') as unknown;
  const a = loadPluginData(raw).assistantSessions;
  expect(Object.prototype.hasOwnProperty.call(a, "__proto__")).toBe(true);
  expect(Object.getOwnPropertyDescriptor(a, "__proto__")?.value).toBe("ses_p");
});

it("fixture (dev only): exactly \"demo\" is kept at the top level; anything else, or nothing, leaves no key", () => {
  const raw = { settings: { serverUrl: "http://127.0.0.1:8001", userId: "regression" }, fixture: "demo" };
  expect(loadPluginData(raw).fixture).toBe("demo");
  // Never a setting: the settings object is the same with or without it.
  expect(loadPluginData(raw).settings).toEqual(loadPluginData({ ...raw, fixture: undefined }).settings);
  for (const v of ["Demo", "live", true, 1, null, {}, ["demo"]]) expect("fixture" in loadPluginData({ fixture: v }), JSON.stringify(v)).toBe(false);
  expect("fixture" in loadPluginData({ settings: { fixture: "demo" } })).toBe(false);
  expect(loadPluginData(null)).not.toHaveProperty("fixture");
});

it("parsePluginData: a BOM is no damage; broken text or a non-object throws (never read as no settings)", () => {
  expect(parsePluginData('\uFEFF{"settings":{"userId":"a"}}')).toEqual({ settings: { userId: "a" } });
  for (const bad of ["{broken", "", "[]", "null", '"x"']) expect(() => parsePluginData(bad)).toThrow();
});
