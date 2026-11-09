/**
 * The main window's side of an EvoUI block: the isolation boundary. A block (from any note, synced or imported, so nothing
 * here assumes the server checked it) runs in two nested frames, both `sandbox="allow-scripts"` under a CSP with no network:
 *
 *   main window > gate frame (no script of its own) > block frame (frame/entry.ts)
 *
 * The block frame has an opaque origin and no `require`, `app`, `parent.document`, storage or `eval`. The gate exists because
 * a frame may always navigate itself: only its parent's `frame-src` stops that request, and the block cannot navigate the gate.
 * A block with an `app` node gets `app`'s own CSP (a nested srcdoc inherits its parent's policy, so a stricter one would stop
 * the app); it still has no network beyond the app's CDNs and the same containment.
 *
 * Chromium keeps these frames off the main thread (measured on Obsidian 1.13.7: a 3 s busy loop in one moves the main thread's
 * timers by ~20 ms), but they share one process, so a hung block stops all of them. The watchdog removes what stopped
 * answering, shows each block's fallback, waits for a new process (Chromium drops the hung one ~10 s after its last frame is
 * gone) and restarts the blocks one at a time. A block involved in two hangs stays text for this session.
 */
import evouiFrame from "virtual:evoui-frame";
import type { BlockEvent, BlockProgressData } from "./block-event";
import { frameTheme } from "./frame-theme";
import { onLanguageChange, uiLanguage } from "../../i18n";
import { typeset } from "./math";
import { type FromFrame, readFromFrame, type ToFrame } from "./protocol";

const SANDBOX = "allow-scripts";
const NO_NETWORK = "connect-src 'none'; frame-src 'none'; worker-src 'none'; object-src 'none'; manifest-src 'none'; form-action 'none'; base-uri 'none'";
export const STRICT_CSP = `default-src 'none'; script-src '${evouiFrame.hash}'; style-src 'unsafe-inline'; img-src data:; font-src data:; ${NO_NETWORK}`;
const APP_CDNS = "https://cdn.jsdelivr.net/ https://unpkg.com/ https://cdnjs.cloudflare.com/";
/** evoui.js APP.csp's allowances (frame-host.test.ts compares them); the frame's own script runs through 'unsafe-inline'. */
export const APP_CSP = `default-src 'none'; script-src 'unsafe-inline' ${APP_CDNS}; style-src 'unsafe-inline' ${APP_CDNS}; img-src data: blob:; font-src data: ${APP_CDNS}; media-src data: blob:; ${NO_NETWORK}`;

const HANG_MS = 5_000;
const TICK_MS = 1_000;
const PROBE_MS = 1_000;
const STRIKES_TO_TEXT = 2;
const MESSAGE_WINDOW_MS = 10_000;
const MAX_MESSAGES_PER_WINDOW = 3_000;
const MAX_MATH_REQUESTS = 300;
const MAX_EVENTS = 2_000;
const FIRST_HEIGHT = 160;

const attr = (s: string): string => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
const csp = (policy: string): string => `<meta http-equiv="Content-Security-Policy" content="${attr(policy)}">`;

/** The two nested documents of a block. */
export function gateDocument(policy: string, code: string): string {
  const block = `<!doctype html><html><head>${csp(policy)}</head><body><div id="mount" class="evowiki-evoui evowiki-evoui-framed"></div><script>${code}</script></body></html>`;
  return `<!doctype html><html><head>${csp(policy)}<style>html,body{margin:0;height:100%;overflow:hidden;background:transparent}iframe{display:block;width:100%;height:100%;border:0}</style></head>`
    + `<body><iframe sandbox="${SANDBOX}" allow="" referrerpolicy="no-referrer" srcdoc="${attr(block)}"></iframe></body></html>`;
}
const gates = new Map<string, string>();
const gateFor = (policy: string): string => {
  let g = gates.get(policy);
  if (g === undefined) { g = gateDocument(policy, evouiFrame.code); gates.set(policy, g); }
  return g;
};

/** The assets of the images an app in the block declares: the only ones its frame may ask for. */
export function declaredImages(v: unknown, out: Set<string> = new Set()): Set<string> {
  if (Array.isArray(v)) { for (const x of v) declaredImages(x, out); return out; }
  if (typeof v !== "object" || v === null) return out;
  const n = v as { kind?: unknown; images?: unknown };
  if ((n.kind === "app" || n.kind === "svg") && Array.isArray(n.images)) {
    for (const im of n.images as { asset?: unknown }[]) if (typeof im.asset === "string") out.add(im.asset);
  }
  for (const x of Object.values(v)) declaredImages(x, out);
  return out;
}

/** Whether anything in the block is an `app` node. A block that hides one only starves the app of its CSP: it fails closed. */
export function hasAppNode(v: unknown): boolean {
  if (Array.isArray(v)) return v.some(hasAppNode);
  if (typeof v !== "object" || v === null) return false;
  if ((v as { kind?: unknown }).kind === "app") return true;
  return Object.values(v).some(hasAppNode);
}

export interface FrameTransport {
  post(m: ToFrame): void;
  remove(): void;
  connected(): boolean;
  setHeight(h: number): void;
  /** Called once the session listens; the frame's `up` arrives after it. */
  start(): void;
}
export type MakeTransport = (host: HTMLElement, receive: (m: FromFrame) => void, policy: string, height: number) => FrameTransport;

/** The frame inside the gate; a missing index on a cross-origin window throws, so the length is read first. */
const childOf = (f: HTMLIFrameElement): Window | null => {
  const w = f.contentWindow;
  return w !== null && w.length > 0 ? w.frames[0] ?? null : null;
};

const sandboxTransport: MakeTransport = (host, receive, policy, height) => {
  const win = host.ownerDocument.defaultView ?? window;
  const f = host.ownerDocument.createElement("iframe");
  f.className = "evowiki-evoui-frame";
  f.setAttribute("sandbox", SANDBOX);
  f.setAttribute("allow", "");
  f.setAttribute("referrerpolicy", "no-referrer");
  f.style.height = `${height}px`;
  const inner = (): Window | null => childOf(f);
  let peer: Window | null = null;
  const onMessage = (e: MessageEvent): void => {
    peer = inner() ?? peer;
    if (e.source === null || e.source !== peer) return;
    const m = readFromFrame(e.data);
    if (m === null) { console.warn("EvoWiki: dropped a message from an evoui frame"); return; }
    receive(m);
  };
  return {
    post: (m) => { inner()?.postMessage(m, "*"); },
    remove: () => { win.removeEventListener("message", onMessage); f.remove(); },
    connected: () => f.isConnected,
    setHeight: (h) => { f.style.height = `${h}px`; },
    start: () => {
      win.addEventListener("message", onMessage);
      f.srcdoc = gateFor(policy);
      host.append(f);
    },
  };
};
let makeTransport: MakeTransport = sandboxTransport;
/** Tests only: the runtime without an iframe (frame/inline-transport.ts); such a session has no watchdog. */
export function useTransportForTests(t: MakeTransport | null): void { makeTransport = t ?? sandboxTransport; }

export interface FrameOptions {
  host: HTMLElement;
  /** The block's `.evowiki-evoui` box in the note: where theme values are read. */
  root: HTMLElement;
  /** The fence text: remembers height and hangs for this block across mounts. */
  source: string;
  block: unknown;
  blockId: string | null;
  progress: BlockProgressData | null;
  onEvent(ev: BlockEvent): void;
  onProgress(p: BlockProgressData): void;
  /** The bytes of an image asset the block declares; absent or null = the app's `evowiki.image` rejects. */
  image?(asset: string): Promise<ArrayBuffer | null>;
  /** The frame is about to load (first, or again after a hang): `host` must be in the document for it to run. */
  onStart(): void;
  /** The block cannot be shown (refused, hung, flooding): show its fallback. */
  onFallback(why: string): void;
}
export interface BlockFrame { retheme(): void; stop(): Promise<void> }

const heights = new Map<string, number>();
const strikes = new Map<string, number>();

class Session implements BlockFrame {
  private offLanguage: (() => void) | null = null;
  private transport: FrameTransport | null = null;
  private latest: BlockProgressData | null;
  private lastCss: string | null = null;
  private stopped = false;
  private stopping: Promise<void> | null = null;
  private stoppedResolve: (() => void) | null = null;
  private stopTimer: ReturnType<typeof setTimeout> | null = null;
  private initialized = false;
  private boot: string | null = null;
  private generation = 0;
  private messages = 0;
  private windowStart = 0;
  private mathRequests = 0;
  private events = 0;
  lastPong = 0;
  wasConnected = true;

  constructor(private readonly o: FrameOptions) { this.latest = o.progress; }

  get running(): boolean { return this.transport !== null; }
  get connected(): boolean { return this.transport?.connected() ?? false; }

  start(): void {
    if (this.stopped) return;
    this.offLanguage ??= onLanguageChange(() => { if (this.initialized) this.post({ t: "locale", locale: uiLanguage() }); });
    if ((strikes.get(this.o.source) ?? 0) >= STRIKES_TO_TEXT) { this.end("this block stopped responding twice"); return; }
    this.lastPong = Date.now();
    this.initialized = false; this.boot = null;
    const generation = ++this.generation;
    this.wasConnected = true;
    this.lastCss = null;
    this.o.onStart();
    this.transport = makeTransport(this.o.host, (m) => { if (this.generation === generation) this.receive(m); }, hasAppNode(this.o.block) ? APP_CSP : STRICT_CSP,
      heights.get(this.o.source) ?? FIRST_HEIGHT);
    this.transport.start();
  }

  post(m: ToFrame): void { this.transport?.post(m); }

  /** Terminal: the frame goes and the fallback stays. */
  private end(why: string): void {
    this.offLanguage?.(); this.offLanguage = null;
    this.halt();
    if (!this.stopped) this.o.onFallback(why);
    watchdog.forget(this);
  }

  /** Removes the frame; the session can start again. */
  private halt(): void {
    this.generation++; this.transport?.remove(); this.transport = null;
    this.stoppedResolve?.(); this.stoppedResolve = null;
  }

  get source(): string { return this.o.source; }
  showHung(): void { this.halt(); if (!this.stopped) this.o.onFallback("the block stopped responding"); }

  private receive(m: FromFrame): void {
    // A snapshot posted before the parent's close can still be queued in another renderer process.
    // Drain only this closing transport's newer snapshots; its final acknowledgment/timeout revokes the generation.
    if (this.stopped && m.t === "progress" && this.transport !== null) {
      if (this.latest === null || m.progress.revision > this.latest.revision) {
        this.latest = m.progress;
        if (this.o.blockId !== null) this.o.onProgress(m.progress);
      }
      return;
    }
    if (this.stopped && m.t === "stopped" && this.transport !== null) {
      if (this.stopTimer !== null) { clearTimeout(this.stopTimer); this.stopTimer = null; }
      try {
        if (m.progress !== null && this.o.blockId !== null && (this.latest === null || m.progress.revision > this.latest.revision)) {
          this.latest = m.progress;
          this.o.onProgress(m.progress);
        }
      }
      finally { this.halt(); }
      return;
    }
    if (this.stopped || this.transport === null || (!this.initialized && m.t !== "up")) return;
    const now = Date.now();
    if (now - this.windowStart > MESSAGE_WINDOW_MS) { this.windowStart = now; this.messages = 0; }
    if (++this.messages > MAX_MESSAGES_PER_WINDOW) { strikes.set(this.o.source, STRIKES_TO_TEXT); this.end("too many messages"); return; }
    this.lastPong = now;
    switch (m.t) {
      case "up":
        // A srcdoc document can reload when Obsidian moves its iframe. Its WindowProxy
        // survives, but a new document boot needs init again; duplicate handshakes do not.
        if (m.boot === this.boot) break;
        this.boot = m.boot;
        this.initialized = true;
        this.post({ t: "init", locale: uiLanguage(), block: this.o.block, blockId: this.o.blockId, progress: this.latest, theme: frameTheme(this.o.root) });
        break;
      case "pong": break;
      case "rendered":
        watchdog.settled(this);
        break;
      case "refused":
        console.warn("EvoWiki: an evoui block was not drawn:", m.why);
        this.end(m.why);
        break;
      case "height":
        if (m.h > 0) heights.set(this.o.source, m.h);     // 0: drawn in a view that is not showing
        this.transport.setHeight(m.h);
        break;
      case "event":
        if (this.o.blockId === null || ++this.events > MAX_EVENTS) break;     // an inline block has no stored copy to grade against
        this.o.onEvent({ ...m.ev, block: this.o.blockId });
        break;
      case "progress":
        this.latest = m.progress;
        if (this.o.blockId !== null) this.o.onProgress(m.progress);
        break;
      case "math": {
        const generation = this.generation;
        if (++this.mathRequests > MAX_MATH_REQUESTS) break;
        void typeset(m.tex, m.display, this.o.root.ownerDocument).then((r) => {
          if (this.generation !== generation || this.stopped) return;
          const css = r !== null && r.css !== this.lastCss ? r.css : null;
          if (css !== null) this.lastCss = css;
          this.post({ t: "math", id: m.id, html: r?.html ?? null, css });
        });
        break;
      }
      case "image": {
        const generation = this.generation;
        const asked = declaredImages(this.o.block).has(m.asset) ? this.o.image?.(m.asset) : undefined;
        void (asked ?? Promise.resolve(null)).catch((e: unknown) => {
          console.warn("EvoWiki: an evoui image could not be read", e);
          return null;
        }).then((bytes) => { if (this.generation === generation && !this.stopped) this.post({ t: "image", id: m.id, bytes }); });
        break;
      }
    }
  }

  retheme(): void { if (this.running) this.post({ t: "theme", theme: frameTheme(this.o.root) }); }
  stop(): Promise<void> {
    this.offLanguage?.(); this.offLanguage = null;
    if (this.stopping !== null) return this.stopping;
    if (this.stopped) return Promise.resolve();
    this.stopped = true;
    watchdog.forget(this);
    if (this.transport === null) return Promise.resolve();
    this.stopping = new Promise<void>((resolve) => {
      this.stoppedResolve = resolve;
      // A responsive frame flushes before acknowledging; a killed frame cannot supply a final snapshot.
      this.stopTimer = setTimeout(() => {
        this.stopTimer = null;
        console.warn("EvoWiki: frame closed without acknowledging its final progress");
        this.halt();
      }, 1_000);
      this.post({ t: "stop" });
    });
    return this.stopping;
  }

}

/** The one timer for all frames. */
class Watchdog {
  private readonly sessions = new Set<Session>();
  private timer: number | null = null;
  private lastTick = 0;
  private n = 0;
  private recovering = false;
  /** Sessions waiting to restart after a hang, one at a time. */
  private queue: Session[] = [];
  private restarting: Session | null = null;

  add(s: Session): void {
    this.sessions.add(s);
    if (this.recovering || this.restarting !== null) this.queue.push(s); else s.start();
    if (this.timer === null) { this.lastTick = Date.now(); this.timer = window.setInterval(() => { this.tick(); }, TICK_MS); }
  }

  forget(s: Session): void {
    this.sessions.delete(s);
    this.queue = this.queue.filter((x) => x !== s);
    if (this.sessions.size === 0 && this.timer !== null) { window.clearInterval(this.timer); this.timer = null; }
    this.settled(s);
  }

  settled(s: Session): void { if (this.restarting === s) { this.restarting = null; this.next(); } }

  private tick(): void {
    const now = Date.now();
    // This window was asleep, busy or hidden (timers throttled): silence proves nothing about the frames.
    const blind = now - this.lastTick > 2 * TICK_MS + 500 || document.hidden;
    this.lastTick = now;
    this.n++;
    const hung: Session[] = [];
    for (const s of this.sessions) {
      if (!s.running) continue;
      const connected = s.connected;
      if (blind || !connected || !s.wasConnected) s.lastPong = now;   // out of the DOM (the view scrolled it away): it reloads when it returns
      s.wasConnected = connected;
      if (!connected) continue;
      s.post({ t: "ping", n: this.n });
      if (now - s.lastPong > HANG_MS) hung.push(s);
    }
    if (hung.length > 0) this.recover(hung);
  }

  /** Every frame shares the hung process: all of them go, and come back once it is gone. */
  private recover(hung: Session[]): void {
    console.warn(`EvoWiki: ${String(hung.length)} evoui block(s) stopped responding; restarting`);
    for (const source of new Set(hung.map((s) => s.source))) strikes.set(source, (strikes.get(source) ?? 0) + 1);
    this.restarting = null;
    this.queue = [...this.sessions];
    for (const s of this.sessions) s.showHung();
    this.recovering = true;
    this.probe();
  }

  /** A throwaway frame each second runs once the hung process is gone. A frame left waiting would pin that process, so each is removed. */
  private probe(): void {
    const f = document.createElement("iframe");
    f.setAttribute("sandbox", SANDBOX);
    f.style.display = "none";
    f.srcdoc = `<iframe sandbox="${SANDBOX}" srcdoc="${attr("<script>parent.parent.postMessage('evoui-probe','*')</script>")}"></iframe>`;
    const done = (): void => { window.clearTimeout(timer); window.removeEventListener("message", onMessage); f.remove(); };
    const onMessage = (e: MessageEvent): void => {
      if (e.source === null || e.source !== childOf(f) || e.data !== "evoui-probe") return;
      done();
      this.recovering = false;
      this.next();
    };
    const timer = window.setTimeout(() => { done(); this.probe(); }, PROBE_MS);
    window.addEventListener("message", onMessage);
    document.body.append(f);
  }

  private next(): void {
    if (this.recovering || this.restarting !== null) return;
    const s = this.queue.shift();
    if (s === undefined) return;
    this.restarting = s;
    s.start();
    if (!s.running) { this.restarting = null; this.next(); }
  }
}
const watchdog = new Watchdog();

/** Draws the block in its frame. Returns at once; `onFallback` says when it cannot. */
export function openBlockFrame(o: FrameOptions): BlockFrame {
  const s = new Session(o);
  if (makeTransport === sandboxTransport) watchdog.add(s); else s.start();
  return s;
}

/** Tests only: forget hangs and heights. */
export function resetFramesForTests(): void { heights.clear(); strikes.clear(); }
