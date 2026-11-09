/**
 * The `evowiki-image` code block (an openly licensed image the page writer placed), exactly as deep/contracts.py
 * image_fence writes it:
 *
 *     ```evowiki-image
 *     asset: <asset id>
 *     alt: <what the image shows>
 *     title: <the file's title>
 *     author: <author>              (optional)
 *     license: <CC BY-SA 4.0 | CC0 | Public domain | …>
 *     license_url: <url>            (optional)
 *     source: <Wikimedia Commons | Openverse>
 *     source_url: <the file's page>
 *     width: <px>
 *     height: <px>
 *     ```
 *
 * The image is the vault file `<notes>/Wiki/assets/<asset id>.webp` (fetched from the backend as an asset, never from
 * the web: nothing remote is ever loaded). Under it one line of small grey text: title · author · licence · source,
 * the licence and the source as links to their pages. A missing file shows one plain line and 「取回」.
 */
import { h } from "../../ui/dom";
import { WIKI_COPY } from "../copy";
import { type EmbedHub, validAssetId } from "./embed-block";

export const IMAGE_FENCE = "evowiki-image";

export interface ImageBlock {
  asset: string; alt: string; title: string; author: string; license: string; licenseUrl: string;
  source: string; sourceUrl: string; width: number; height: number;
}

const KEYS = new Set(["asset", "alt", "title", "author", "license", "license_url", "source", "source_url", "width", "height"]);

/** An http(s) URL without whitespace or control characters; "" for anything else (shown as plain text then). */
export function safeUrl(u: string): string {
  return /^https?:\/\/[^\s\u0000-\u001f\u007f"'<>]+$/i.test(u) && u.length <= 500 ? u : "";
}

/**
 * One `key: value` per line (blank lines skipped, other keys ignored). Null when a line is not `key: value`, a key
 * repeats, `asset` is not a valid asset id, or alt / title / license / source / source_url / width / height is missing.
 */
export function parseImageBlock(source: string): ImageBlock | null {
  const got = new Map<string, string>();
  for (const raw of source.split(/\r\n?|\n/)) {
    if (raw.trim() === "") continue;
    const m = /^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:(.*)$/.exec(raw);
    if (!m) return null;
    const key = m[1]!;
    if (!KEYS.has(key)) continue;
    if (got.has(key)) return null;
    got.set(key, m[2]!.trim());
  }
  const v = (k: string): string => got.get(k) ?? "";
  const width = Number(v("width")), height = Number(v("height"));
  if (!validAssetId(v("asset")) || !v("alt") || !v("title") || !v("license") || !v("source") || !safeUrl(v("source_url"))
    || !Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1) return null;
  return { asset: v("asset"), alt: v("alt"), title: v("title"), author: v("author"), license: v("license"),
    licenseUrl: safeUrl(v("license_url")), source: v("source"), sourceUrl: v("source_url"), width, height };
}

/** What a block reads and calls; main.ts binds it to the vault, the settings, the connection and the asset fetcher. */
export interface ImagePort {
  /** The vault file's resource URL (app://…) when `path` exists, else null. */
  resource(path: string): string | null;
  canFetch(): boolean;
  /** Where the image lands (`<notes>/Wiki/assets/<id>.webp`); null for an invalid id or notes folder. */
  assetPath(assetId: string): string | null;
  /** Fetches the image into the vault; true when the file is there afterwards. */
  retrieve(assetId: string): Promise<boolean>;
}

const link = (text: string, href: string): HTMLElement | string =>
  href ? h("a", { href, class: "evowiki-figure-link", target: "_blank", rel: "noopener noreferrer" }, text) : text;

/** The attribution line: title · author · licence · source (TASL), the licence and the source linked. */
export function captionEl(b: ImageBlock): HTMLElement {
  const parts: (HTMLElement | string)[] = [b.title, ...(b.author ? [b.author] : []), link(b.license, b.licenseUrl),
    link(b.source, b.sourceUrl)];
  const cap = h("figcaption", { class: "evowiki-figure-caption" });
  parts.forEach((p, i) => { if (i > 0) cap.append(" · "); cap.append(p); });
  return cap;
}

/** Draws the block into `el` and redraws it when its image is written (through `hub`); returns the cleanup. */
export function mountImageBlock(el: HTMLElement, source: string, port: ImagePort, hub: EmbedHub): () => void {
  const root = h("figure", { class: "evowiki-figure" });
  el.replaceChildren(root);
  const b = parseImageBlock(source);
  if (b === null) {
    root.replaceChildren(h("div", { class: "evowiki-embed-hint" }, () => (WIKI_COPY.imageUnreadable)));
    return () => undefined;
  }
  let fetching = false, failed = false, stopped = false, shown = false;
  const draw = (): void => {
    const path = port.assetPath(b.asset);
    const src = path === null ? null : port.resource(path);
    if (src !== null) {
      shown = true;
      const img = h("img", { class: "evowiki-figure-img", src, alt: b.alt, loading: "lazy", decoding: "async",
        width: String(b.width), height: String(b.height) });
      root.replaceChildren(img, captionEl(b));
      return;
    }
    const button = path !== null && port.canFetch()
      ? h("button", { class: "evowiki-embed-fetch", disabled: fetching, onclick: () => void fetchNow() }, () => (fetching ? WIKI_COPY.embedFetching : WIKI_COPY.embedFetch))
      : null;
    root.replaceChildren(h("div", { class: "evowiki-embed-missing" }, h("div", { class: "evowiki-embed-hint" }, () => (WIKI_COPY.imageMissing)), button, failed ? h("div", { class: "evowiki-embed-error" }, () => (WIKI_COPY.embedFetchFailed)) : null), captionEl(b));
  };
  const fetchNow = async (): Promise<void> => {
    if (fetching) return;
    fetching = true; failed = false;
    draw();
    let got = false;
    try { got = await port.retrieve(b.asset); } catch (e) { console.warn("EvoWiki: could not fetch an image", e); }
    fetching = false; failed = !got;
    if (!stopped) draw();
  };
  draw();
  // Fetched once by itself (a note not synced yet, a review answer): 取回 stays only for a failure.
  if (!shown && port.assetPath(b.asset) !== null && port.canFetch()) void fetchNow();
  const off = hub.add({ themeChanged: () => undefined,
    assetWritten: (id) => { if (id === b.asset && !fetching && !shown) draw(); } });
  return () => { stopped = true; off(); };
}
