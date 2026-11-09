import { tr } from "./i18n/index";
import { validateNotesFolder } from "./notes/note-format";
import { parseLocalSetup, type LocalSetupMetadata } from "./local-setup";

export interface EvoWikiSettings {
  serverUrl: string; userId: string; allowSelectionContext: boolean;
  /** Independent vault root for the active server/user identity. */
  notesFolder: string;
}
export const DEFAULT_SETTINGS: EvoWikiSettings =
  { serverUrl: "http://127.0.0.1:8002", userId: "me", allowSelectionContext: true, notesFolder: "EvoWiki" };
/** Reading assistant (F3o): key = JSON [serverUrl, userId, "page:<id>" | "note:<path>"], value = session id. Ids only. */
export const MAX_ASSISTANT_SESSIONS = 200;
export const MAX_PROFILE_BASES = 20;
/** The sidebar's red dots: the pending items already seen (wiki/dots.ts keys), the newest MAX_SEEN_DOTS kept. */
export const MAX_SEEN_DOTS = 200;
/**
 * The chat diagnoses of goals confirmed in the chat (assistant goal-run.ts): key = JSON [serverUrl, userId, the goal_new
 * row's id], value = the block
 * the run entered ("" before it entered one). A reloaded chat rebuilds only these. Ids only; the newest MAX_GOAL_RUNS kept.
 */
export const MAX_GOAL_RUNS = 50;
export interface PluginData {
  settings: EvoWikiSettings; assistantSessions: Record<string, string>;
  goalRuns: Record<string, string>;
  /** 我.md path → the version of the copy it is based on (ProfileNote). */
  profileBases: Record<string, string>;
  seenDots: string[];
  /** Startup display only. The connected account's WikiState.language is authoritative. */
  bootstrapLanguage?: "zh" | "en";
  /** JSON [serverUrl, userId] → the actual managed background note path. */
  profilePaths?: Record<string, string>;
  /** JSON [serverUrl, userId] → independent vault root; never evicted while its files exist. */
  noteFolders?: Record<string, string>;
  localSetup?: LocalSetupMetadata;
  /** UI bookmark per identity; no credentials or authoritative service settings. */
  setupSteps?: Record<string, string>;
  /** The one-time rename of the old 「X（i-n）」 wiki files to the page titles and folders has run (Task 13). */
  wikiFilesMigrated?: true;
  /**
   * Dev only, never shown in the settings UI: "demo" runs the real wiki controller, the assistant and the usage section
   * on fakes seeded with the approved demo's data (src/wiki/fixtures-demo.ts), for side-by-side screenshots. The key is
   * present only when valid.
   */
  fixture?: "demo";
}

const USER_ID = /^[A-Za-z0-9_.-]{1,64}$/; // CreateUserIn.user_id
const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]"]);

export const isValidUserId = (s: string): boolean => USER_ID.test(s);

export function normalizeServerUrl(input: string):
  { ok: true; url: string; loopback: boolean } | { ok: false; reason: string } {
  let u: URL;
  try { u = new URL(input.trim()); } catch { return { ok: false, reason: tr("settings.reason.1") }; }
  if (u.protocol !== "http:" && u.protocol !== "https:") return { ok: false, reason: tr("settings.reason.2") };
  return { ok: true, url: `${u.protocol}//${u.host}`, loopback: LOOPBACK.has(u.hostname) || u.hostname === "::1" };
}

const obj = (x: unknown): Record<string, unknown> =>
  x !== null && typeof x === "object" && !Array.isArray(x) ? (x as Record<string, unknown>) : {};

/** E1: a stored folder survives only if it validates (stored normalised); anything else is the default. */
function notesFolder(v: unknown): string {
  if (typeof v !== "string") return DEFAULT_SETTINGS.notesFolder;
  const r = validateNotesFolder(v);
  return r.ok ? r.folder : DEFAULT_SETTINGS.notesFolder;
}

/** Only known keys with the right type survive; everything else falls back to defaults. */
/** data.json's text as an object (a leading BOM is no damage); throws on anything else: never read as "no settings yet". */
export function parsePluginData(text: string): Record<string, unknown> {
  const raw: unknown = JSON.parse(text.replace(/^\uFEFF/, ""));
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) throw new Error("data.json is not an object");
  return raw as Record<string, unknown>;
}

export function loadPluginData(raw: unknown): PluginData {
  const r = obj(raw); const s = obj(r.settings);
  const profilePaths = recentStrings(r.profilePaths, MAX_PROFILE_BASES);
  const noteFolders = recentStrings(r.noteFolders, Number.MAX_SAFE_INTEGER);
  const setupSteps = recentStrings(r.setupSteps, MAX_PROFILE_BASES);
  const localSetup = parseLocalSetup(r.localSetup);
  const settings: EvoWikiSettings = {
    serverUrl: typeof s.serverUrl === "string" ? s.serverUrl : DEFAULT_SETTINGS.serverUrl,
    userId: typeof s.userId === "string" ? s.userId : DEFAULT_SETTINGS.userId,
    allowSelectionContext: typeof s.allowSelectionContext === "boolean" ? s.allowSelectionContext : DEFAULT_SETTINGS.allowSelectionContext,
    notesFolder: notesFolder(s.notesFolder),
  };
  // Keys of the retired concept-era UI (showLegacy, viewSource / showDemoView, its session ids) are dropped here.
  return { settings, assistantSessions: recentStrings(r.assistantSessions, MAX_ASSISTANT_SESSIONS),
    ...(r.bootstrapLanguage === "zh" || r.bootstrapLanguage === "en" ? { bootstrapLanguage: r.bootstrapLanguage } : {}),
    ...(Object.keys(profilePaths).length ? { profilePaths } : {}),
    ...(Object.keys(noteFolders).length ? { noteFolders } : {}),
    ...(localSetup ? { localSetup } : {}),
    ...(Object.keys(setupSteps).length ? { setupSteps } : {}),
    goalRuns: recentStrings(r.goalRuns, MAX_GOAL_RUNS),
    profileBases: recentStrings(r.profileBases, MAX_PROFILE_BASES),
    seenDots: Array.isArray(r.seenDots) ? r.seenDots.filter((x): x is string => typeof x === "string").slice(-MAX_SEEN_DOTS) : [],
    // Only the one known fixture; anything else (a typo, another type) is dropped and the plugin runs as usual.
    ...(r.wikiFilesMigrated === true ? { wikiFilesMigrated: true as const } : {}),
    ...(r.fixture === "demo" ? { fixture: "demo" as const } : {}) };
}

/** Insertion order = recency (session-store.ts re-appends on use): the newest `max` string entries, others dropped. */
function recentStrings(raw: unknown, max: number): Record<string, string> {
  const out: Record<string, string> = {};
  const kept = Object.entries(obj(raw)).filter(([, v]) => typeof v === "string").slice(-max);
  // defineProperty, not `[k] =`: a "__proto__" key would otherwise be dropped.
  for (const [k, v] of kept) Object.defineProperty(out, k, { value: v, writable: true, enumerable: true, configurable: true });
  return out;
}
