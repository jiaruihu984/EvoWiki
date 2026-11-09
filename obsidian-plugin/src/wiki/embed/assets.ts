/**
 * Fetching interactive assets into the vault (appendix A·6). After a page's note sync, and from a block's 「取回」
 * button: GET /wiki/assets/{id}/meta → GET /wiki/assets/{id} → the HTML's sha256 must equal meta.sha256 and it must be
 * at most INTERACTIVE_MAX_BYTES (deep/contracts.py) → written to `<notes>/Wiki/assets/<id>.html`. Only that folder is ever written; an existing
 * file is never fetched again, overwritten or deleted (an asset id's content does not change). A mismatch, an
 * oversized file or a failed request writes nothing (console.warn); 503 (refine is off on the server) is silent.
 * An image (kind "image", an `evowiki-image` fence) comes as its WebP in base64 text: the sha256 is of that text, at
 * most MAX_IMAGE_B64 characters, and it must decode to a WebP; it is saved as the binary `<id>.webp`.
 */
import { createHash } from "node:crypto";
import { ApiError } from "../../api/errors";
import { validateNotesFolder } from "../../notes/note-format";
import type { App } from "obsidian";
import { type BinaryVault, type NoteVault, ObsidianNoteVault } from "../../notes/note-service";
import type { WikiApi } from "../api";
import { EVOUI_FENCE, parseEvouiSource } from "../evoui/block";
import { assetIdOfSrc, validAssetId, EMBED_FENCE, parseEmbedBlock } from "./embed-block";
import { IMAGE_FENCE, parseImageBlock } from "./image-block";
import { INTERACTIVE_MAX_BYTES } from "../../generated/wiki";

/** deep/contracts.py INTERACTIVE_MAX_BYTES: a component may inline a vendored library (network-edit: Cytoscape.js). */
export const MAX_ASSET_BYTES = INTERACTIVE_MAX_BYTES;
/** deep/contracts.py IMAGE_MAX_B64: the stored WebP in base64. */
export const MAX_IMAGE_B64 = 2_100_000;
const SHA256_HEX = /^[0-9a-f]{64}$/;

export type AssetSource = Pick<WikiApi, "assetGenerated" | "assetMeta" | "assetHtml">;
export type AssetStore = Pick<NoteVault, "occupied" | "ensureFolder" | "create"> & BinaryVault;
/** written: fetched and saved; present: already there; failed: nothing written (logged); unavailable: 503, silent. */
export type FetchResult = "written" | "present" | "failed" | "unavailable";

/** `<wiki folder>/assets`, or null when the wiki folder is not a valid vault folder. */
export function assetsFolder(wikiFolder: string): string | null {
  const v = validateNotesFolder(wikiFolder);
  return v.ok && wikiFolder.trim() !== "" ? `${v.folder}/assets` : null;
}

/** The asset ids of the embedded-html blocks in `markdown` whose src is `assets/<id>.html` (each once, in order). */
export function assetIdsIn(markdown: string): string[] {
  const ids: string[] = [];
  const fence = new RegExp(`^\`\`\`${EMBED_FENCE}[ \\t]*\\r?\\n([\\s\\S]*?)^\`\`\`[ \\t]*$`, "gm");
  for (const m of markdown.matchAll(fence)) {
    const b = parseEmbedBlock(m[1]!);
    const id = b === null ? null : assetIdOfSrc(b.src);
    if (id !== null && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

/** The asset ids of the `evoui` blocks in `markdown` that are `asset: <id>` references (each once, in order). */
export function evouiAssetIdsIn(markdown: string): string[] {
  const ids: string[] = [];
  const fence = new RegExp(`^\`\`\`${EVOUI_FENCE}[ \\t]*\\r?\\n([\\s\\S]*?)^\`\`\`[ \\t]*$`, "gm");
  for (const m of markdown.matchAll(fence)) {
    const src = parseEvouiSource(m[1]!);
    if (src.kind === "asset" && !ids.includes(src.id)) ids.push(src.id);
  }
  return ids;
}

/** The asset ids of the `evowiki-image` blocks in `markdown` (each once, in order). */
export function imageAssetIdsIn(markdown: string): string[] {
  const ids: string[] = [];
  const fence = new RegExp(`^\`\`\`${IMAGE_FENCE}[ \\t]*\\r?\\n([\\s\\S]*?)^\`\`\`[ \\t]*$`, "gm");
  for (const m of markdown.matchAll(fence)) {
    const b = parseImageBlock(m[1]!);
    if (b !== null && !ids.includes(b.asset)) ids.push(b.asset);
  }
  return ids;
}

/** html: an HTML interactive saved as `<id>.html`; evoui: an EvoUI block's JSON saved as `<id>.json`; image: `<id>.webp`. */
export type AssetKind = "html" | "evoui" | "image";
const EXT: Record<AssetKind, string> = { html: "html", evoui: "json", image: "webp" };

/** The bytes of a base64 WebP (RIFF....WEBP), or null when it is not one. */
export function webpBytes(b64: string): Uint8Array | null {
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(b64) || b64.length % 4 !== 0) return null;
  const bytes = Uint8Array.from(Buffer.from(b64, "base64"));
  const tag = (from: number): string => String.fromCharCode(...bytes.subarray(from, from + 4));
  return bytes.length > 12 && tag(0) === "RIFF" && tag(8) === "WEBP" ? bytes : null;
}

/** The vault side of the fetcher: the 7.1e ObsidianNoteVault (create rejects an existing path, so nothing is overwritten). */
export function obsidianAssetStore(app: App): AssetStore {
  return new ObsidianNoteVault(app.vault);
}

const sha256Hex = (text: string): string => createHash("sha256").update(text, "utf8").digest("hex");

export class AssetFetcher {
  private readonly inFlight = new Map<string, Promise<FetchResult>>();

  /**
   * `wikiFolder` is read per call (the notes folder setting); `onWritten` runs after a file was written (main.ts
   * redraws the blocks waiting for it).
   */
  constructor(private readonly api: () => AssetSource, private readonly store: AssetStore,
    private readonly wikiFolder: () => string, private readonly onWritten: (assetId: string) => void = () => undefined) {}

  /** Whether the server stored this content; a failing request rejects. */
  async isGenerated(text: string, kind: "evoui" | "html"): Promise<boolean> { return (await this.api().assetGenerated(text, kind)).generated; }

  /** The vault path an asset is written to; null for an invalid id or wiki folder. */
  pathFor(assetId: string, kind: AssetKind = "html"): string | null {
    const dir = assetsFolder(this.wikiFolder());
    return dir === null || !validAssetId(assetId) ? null : `${dir}/${assetId}.${EXT[kind]}`;
  }

  /** One asset; a second call for an id already being fetched waits for the first. */
  retrieve(assetId: string, kind: AssetKind = "html"): Promise<FetchResult> {
    const key = `${kind}:${assetId}`;
    const running = this.inFlight.get(key);
    if (running) return running;
    const p = this.run(assetId, kind).finally(() => { this.inFlight.delete(key); });
    this.inFlight.set(key, p);
    return p;
  }

  /** Every id, one after another; failures are already logged by fetch. */
  async retrieveAll(ids: readonly string[], kind: AssetKind = "html"): Promise<void> {
    for (const id of ids) await this.retrieve(id, kind);
  }

  private async run(assetId: string, kind: AssetKind): Promise<FetchResult> {
    const dir = assetsFolder(this.wikiFolder());
    const path = this.pathFor(assetId, kind);
    if (dir === null || path === null) {
      console.warn("EvoWiki: interactive not fetched (invalid id or wiki folder)", assetId);
      return "failed";
    }
    try {
      if (await this.store.occupied(path)) return "present";
      const api = this.api();
      const meta = await api.assetMeta(assetId);
      if (meta.asset_id !== assetId || typeof meta.sha256 !== "string" || !SHA256_HEX.test(meta.sha256)) {
        console.warn("EvoWiki: interactive not saved (unexpected meta)", assetId);
        return "failed";
      }
      // An older server sends no kind: that is html. A block pointing at the wrong kind of asset writes nothing.
      if ((meta.kind ?? "html") !== kind) {
        console.warn("EvoWiki: interactive not saved (wrong kind)", assetId);
        return "failed";
      }
      const html = await api.assetHtml(assetId);
      if (kind === "image") return await this.saveImage(assetId, dir, path, html, meta.sha256);
      if (typeof html !== "string" || new TextEncoder().encode(html).length > MAX_ASSET_BYTES) {
        console.warn("EvoWiki: interactive not saved (too large)", assetId);
        return "failed";
      }
      if (sha256Hex(html) !== meta.sha256) {
        console.warn("EvoWiki: interactive not saved (sha256 mismatch)", assetId);
        return "failed";
      }
      // Checked again right before writing: create never overwrites (it rejects an existing path).
      if (await this.store.occupied(path)) return "present";
      await this.store.ensureFolder(dir);
      await this.store.create(path, html);
    } catch (e) {
      if (e instanceof ApiError && e.status === 503) return "unavailable";
      console.warn("EvoWiki: interactive not fetched", assetId, e);
      return "failed";
    }
    this.onWritten(assetId);
    return "written";
  }

  private async saveImage(assetId: string, dir: string, path: string, b64: unknown, sha: string): Promise<FetchResult> {
    if (typeof b64 !== "string" || b64.length > MAX_IMAGE_B64) {
      console.warn("EvoWiki: image not saved (too large)", assetId);
      return "failed";
    }
    if (sha256Hex(b64) !== sha) {
      console.warn("EvoWiki: image not saved (sha256 mismatch)", assetId);
      return "failed";
    }
    const bytes = webpBytes(b64);
    if (bytes === null) {
      console.warn("EvoWiki: image not saved (not a WebP)", assetId);
      return "failed";
    }
    if (await this.store.occupied(path)) return "present";
    await this.store.ensureFolder(dir);
    await this.store.createBinary(path, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer);
    this.onWritten(assetId);
    return "written";
  }
}
