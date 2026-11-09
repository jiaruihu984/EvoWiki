import type { ProfileOut } from "../generated/wiki";
import type { ProfileClient } from "./api";

/** The learner background (server: evowiki.profile): `<notes folder>/我的背景.md`, the server's canonical copy in the vault. */
export const profileNoteName = (language: "zh" | "en"): string => language === "zh" ? "我的背景.md" : "My background.md";
/** A save of the note is sent once typing pauses this long (ms). */
export const PROFILE_SAVE_MS = 1500;

/** The vault side (obsidianProfileVault, profile-vault.ts): read the note (null = no such file), create it, and replace its text only while it is still `expected`. */
export interface ProfileVault {
  read(path: string): Promise<string | null>;
  create(path: string, text: string): Promise<void>;
  /** Writes `text` if the note still reads `expected`; false (nothing written) when he changed it meanwhile. */
  replace(path: string, expected: string, text: string): Promise<boolean>;
  /** Keeps `text` (the note as it is about to be replaced) under .evowiki/backups, like a page note's backup; throws when it could not. */
  backup(path: string, text: string): Promise<void>;
}

/** The version of the copy each note is based on (`ProfileOut.version`), kept across restarts. */
export interface ProfileBases {
  get(path: string): string | null;
  set(path: string, version: string): Promise<void>;
}

/** Whether the note has a line besides its headings. */
const hasLines = (text: string): boolean => text.split("\n").some((line) => line.trim() !== "" && !/^\s*(?:#|<!--)/.test(line));

function memoryBases(): ProfileBases {
  const m = new Map<string, string>();
  return { get: (p) => m.get(p) ?? null, set: (p, v) => { m.set(p, v); return Promise.resolve(); } };
}

/**
 * Keeps 「我」 and the server's copy the same. `sync`: no note yet → the server's text is written; a note → its text is sent
 * with the version it is based on (the server merges it, his lines win; only the last copy can retract) and the merged text
 * written back. `edited`: the note was saved → `sync` once typing pauses. The text the plugin itself last wrote is not sent
 * again (its own write is a vault change too), unless the server's copy has no line at all: that never replaces a note with
 * lines, the note is sent instead. `kept`: lines the note lacks that the server did not retract (the note was another copy). `retracted`: lines of 已经会的 he
 * deleted that K took back, with their undo.
 */
export class ProfileNote {
  private written: string | null = null;
  private timer: number | null = null;
  private running: Promise<void> = Promise.resolve();
  private currentPath: string | null = null;

  constructor(private readonly api: ProfileClient, private readonly port: ProfileVault, private readonly path: () => string | Promise<string>,
              private readonly timers: { set: (fn: () => void, ms: number) => number; clear: (id: number) => void } =
                { set: (fn, ms) => window.setTimeout(fn, ms), clear: (id) => window.clearTimeout(id) },
              private readonly bases: ProfileBases = memoryBases(),
              private readonly kept: (n: number) => void = () => undefined,
              private readonly retracted: (lines: string[], undo: (() => Promise<void>) | null) => void = () => undefined) {}

  /** One sync at a time; a failure is logged and the next sync tries again. */
  sync(): Promise<void> {
    this.running = this.running.then(() => this.once()).catch((e: unknown) => { console.warn("EvoWiki: profile sync failed", e); });
    return this.running;
  }

  edited(path: string): void {
    const current = this.currentPath ?? this.path();
    if (path !== current) return;
    if (this.timer !== null) this.timers.clear(this.timer);
    this.timer = this.timers.set(() => { this.timer = null; void this.sync(); }, PROFILE_SAVE_MS);
  }

  /** A display language change fetches a rendering; it never submits the note as a user edit. */
  refreshLanguage(): Promise<void> {
    if (this.currentPath === null || this.written === null) return this.running;
    this.running = this.running.then(async () => {
      const path = await this.path();
      const mine = await this.port.read(path);
      if (mine === null || mine !== this.written) return;
      const out = await this.api.get();
      if (mine === out.text || await this.port.replace(path, mine, out.text)) await this.took(path, out);
    }).catch((e: unknown) => { console.warn("EvoWiki: profile language refresh failed", e); });
    return this.running;
  }

  private async took(path: string, out: ProfileOut): Promise<void> {
    this.written = out.text;
    await this.bases.set(path, out.version);
  }

  private async once(): Promise<void> {
    const path = await this.path();
    this.currentPath = path;
    const mine = await this.port.read(path);
    if (mine === null) {
      const out = await this.api.get();
      await this.port.create(path, out.text);
      await this.took(path, out);
      return;
    }
    if (mine === this.written) {                          // nothing of his to send: only take the server's news
      const out = await this.api.get();
      if (out.text === mine) { await this.took(path, out); return; }
      if (hasLines(out.text) || !hasLines(mine)) {
        if (await this.port.replace(path, mine, out.text)) await this.took(path, out);
        return;
      }
      console.warn("EvoWiki: the server's profile has no line; the note is kept and sent");
    }
    const out = await this.api.put(mine, this.bases.get(path));
    if (out.kept.length > 0) this.kept(out.kept.length);
    const took = out.retracted;
    if (took) {
      const id = took.action_id;
      this.retracted(took.lines, id === null ? null : async () => { await this.api.undoRetract(id); await this.sync(); });
    }
    if (out.text !== mine) await this.port.backup(path, mine);       // his text is about to be rewritten: no copy, no rewrite
    if (out.text === mine || await this.port.replace(path, mine, out.text)) await this.took(path, out);
  }
}
