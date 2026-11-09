/** What 资料 needs from Obsidian (sources/obsidian-sources.ts builds the real one; tests pass a fake). */

export interface SourcePort {
  /** The path a file named `name` would get under `<notes folder>/资料/` (first free name; nothing is written). */
  originalPath(name: string): Promise<string>;
  /** Writes an original at a path from originalPath, once: an existing file is never overwritten (rejects). */
  writeOriginal(path: string, data: ArrayBuffer): Promise<void>;
  /** Opens a vault file (Obsidian's own viewer, else the system's app; a PDF at `page`); false when there is no such file. */
  openFile(path: string, page?: number): Promise<boolean>;
  /** True when a vault file exists at `path`. */
  exists(path: string): boolean;
}
