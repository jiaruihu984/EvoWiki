/**
 * Inside the sandboxed frame: takes a block from the main window, validates it and draws it with D1's renderer. Dragging a
 * slider is evaluated here and never crosses the channel; only the frame's height, the result events and the debounced
 * progress go back.
 */
import type { BlockEvent, BlockProgressData } from "../block-event";
import type { FromFrame, FrameTheme, ToFrame } from "../protocol";
import { evoEngine, type EvoUIHandle } from "./engine";
import { validateBlock } from "./validate";

// The host sizes the frame to its content: a scrollbar here only ever narrows it.
const FRAMED = "html,body{margin:0;padding:0;background:transparent}html{scrollbar-width:none}";

function applyTheme(doc: Document, mount: HTMLElement, theme: FrameTheme): void {
  let style = doc.getElementById("evoui-theme");
  if (style === null) { style = doc.createElement("style"); style.id = "evoui-theme"; doc.head.append(style); }
  const vars = Object.entries(theme.vars).map(([k, v]) => `${k}:${v};`).join("");
  style.textContent = `:root{${vars}}\n${theme.css}\n${FRAMED}`;
  doc.body.classList.toggle("evowiki-page", theme.page);
  doc.body.classList.toggle("theme-dark", theme.dark);
  doc.body.classList.toggle("theme-light", !theme.dark);
  // On the mount, not the root: a root whose color-scheme differs from the embedding element paints an opaque backdrop.
  mount.style.colorScheme = theme.dark ? "dark" : "light";
  Object.assign(mount.style, { fontFamily: theme.font.family, fontSize: theme.font.size, lineHeight: theme.font.lineHeight, color: theme.font.color });
}

/** Starts the frame's side of the channel in `mount`; returns what handles the main window's messages. */
export function startFrame(win: Window, mount: HTMLElement, post: (m: FromFrame) => void): (m: ToFrame) => void {
  const doc = mount.ownerDocument;
  const slots = new Map<number, HTMLElement>();
  const images = new Map<number, (bytes: ArrayBuffer | null) => void>();
  let nextMath = 0;
  let nextImage = 0;
  let lastHeight = -1;
  let handle: EvoUIHandle | null = null;
  let paintRaf: number | null = null;
  let pendingPaint: (() => void) | null = null;
  let observer: ResizeObserver | null = null;
  let stopped = false;
  let initialized = false;

  const math = (tex: string, display: boolean): HTMLElement => {
    const slot = doc.createElement("span");
    slot.className = "evowiki-math-slot";
    const source = doc.createElement("code");
    source.textContent = tex;
    slot.append(source);
    const id = nextMath++;
    slots.set(id, slot);
    post({ t: "math", id, tex, display });
    return slot;
  };

  const reportHeight = (): void => {
    const h = Math.ceil(mount.getBoundingClientRect().height);
    if (h !== lastHeight) { lastHeight = h; post({ t: "height", h }); }
  };

  const draw = (block: unknown, blockId: string | null, progress: BlockProgressData | null, language: "zh" | "en"): void => {
    const v = validateBlock(block);
    const engine = evoEngine();
    if (!v.ok || engine === null) { post({ t: "refused", why: v.ok ? "renderer missing" : v.errors.join("; ") }); return; }
    const host = doc.createElement("div");
    host.className = "evowiki-evoui-host";
    mount.replaceChildren(host);
    try {
      handle = engine.render(v.block, host, {
        language,
        blockId,
        onEvent: (ev: BlockEvent) => { post({ t: "event", ev }); },
        onProgress: (p: BlockProgressData) => { post({ t: "progress", progress: p }); },
        // A removed browsing context cannot deliver a deferred teardown request. Hand state off in the input task;
        // only expensive painting waits for the next animation frame.
        immediateProgress: true,
        scheduleUpdate: (paint) => {
          pendingPaint = paint;
          if (paintRaf !== null) return;
          paintRaf = win.requestAnimationFrame(() => {
            paintRaf = null;
            const update = pendingPaint;
            pendingPaint = null;
            if (!stopped) update?.();
          });
        },
        renderMath: math,
        image: (im: { asset?: string }) => new Promise<{ mime: string; bytes: ArrayBuffer } | null>((resolve) => {
          if (im.asset === undefined) { resolve(null); return; }
          const id = nextImage++;
          images.set(id, (bytes) => { resolve(bytes === null ? null : { mime: "image/webp", bytes }); });
          post({ t: "image", id, asset: im.asset });
        }),
        ...(progress === null ? {} : { progress }),
      });
    } catch (e) {
      post({ t: "refused", why: e instanceof Error ? e.message : String(e) });
      return;
    }
    const RO = (win as unknown as { ResizeObserver?: typeof ResizeObserver }).ResizeObserver;
    if (RO !== undefined) { observer = new RO(reportHeight); observer.observe(mount); }
    reportHeight();
    post({ t: "rendered" });
  };

  const stop = (): void => {
    if (stopped) return;
    if (paintRaf !== null) win.cancelAnimationFrame(paintRaf);
    paintRaf = null; pendingPaint = null;
    observer?.disconnect();
    const progress = handle?.destroy() ?? null;
    stopped = true;
    win.removeEventListener("pagehide", stop);
    for (const resolve of images.values()) resolve(null);
    images.clear(); slots.clear();
    post({ t: "stopped", progress });
  };
  win.addEventListener("pagehide", stop, { once: true });
  return (m) => {
    if (stopped) return;
    switch (m.t) {
      case "stop": stop(); break;
      case "init":
        if (initialized) return;
        initialized = true;
        applyTheme(doc, mount, m.theme);
        draw(m.block, m.blockId, m.progress, m.locale);
        break;
      case "theme":
        applyTheme(doc, mount, m.theme);
        break;
      case "locale": handle?.setLanguage(m.locale); break;
      case "ping":
        post({ t: "pong", n: m.n });
        break;
      case "image":
        images.get(m.id)?.(m.bytes);
        images.delete(m.id);
        break;
      case "math": {
        const slot = slots.get(m.id);
        slots.delete(m.id);
        if (m.css !== null) {
          let style = doc.getElementById("evoui-mathjax");
          if (style === null) { style = doc.createElement("style"); style.id = "evoui-mathjax"; doc.head.append(style); }
          style.textContent = m.css;
        }
        if (slot !== undefined && m.html !== null) {
          const typeset = new DOMParser().parseFromString(m.html, "text/html").body;
          slot.replaceChildren(...Array.from(typeset.childNodes));
        }
        break;
      }
    }
  };
}
