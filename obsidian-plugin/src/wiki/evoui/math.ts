/**
 * A `math` node's LaTeX goes through Obsidian's own MathJax (`loadMathJax` / `renderMath` / `finishRenderMath`, shipped with
 * the app), never a renderer of ours. It runs in the main window, as it does for any `$…$` in any note; the frame gets the
 * typeset HTML and the styles. Fonts travel inside the styles: a frame with an opaque origin cannot load them from app://
 * (measured: NetworkError). When MathJax cannot load or a formula fails (MathJax answers bad TeX with an <mjx-merror> box),
 * the answer is null and the frame keeps the source: a formula never breaks the block.
 */
import { finishRenderMath, loadMathJax, renderMath } from "obsidian";

let ready: Promise<void> | null = null;
const fonts = new Map<string, string>();
let styles: { source: string; inlined: string } | null = null;

function mathJax(): Promise<void> {
  ready ??= loadMathJax().catch((e: unknown) => { ready = null; throw e; });   // a failed load is tried again next time
  return ready;
}

async function fontData(url: string): Promise<string> {
  const known = fonts.get(url);
  if (known !== undefined) return known;
  // eslint-disable-next-line no-restricted-globals -- a font Obsidian ships at its own app:// origin; requestUrl takes only http(s)
  const res = await fetch(url);
  const bytes = new Uint8Array(await res.arrayBuffer());
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  const data = `data:font/woff2;base64,${btoa(bin)}`;
  fonts.set(url, data);
  return data;
}

/** MathJax's stylesheets as they stand now, every font inline. */
async function mathStyles(doc: Document): Promise<string> {
  const source = Array.from(doc.querySelectorAll<HTMLStyleElement>('style[id^="MJX"]')).map((s) => s.textContent).join("\n");
  if (styles?.source === source) return styles.inlined;
  let inlined = source;
  for (const url of new Set(Array.from(source.matchAll(/url\("?([^")]+\.woff2)"?\)/g), (m) => m[1]!))) {
    inlined = inlined.split(url).join(await fontData(url));
  }
  styles = { source, inlined };
  return inlined;
}

export interface Typeset { html: string; css: string }

/** `tex` typeset by Obsidian's MathJax (display = its own line), or null. Never throws. */
export async function typeset(tex: string, display: boolean, doc: Document): Promise<Typeset | null> {
  try {
    await mathJax();
    const el = renderMath(tex, display);
    if (el.querySelector("mjx-merror")) { console.warn("EvoWiki: MathJax could not read a formula; its LaTeX is shown", tex); return null; }
    await finishRenderMath();
    return { html: el.outerHTML, css: await mathStyles(doc) };
  } catch (e) {
    console.warn("EvoWiki: a formula could not be typeset; its LaTeX is shown", e);
    return null;
  }
}

/** Tests only: forget a loaded MathJax, so the next formula loads it again. */
export function resetMathForTests(): void {
  ready = null;
  styles = null;
}
