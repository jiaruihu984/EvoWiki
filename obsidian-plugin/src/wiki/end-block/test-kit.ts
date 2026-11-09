/** Test-only helpers for the end-block tests (no production code imports this). */
import { CheckBlockHub } from "../check-block";
import type { WikiController } from "../controller";
import { firstPage, setup, type Setup } from "../controller-test-kit";
import { syncWithVault, type WikiNoteVault } from "../note/sync";
import type { WikiNoteInput } from "../note-port";

/** An in-memory vault for the real note writer (same shape sync.test.ts uses). */
export class MemVault implements WikiNoteVault {
  files = new Map<string, string>();
  folders = new Set<string>();
  list(p: string) {
    const direct = (paths: Iterable<string>) => [...paths].filter((f) => f.startsWith(`${p}/`) && !f.slice(p.length + 1).includes("/"));
    return Promise.resolve({ files: direct(this.files.keys()), folders: direct(this.folders) });
  }
  removeFile(p: string) { this.files.delete(p); return Promise.resolve(); }
  removeEmptyDir(p: string) { this.folders.delete(p); return Promise.resolve(); }

  occupied(path: string) { const l = path.toLowerCase(); return Promise.resolve([...this.files.keys(), ...this.folders].some((p) => p.toLowerCase() === l)); }
  read(path: string) { const t = this.files.get(path); return t === undefined ? Promise.reject(new Error(`missing ${path}`)) : Promise.resolve(t); }
  readHidden(path: string) { return Promise.resolve(this.files.get(path) ?? null); }
  create(path: string, text: string) { this.files.set(path, text); return Promise.resolve(); }
  process(path: string, fn: (d: string) => string) {
    const t = this.files.get(path);
    if (t === undefined) return Promise.reject(new Error(`missing ${path}`));
    const out = fn(t);
    this.files.set(path, out);
    return Promise.resolve(out);
  }
  ensureFolder(path: string) { this.folders.add(path); return Promise.resolve(); }
  rename(from: string, to: string) { const t = this.files.get(from); if (t === undefined || this.files.has(to)) return Promise.reject(new Error("cannot rename")); this.files.delete(from); this.files.set(to, t); return Promise.resolve(); }
  trash(path: string) { this.files.delete(path); return Promise.resolve(); }
  listMarkdown(folder: string) { return [...this.files.keys()].filter((p) => p.toLowerCase().startsWith(`${folder.toLowerCase()}/`) && p.endsWith(".md")).sort(); }
}

export interface Harness extends Setup { vault: MemVault; hub: CheckBlockHub<WikiController>; path: string; text(): string }

/** A controller on the scripted fake whose note writer is the real one over an in-memory vault, on the first page. */
export async function harness(o: Parameters<typeof setup>[0] = {}): Promise<Harness> {
  const s = setup(o);
  const vault = new MemVault();
  let n = 0;   // every sync gets its own backup name
  const deps = { store: vault, now: () => new Date(Date.UTC(2026, 9, 9, 8, 0, n)), rand: () => `abcd${String(++n).padStart(4, "0")}` };
  s.sync.run = async (input: WikiNoteInput) => { s.sync.runs.push(input); return syncWithVault(deps, "Notes", input); };
  await firstPage(s.c);
  const hub = new CheckBlockHub<WikiController>();
  hub.set(s.c);
  const path = [...vault.files.keys()].find((p) => p.endsWith(".md"));
  if (path === undefined) throw new Error("no note written");
  return { ...s, vault, hub, path, text: () => vault.files.get(path) ?? "" };
}
