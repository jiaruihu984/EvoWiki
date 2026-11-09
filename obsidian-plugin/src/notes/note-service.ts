import { TFile, type Vault } from "obsidian";

/** The narrow vault port: the only writes are create, createBinary, process and ensureFolder (createFolder). Nothing deletes or renames. */
export interface NoteVault {
  /** Case-insensitive: true if any existing file OR folder path equals `path` ignoring case (E1). */
  occupied(path: string): Promise<boolean>;
  read(path: string): Promise<string>;
  /** obsidian vault.create; rejects if it exists. */
  create(path: string, text: string): Promise<void>;
  /** obsidian vault.process. */
  process(path: string, fn: (data: string) => string): Promise<string>;
  /** vault.createFolder for each missing segment. */
  ensureFolder(path: string): Promise<void>;
}

/**
 * The one binary write: a file from the computer saved once under `<notes folder>/资料/` (wiki/sources/save-original.ts).
 * Rejects if the path is taken, never overwrites. Kept apart from NoteVault so the text-note fakes need not know it.
 */
export interface BinaryVault {
  createBinary(path: string, data: ArrayBuffer): Promise<void>;
}

/** NoteVault over Obsidian's Vault. Writes: vault.create, vault.createBinary, vault.process, vault.createFolder — nothing else. */
export class ObsidianNoteVault implements NoteVault, BinaryVault {
  constructor(private readonly vault: Vault) {}

  async occupied(path: string): Promise<boolean> {
    const l = path.toLowerCase();
    if (this.vault.getAllLoadedFiles().some((f) => f.path.toLowerCase() === l)) return true;
    return this.vault.adapter.exists(path); // covers unindexed dot folders such as .evowiki
  }

  async read(path: string): Promise<string> { return this.vault.read(this.file(path)); }

  async create(path: string, text: string): Promise<void> { await this.vault.create(path, text); }

  async createBinary(path: string, data: ArrayBuffer): Promise<void> {
    // Obsidian refuses an existing path itself; the check also covers a case-only clash and an unindexed folder.
    if (await this.occupied(path)) throw new Error(`EvoWiki: ${path} already exists`);
    await this.vault.createBinary(path, data);
  }

  async process(path: string, fn: (data: string) => string): Promise<string> { return this.vault.process(this.file(path), fn); }

  async ensureFolder(path: string): Promise<void> {
    const parts = path.split("/").filter((x) => x !== "");
    for (let i = 1; i <= parts.length; i++) {
      const seg = parts.slice(0, i).join("/");
      if (this.vault.getAbstractFileByPath(seg) !== null || await this.vault.adapter.exists(seg)) continue;
      await this.vault.createFolder(seg);
    }
  }

  private file(path: string): TFile {
    const f = this.vault.getAbstractFileByPath(path);
    if (!(f instanceof TFile)) throw new Error(`Not a file: ${path}`);
    return f;
  }
}
