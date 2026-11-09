import { validateNotesFolder } from "./notes/note-format";
import type { EvoWikiSettings } from "./settings";

export interface SettingsUpdaterDeps {
  /** The settings in force right now (the API client reads these at call time). */
  get(): EvoWikiSettings;
  /** Resolve the destination identity's independent vault root before switching settings. */
  prepare(s: EvoWikiSettings): Promise<EvoWikiSettings>;
  /** Make `s` the settings in force and persist them. */
  save(s: EvoWikiSettings): Promise<void>;
  /** Fresh controller/state for a new identity. */
  reinit(): Promise<void>;
}

/**
 * Applies settings patches one at a time. Each patch reads `prev` when it starts running, so overlapping
 * commits (server URL then user id while the first save is in flight) both apply, in order. An identity change (server
 * URL or user id) rebuilds the plugin's state for the new identity after saving.
 */
export class SettingsUpdater {
  private queue: Promise<void> = Promise.resolve();
  constructor(private readonly d: SettingsUpdaterDeps) {}

  update(patch: Partial<EvoWikiSettings>): Promise<void> {
    const run = this.queue.then(() => this.apply(patch));
    this.queue = run.catch(() => undefined);
    return run;
  }

  private async apply(patch: Partial<EvoWikiSettings>): Promise<void> {
    const prev = this.d.get();
    const next = { ...prev, ...patch };
    const folder = validateNotesFolder(next.notesFolder);
    if (!folder.ok) throw new Error(folder.reason);
    next.notesFolder = folder.folder;
    const identityChanged = next.userId !== prev.userId || next.serverUrl !== prev.serverUrl;
    await this.d.save(await this.d.prepare(next));
    if (identityChanged) await this.d.reinit();
  }
}
