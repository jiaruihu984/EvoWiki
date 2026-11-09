/**
 * The `embedded-html` code block (an interactive in a note), exactly as deep/contracts.py embed_block writes it:
 *
 *     ```embedded-html
 *     type: embedded-html
 *     id: <interactive id>
 *     src: assets/<asset id>.html
 *     title: <title>
 *     height: <px>
 *     ```
 *
 * The same format is used by the user's own wiki (another plugin), so any note's block is drawn when it passes the
 * path rules. The HTML is read from the vault and shown only through `srcdoc` in an iframe sandboxed with
 * `allow-scripts` alone (an opaque origin: no access to Obsidian, its storage or `app://`; R8.2), with the backend's
 * asset CSP injected as the document's first element, inside a gate frame (gateSrcdoc). A file runs by itself only when the
 * server says it stored exactly this content (`port.isGenerated`); otherwise, and when the server cannot be asked, the
 * block shows its title and 运行这个交互, and one press runs it in this mount.
 */
import { onLanguageChange, uiLanguage } from "../../i18n";
import { h } from "../../ui/dom";
import { WIKI_COPY } from "../copy";
import { OFFER_AFTER_MS, runOffer } from "../run-offer";
import type { BlockEvent } from "../evoui/block-event";

export const EMBED_FENCE = "embedded-html";
/** deep/contracts.py: an interactive id. */
const INTERACTIVE_ID = /^[a-z0-9][a-z0-9-]{1,63}$/;
/** deep/store.py: an asset id (the file name the block's src points at). */
export const ASSET_ID = /^[A-Za-z0-9_-]{1,100}$/;
/** Windows device names: `assets/con.html` and the like misbehave there, so they are never an asset file name. */
const RESERVED_NAME = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i;
/** An asset id that is also a safe file name on every platform. */
export const validAssetId = (id: string): boolean => ASSET_ID.test(id) && !RESERVED_NAME.test(id);
const MIN_HEIGHT = 400;
const MAX_HEIGHT = 1600;
/** A height the interactive reports itself (frameHeightOf) is clamped to this range; the block's 400 floor does not apply. */
export const REPORTED_MIN_HEIGHT = 80;
export const REPORTED_MAX_HEIGHT = MAX_HEIGHT;

/** deep/render.py ASSET_CSP without its `sandbox allow-scripts` part (the iframe's sandbox attribute does that). */
export const EMBED_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
  + "img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'";

/**
 * The host theme variables handed to the interactive (their computed values; an opaque origin cannot read them). First
 * Obsidian's own names (kept for existing assets), then the approved demo's tokens as a wiki note (`.evowiki-page`)
 * defines them in the EXACT block of styles.css: surfaces / text / border, the monochrome accent, the data palette
 * (--viz-1 primary series and highlight, --viz-2 second series) and the mastery colours (--known right, --gap wrong,
 * --shaky caution). Outside a wiki note the demo tokens are not defined, read as "" and are left out, so an asset keeps
 * its own fallbacks.
 */
export const THEME_VARS = [
  "--background-primary", "--background-secondary", "--text-normal", "--text-muted", "--text-faint",
  "--interactive-accent", "--text-on-accent", "--background-modifier-border", "--font-text", "--font-monospace",
  "--bg-0", "--bg-1", "--bg-2", "--border", "--tx-1", "--tx-2", "--tx-3", "--accent", "--accent-tx",
  "--viz-1", "--viz-2", "--known", "--likely", "--shaky", "--gap", "--none", "--font-ui", "--font-mono",
] as const;

export interface EmbedBlock { id: string; src: string; title: string; height: number }

const KEYS = new Set(["type", "id", "src", "title", "height"]);

/**
 * One `key: value` per line (blank lines skipped, other keys ignored). Null — never a guessed default — when a line is
 * not `key: value`, a key repeats or is missing, `type` is not embedded-html, `id` is not an interactive id, `src`
 * fails safeSrc, or `height` is not an integer in 400–1600.
 */
export function parseEmbedBlock(source: string): EmbedBlock | null {
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
  const type = got.get("type"), id = got.get("id"), src = got.get("src"), title = got.get("title"), height = got.get("height");
  if (type !== EMBED_FENCE || id === undefined || src === undefined || title === undefined || height === undefined) return null;
  if (!INTERACTIVE_ID.test(id) || !safeSrc(src) || !/^[0-9]{1,5}$/.test(height)) return null;
  const px = Number(height);
  if (px < MIN_HEIGHT || px > MAX_HEIGHT) return null;
  return { id, src, title, height: px };
}

/**
 * A relative `.html` path inside the vault: no URL scheme, no leading `/`, no `..` anywhere, no backslash, no empty or
 * `.` segment, no query / fragment / percent-escape / control character.
 */
export function safeSrc(src: string): boolean {
  if (src.length === 0 || src.length > 300) return false;
  if (!/\.html$/i.test(src)) return false;
  if (src.includes("..") || src.startsWith("/") || /^[A-Za-z][A-Za-z0-9+.-]*:/.test(src)) return false;
  if (/[\\?#%:\u0000-\u001f\u007f]/.test(src)) return false;
  return src.split("/").every((seg) => seg !== "" && seg !== ".");
}

/** The vault path of `src` relative to the note at `notePath` (its folder); null when src is not safe. */
export function resolveSrc(src: string, notePath: string): string | null {
  if (!safeSrc(src)) return null;
  const slash = notePath.lastIndexOf("/");
  return slash < 0 ? src : `${notePath.slice(0, slash)}/${src}`;
}

/** The asset id when src is exactly `assets/<asset id>.html` (what EvoWiki writes), else null. */
export function assetIdOfSrc(src: string): string | null {
  const m = /^assets\/([^/]+)\.html$/.exec(src);
  return m && validAssetId(m[1]!) ? m[1]! : null;
}

/** Only a value that cannot leave the `<style>` rule it is put in (no markup, braces, semicolons or escapes). */
const safeCssValue = (v: string): boolean => v.length > 0 && v.length <= 300 && !/[<>{};\\@\u0000-\u001f]/.test(v);

/** `:root { --var: <computed value>; …; color-scheme: dark|light }` — values that are not safe are left out. */
const COMPONENT_TOKENS = [
  ["--evowiki-bg", "--background-primary"], ["--evowiki-bg-1", "--background-secondary"], ["--evowiki-bg-2", "--background-modifier-hover"],
  ["--evowiki-line", "--background-modifier-border"], ["--evowiki-tx", "--text-normal"], ["--evowiki-tx-2", "--text-muted"],
  ["--evowiki-tx-3", "--text-faint"], ["--evowiki-accent", "--interactive-accent"],
] as const;

export function themeStyle(read: (name: string) => string, dark: boolean): string {
  const decls: string[] = [];
  for (const name of THEME_VARS) {
    const v = read(name).trim();
    if (safeCssValue(v)) decls.push(`${name}: ${v};`);
  }
  // the library components' base tokens (deep/components/AUTHORING.md): without them they follow the OS, not the note
  for (const [token, from] of COMPONENT_TOKENS) {
    const v = read(from).trim();
    if (safeCssValue(v)) decls.push(`${token}: ${v};`);
  }
  const accent = read("--interactive-accent").trim(), bg = read("--background-primary").trim();
  if (safeCssValue(accent) && safeCssValue(bg)) decls.push(`--evowiki-accent-soft: color-mix(in srgb, ${accent} 18%, ${bg});`);
  decls.push(`color-scheme: ${dark ? "dark" : "light"};`);
  return `<style>:root { ${decls.join(" ")} }</style>`;
}

/**
 * The iframe's srcdoc: the CSP meta first, then the theme style, then the file. Prepending (instead of searching the
 * file for `<head>`) makes the parser open an implied `<head>` holding the meta and the style before any of the file
 * is parsed; the file's own doctype / `<html>` / `<head>` tags that follow are ignored parse errors, and an iframe
 * srcdoc document is never put into quirks mode for a missing doctype.
 */
export function buildSrcdoc(html: string, theme: string): string {
  return `<meta http-equiv="Content-Security-Policy" content="${EMBED_CSP}">${theme}${html}`;
}

/**
 * A frame may always navigate itself, and only its parent's CSP (`default-src 'none'` covers `frame-src`) stops the request;
 * the file cannot navigate the gate. So the file sits one level down. The gate's script hands the file's messages up (a file
 * says `parent.postMessage`, deep/components.py) and reports a second load of the file, which only a blocked navigation
 * makes: the gate itself reloading (a frame Obsidian moved in the DOM) starts the count over.
 */
const GATE_SCRIPT = "var f=document.querySelector('iframe'),n=0,locale=null;"
  + "f.addEventListener('load',function(){if(++n>1){f.remove();parent.postMessage({evowikiFrameNavigated:true},'*')}else if(locale)f.contentWindow.postMessage(locale,'*')});"
  + "addEventListener('message',function(e){if(e.source===f.contentWindow)parent.postMessage(e.data,'*');else if(e.source===parent&&e.data&&e.data.t==='locale'&&(e.data.locale==='zh'||e.data.locale==='en')){locale={t:'locale',locale:e.data.locale};f.contentWindow.postMessage(locale,'*')}})";
const attr = (s: string): string => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;");

export function gateSrcdoc(fileDoc: string): string {
  return `<meta http-equiv="Content-Security-Policy" content="${EMBED_CSP}"><style>html,body{margin:0;height:100%;overflow:hidden;background:transparent}iframe{display:block;width:100%;height:100%;border:0}</style>`
    + `<iframe sandbox="allow-scripts" referrerpolicy="no-referrer" srcdoc="${attr(fileDoc)}"></iframe><script>${GATE_SCRIPT}</script>`;
}

/** What a block reads and calls; main.ts binds it to the vault, the settings, the connection and the asset fetcher. */
export interface EmbedPort {
  /** The text of the vault file at `path`, or null when there is none. */
  read(path: string): Promise<string | null>;
  /** Server set, online and not the demo: the 取回 button may be shown. */
  canFetch(): boolean;
  /** Where a fetched asset lands (`<notes>/Wiki/assets/<id>.html`); the button is shown only when the block points there. */
  assetPath(assetId: string): string | null;
  /** Fetches one asset into the vault; true when the file is there afterwards. */
  retrieve(assetId: string): Promise<boolean>;
  /**
   * The computed theme variables at `at` (the block's element: main.ts reads them from the note's `.evowiki-page`
   * element around it, where the demo's tokens are defined) and whether the dark theme is on.
   */
  theme(at: HTMLElement): { read(name: string): string; dark: boolean };
  /** Whether the server stored a file with exactly this content; absent or failing = not known. */
  isGenerated?(html: string): Promise<boolean>;
  /** A graded component's result in a wiki note (componentItemOf), as a result event for the server to grade; absent = nobody listens. */
  onEvent?(assetId: string, ev: BlockEvent): void;
}

/** One mounted block as the hub sees it. */
export interface EmbedEntry {
  /** The host theme may have changed: redraw only if the values handed to the interactive differ. */
  themeChanged(): void;
  /** An asset file was written: redraw only if this block waits for that asset (a running interactive is left alone). */
  assetWritten(assetId: string): void;
}

/** The mounted blocks: told about a theme change (css-change) and about each asset written. */
export class EmbedHub {
  private readonly entries = new Set<EmbedEntry>();
  add(e: EmbedEntry): () => void { this.entries.add(e); return () => { this.entries.delete(e); }; }
  themeChanged(): void { for (const e of [...this.entries]) e.themeChanged(); }
  assetWritten(assetId: string): void { for (const e of [...this.entries]) e.assetWritten(assetId); }
}

const line = (text: string, cls = "evowiki-embed-hint"): HTMLElement => h("div", { class: cls }, text);

const currentTheme = (port: EmbedPort, at: HTMLElement): string => {
  const t = port.theme(at);
  return themeStyle((name) => t.read(name), t.dark);
};

/**
 * The height an interactive asks for: a message whose data is exactly `{ evowikiFrameHeight: <finite number> }` (a plain
 * object with that one key), clamped to REPORTED_MIN_HEIGHT–REPORTED_MAX_HEIGHT and rounded up; null for anything else.
 * The sender is checked by the caller (event.source), never here.
 */
export function frameHeightOf(data: unknown): number | null {
  if (typeof data !== "object" || data === null || Object.prototype.toString.call(data) !== "[object Object]") return null;
  const keys = Object.keys(data);
  if (keys.length !== 1 || keys[0] !== "evowikiFrameHeight") return null;
  const n = (data as { evowikiFrameHeight: unknown }).evowikiFrameHeight;
  if (typeof n !== "number" || !Number.isFinite(n)) return null;
  return Math.ceil(Math.min(REPORTED_MAX_HEIGHT, Math.max(REPORTED_MIN_HEIGHT, n)));
}

/** A graded library component's report: its config's item id, and where the reader put it (null: the answer was shown first). */
export interface ComponentItem { item: string; target: string | null }

const COMPONENT_ID_MAX = 64;
const componentId = (v: unknown): v is string => typeof v === "string" && v.length > 0 && v.length <= COMPONENT_ID_MAX;
const plainObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && Object.prototype.toString.call(v) === "[object Object]";

/**
 * A message whose data is exactly `{ evowikiComponentItem: { item, target } }` (deep/components/AUTHORING.md rule 11), else
 * null. Evidence goes no further than this shape: the server grades it against the asset's stored config and records one
 * result per item. The sender is checked by the caller (event.source), never here.
 */
export function componentItemOf(data: unknown): ComponentItem | null {
  if (!plainObject(data)) return null;
  const keys = Object.keys(data);
  if (keys.length !== 1 || keys[0] !== "evowikiComponentItem") return null;
  const v = data.evowikiComponentItem;
  if (!plainObject(v) || Object.keys(v).length !== 2) return null;
  const { item, target } = v;
  return componentId(item) && (target === null || componentId(target)) ? { item, target } : null;
}

/** The wire form of a component's report (BlockEventIn; wikiapp/block_results.py, kind `component`). */
export const componentEvent = (c: ComponentItem): BlockEvent =>
  ({ type: "item", node: "component", kind: "component", item: c.item, attempt: 1, response: c.target, revision: 0 });

/**
 * The sandboxed iframe (the gate; the file inside it, see gateSrcdoc).
 *
 * Never add a `message` listener for these frames without checking `event.source === frame.contentWindow` and the
 * message's shape: an opaque-origin frame's `event.origin` is "null", so the origin proves nothing. The one listener
 * there is (mountEmbedBlock) does both: only the shown frame's own window, only frameHeightOf's or componentItemOf's exact
 * shape, or the gate's report of a navigation. All it can do is set that frame's height within REPORTED_MIN/MAX_HEIGHT,
 * post an EvoWiki asset's result in a wiki note for the server to grade, or take that frame away.
 */
function frame(b: EmbedBlock, html: string, theme: string): HTMLIFrameElement {
  const f = document.createElement("iframe");
  f.className = "evowiki-embed-frame";
  // allow-scripts only: never allow-same-origin (it would hand the interactive Obsidian's origin), nor any other token.
  f.setAttribute("sandbox", "allow-scripts");
  f.setAttribute("referrerpolicy", "no-referrer");
  f.setAttribute("loading", "lazy");
  f.setAttribute("title", b.title);
  f.setAttribute("height", String(b.height));
  f.style.width = "100%";
  f.style.height = `${b.height}px`;
  f.setAttribute("srcdoc", gateSrcdoc(buildSrcdoc(html, theme)));
  return f;
}

/**
 * Draws the block into `el` (title + sandboxed iframe, a 「取回」 placeholder, or one plain line) and redraws it through
 * `hub`; returns the cleanup. `notePath` is the vault path of the note holding the block (src is relative to its folder).
 */
export function mountEmbedBlock(el: HTMLElement, source: string, notePath: string, port: EmbedPort, hub: EmbedHub): () => void {
  const root = h("div", { class: "evowiki-embed" });
  el.replaceChildren(root);
  const b = parseEmbedBlock(source);
  const assetId = b === null ? null : assetIdOfSrc(b.src);
  // an EvoWiki asset (`assets/<id>.html`) in a wiki note lives in the wiki's one assets folder, also for a note in a
  // topic folder and for a draft rendered without a note path; any other src (and a note outside the wiki) is
  // relative to the note's folder
  const wikiAsset = assetId === null ? null : port.assetPath(assetId);
  const wikiDir = wikiAsset === null ? null : wikiAsset.slice(0, wikiAsset.length - b!.src.length);
  const inWiki = wikiDir !== null && (notePath === "" || notePath.startsWith(wikiDir));
  const path = b === null ? null : inWiki ? wikiAsset : resolveSrc(b.src, notePath);
  if (b === null || path === null) {
    root.replaceChildren(line(WIKI_COPY.embedUnreadable));
    return () => undefined;
  }
  let gen = 0;
  let fetching = false;
  let triedFetch = false;
  let failed = false;
  let stopped = false;
  /** What is shown: nothing yet, the 还没取回 placeholder, or the frame (with the theme it got). */
  let shown: { kind: "none" | "missing" } | { kind: "frame"; theme: string } = { kind: "none" };
  /** A read is in flight (its answer may predate a write that lands meanwhile). */
  let reading = false;
  /** The file the server confirmed or the reader chose to run, in this mount only: a redraw does not ask again. */
  let cleared: string | null = null;
  /**
   * The opt-in height handshake: an asset may `parent.postMessage({ evowikiFrameHeight: n }, "*")`. One window listener
   * while a frame is shown (on the window holding the block); a message counts only when it comes from the shown
   * frame's own window and has frameHeightOf's exact shape. Removed with the frame's redraw and on cleanup.
   */
  let shownFrame: HTMLIFrameElement | null = null;
  let stopListening: (() => void) | null = null;
  const listen = (f: HTMLIFrameElement): void => {
    stopListening?.();
    shownFrame = f;
    const win = el.ownerDocument.defaultView ?? window;
    const onMessage = (ev: MessageEvent): void => {
      const cur = shownFrame;
      if (stopped || cur === null || ev.source === null || ev.source !== cur.contentWindow || !root.contains(cur)) return;
      if (plainObject(ev.data) && ev.data.evowikiFrameNavigated === true) {
        stopListening?.();
        shown = { kind: "none" };
        root.replaceChildren(line(WIKI_COPY.embedPaused));
        return;
      }
      const px = frameHeightOf(ev.data);
      if (px !== null) {
        cur.setAttribute("height", String(px));
        cur.style.height = `${String(px)}px`;
        return;
      }
      // Only a saved wiki note: a draft or a chat preview (no note path) is not an answer.
      const answer = assetId !== null && inWiki && notePath !== "" ? componentItemOf(ev.data) : null;
      if (answer !== null) port.onEvent?.(assetId!, componentEvent(answer));
    };
    win.addEventListener("message", onMessage);
    stopListening = () => { win.removeEventListener("message", onMessage); stopListening = null; shownFrame = null; };
  };
  const head = (): HTMLElement => h("div", { class: "evowiki-embed-title" }, b.title);
  const missing = (): HTMLElement => {
    const target = assetId === null ? null : port.assetPath(assetId);
    const button = assetId !== null && target === path && port.canFetch()
      ? h("button", { class: "evowiki-embed-fetch", disabled: fetching, onclick: () => void fetchNow(assetId) }, () => (fetching ? WIKI_COPY.embedFetching : WIKI_COPY.embedFetch))
      : null;
    return h("div", { class: "evowiki-embed-missing" }, line(WIKI_COPY.embedMissing), button, failed ? line(WIKI_COPY.embedFetchFailed, "evowiki-embed-error") : null);
  };
  const draw = (): void => {
    const mine = ++gen;
    reading = true;
    void port.read(path).then((html) => {
      if (stopped || mine !== gen) return;
      reading = false;
      if (html === null) {
        stopListening?.(); shown = { kind: "missing" }; root.replaceChildren(head(), missing());
        // Fetched once by itself (a note not synced yet, a review answer): 取回 stays only for a failure.
        if (assetId !== null && port.assetPath(assetId) === path && port.canFetch() && !triedFetch) { triedFetch = true; void fetchNow(assetId); }
        return;
      }
      const show = (): void => {
        cleared = html;
        const theme = currentTheme(port, root);
        shown = { kind: "frame", theme };
        const f = frame(b, html, theme);
        root.replaceChildren(f);
        listen(f);
        if (html.includes("data-evowiki-locale")) {
          const sendLocale = (): void => { f.contentWindow?.postMessage({ t: "locale", locale: uiLanguage() }, "*"); };
          f.addEventListener("load", sendLocale); sendLocale();
          const stopLocale = onLanguageChange(sendLocale); const stop = stopListening;
          stopListening = () => { stopLocale(); f.removeEventListener("load", sendLocale); stop?.(); };
        }
      };
      if (cleared === html) { show(); return; }
      stopListening?.();
      shown = { kind: "none" };
      root.replaceChildren(head());       // while the server is asked: nothing running
      let pressed = false;
      const run = (): void => { if (!pressed && !stopped && mine === gen) { pressed = true; show(); } };
      // a server just started or gone answers in seconds: the offer does not wait for it
      const offer = window.setTimeout(() => { if (!pressed && !stopped && mine === gen) root.replaceChildren(head(), runOffer(run)); }, OFFER_AFTER_MS);
      void (port.isGenerated?.(html) ?? Promise.resolve(false)).catch((e: unknown) => {
        console.warn("EvoWiki: could not ask where an interactive came from", e);
        return false;
      }).then((generated) => {
        window.clearTimeout(offer);
        if (stopped || mine !== gen || pressed) return;
        if (generated) run();
        else root.replaceChildren(head(), runOffer(run));
      });
    }, (e: unknown) => {
      if (stopped || mine !== gen) return;
      reading = false;
      console.warn("EvoWiki: could not read an interactive", e);
      stopListening?.();
      shown = { kind: "missing" };
      root.replaceChildren(head(), missing());
    });
  };
  const fetchNow = async (id: string): Promise<void> => {
    if (fetching) return;
    fetching = true; failed = false;
    draw();
    let got = false;
    try { got = await port.retrieve(id); } catch (e) { console.warn("EvoWiki: could not fetch an interactive", e); }
    fetching = false; failed = !got;
    if (!stopped) draw();
  };
  draw();
  const off = hub.add({
    themeChanged: () => { if (shown.kind === "frame" && shown.theme !== currentTheme(port, root)) draw(); },
    // Also while a read is in flight: it may have looked before the file was written, so a fresh read supersedes it.
    assetWritten: (id) => { if (id === assetId && !fetching && (shown.kind === "missing" || reading)) draw(); },
  });
  return () => { stopped = true; stopListening?.(); off(); };
}
