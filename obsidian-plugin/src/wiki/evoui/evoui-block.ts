/**
 * Draws an `evoui` code block in place: the block's JSON (inline, or an `asset: <id>` read from the vault's
 * `Wiki/assets/<id>.json`) goes to a sandboxed frame (frame-host.ts), which validates it and runs D1's renderer. Nothing of
 * the block is evaluated in this window. A block that fails the spec, a renderer that throws or a block that stops responding
 * shows the block's own fallback text, or one plain line; the note never breaks.
 *
 * What the reader does in an asset block comes back as the renderer's result events (item / summary / route / request,
 * every question kind included), handed to `port.onEvent` with the asset id. An inline block reports nothing: the server
 * grades against its stored copy, and an inline block has none.
 *
 * A block with an `app` node runs a program, so it runs by itself only when the server says it wrote exactly this content
 * (`port.isGenerated`: a content hash, nothing in the note counts). Otherwise, and when the server cannot be asked, it shows
 * its text and 运行这个交互; one press runs this block in this mount, the same sandbox, and trusts nothing else.
 */
import { h } from "../../ui/dom";
import { WIKI_COPY } from "../copy";
import type { EmbedHub } from "../embed/embed-block";
import { fallbackOf, parseEvouiSource } from "./block";
import { OFFER_AFTER_MS, runOffer } from "../run-offer";
import { type BlockFrame, hasAppNode, openBlockFrame } from "./frame-host";
import type { BlockEvent, BlockProgressData } from "./block-event";

export type { BlockEvent };

/** What a block reads and calls; main.ts binds it to the vault, the connection, the asset fetcher and the controller. */
export interface EvouiPort {
  /** Whether EvoWiki stored a block with exactly this content (POST /wiki/assets/generated); absent or failing = not known. */
  isGenerated?(text: string): Promise<boolean>;
  /** The text of the vault file at `path`, or null when there is none. */
  read(path: string): Promise<string | null>;
  canFetch(): boolean;
  /** Where a fetched evoui asset lands (`<notes>/Wiki/assets/<id>.json`). */
  assetPath(assetId: string): string | null;
  retrieve(assetId: string): Promise<boolean>;
  /** A result event of an asset block (evidence, graded by the server); absent = nobody listens. */
  onEvent?(assetId: string, ev: BlockEvent): void;
  /** The progress saved for an asset block (null = none, or the server cannot be reached: render fresh); absent = no restore. */
  progress?(assetId: string): Promise<BlockProgressData | null>;
  /** The renderer's `onProgress` of an asset block: the newest progress, to be kept until the server has it. */
  onProgress?(assetId: string, progress: BlockProgressData): void;
  /** The WebP of an image asset (fetched when it is not in the vault yet), null when there is none. */
  image?(assetId: string): Promise<ArrayBuffer | null>;
}

// A replacement mount restores only after the previous mount handed off its last snapshot.
const pendingStops = new Map<string, Promise<void>>();

const line = (text: string, cls = "evowiki-embed-hint"): HTMLElement => h("div", { class: cls }, text);

/** Draws the block into `el`; returns the cleanup. */
export function mountEvouiBlock(el: HTMLElement, source: string, port: EvouiPort, hub: EmbedHub): () => void {
  const root = h("div", { class: "evowiki-evoui" });
  el.replaceChildren(root);
  let stopped = false;
  let gen = 0;
  let fetching = false;
  let failed = false;
  let triedFetch = false;
  let frame: BlockFrame | null = null;
  let drawn = 0;
  let frameAsset: string | null = null;
  const stopFrame = (): Promise<void> | null => {
    const previous = frame;
    frame = null;
    if (previous === null) return null;
    const pending = previous.stop();
    if (frameAsset !== null) {
      const asset = frameAsset;
      pendingStops.set(asset, pending);
      void pending.then(() => { if (pendingStops.get(asset) === pending) pendingStops.delete(asset); });
    }
    return pending;
  };

  const showFallback = (block: unknown): void => {
    const text = fallbackOf(block);
    root.replaceChildren(h("div", { class: text === null ? "evowiki-embed-hint" : "evowiki-evoui-fallback" }, () => (text ?? WIKI_COPY.evouiUnreadable)));
  };

  /** The block's text with 运行这个交互. */
  const offerRun = (raw: unknown, run: () => void): void => {
    showFallback(raw);
    root.append(runOffer(run));
  };

  /** `text`: what `raw` was parsed from, the content the server is asked about. */
  const draw = (raw: unknown, text: string, assetId: string | null, saved: BlockProgressData | null = null): void => {
    const mine = ++drawn;
    const previous = stopFrame();
    if (previous !== null) {
      void previous.then(async () => {
        const latest = assetId !== null && port.progress ? await port.progress(assetId).catch(() => saved) : saved;
        if (!stopped && mine === drawn) draw(raw, text, assetId, latest);
      });
      return;
    }
    if (!hasAppNode(raw)) { open(raw, assetId, saved); return; }
    showFallback(raw);        // while the server is asked: nothing running
    let pressed = false;
    const run = (): void => { if (!pressed && !stopped && mine === drawn) { pressed = true; open(raw, assetId, saved); } };
    // a server just started or gone answers in seconds: the offer does not wait for it
    const offer = window.setTimeout(() => { if (!pressed && !stopped && mine === drawn) offerRun(raw, run); }, OFFER_AFTER_MS);
    void (port.isGenerated?.(text) ?? Promise.resolve(false)).catch((e: unknown) => {
      console.warn("EvoWiki: could not ask where an evoui block came from", e);
      return false;
    }).then((generated) => {
      window.clearTimeout(offer);
      if (stopped || mine !== drawn || pressed) return;
      if (generated) run();
      else offerRun(raw, run);
    });
  };

  const open = (raw: unknown, assetId: string | null, saved: BlockProgressData | null): void => {
    frameAsset = assetId;
    const host = h("div", { class: "evowiki-evoui-host" });
    frame = openBlockFrame({
      host, root, source, block: raw, blockId: assetId, progress: saved,
      onEvent: (ev) => {
        if (stopped || assetId === null) return;
        try { port.onEvent?.(assetId, ev); } catch (e) { console.warn("EvoWiki: block event not delivered", e); }
      },
      onProgress: (p) => {
        if (assetId === null) return;
        try { port.onProgress?.(assetId, p); } catch (e) { console.warn("EvoWiki: block progress not delivered", e); }
      },
      image: (asset) => port.image?.(asset) ?? Promise.resolve(null),
      onStart: () => { root.replaceChildren(host); },
      onFallback: () => { showFallback(raw); },
    });
  };

  const parsed = parseEvouiSource(source);
  if (parsed.kind === "invalid") {
    root.replaceChildren(line(WIKI_COPY.evouiUnreadable));
    return () => undefined;
  }
  if (parsed.kind === "block") {
    draw(parsed.block, source, null);
    const offTheme = hub.add({ themeChanged: () => { frame?.retheme(); }, assetWritten: () => undefined });
    return () => { stopped = true; void stopFrame(); offTheme(); };
  }

  // asset reference: read the cached file; a missing one offers 取回 (same as an embedded-html interactive)
  const id = parsed.id;
  const path = port.assetPath(id);
  if (path === null) {
    root.replaceChildren(line(WIKI_COPY.evouiUnreadable));
    return () => undefined;
  }
  const missing = (): HTMLElement => h("div", { class: "evowiki-embed-missing" }, line(WIKI_COPY.evouiMissing), port.canFetch()
      ? h("button", { class: "evowiki-embed-fetch", disabled: fetching, onclick: () => void fetchNow() }, () => (fetching ? WIKI_COPY.embedFetching : WIKI_COPY.embedFetch))
      : null, failed ? line(WIKI_COPY.embedFetchFailed, "evowiki-embed-error") : null);
  const load = (): void => {
    const mine = ++gen;
    void port.read(path).then((text) => {
      if (stopped || mine !== gen) return;
      if (text === null) {
        // Fetched once by itself (a note not synced yet, a review answer): 取回 stays only for a failure.
        if (port.canFetch() && !triedFetch) { triedFetch = true; void fetchNow(); } else root.replaceChildren(missing());
        return;
      }
      let raw: unknown;
      try { raw = JSON.parse(text) as unknown; } catch { root.replaceChildren(line(WIKI_COPY.evouiUnreadable)); return; }
      if (port.progress === undefined) { draw(raw, text, id); return; }
      void (pendingStops.get(id) ?? Promise.resolve()).then(() => port.progress!(id)).catch(() => null).then((saved) => {
        if (stopped || mine !== gen) return;
        draw(raw, text, id, saved);
      });
    }, (e: unknown) => {
      if (stopped || mine !== gen) return;
      console.warn("EvoWiki: could not read an evoui asset", e);
      root.replaceChildren(missing());
    });
  };
  const fetchNow = async (): Promise<void> => {
    if (fetching) return;
    fetching = true; failed = false;
    root.replaceChildren(missing());
    let got = false;
    try { got = await port.retrieve(id); } catch (e) { console.warn("EvoWiki: could not fetch an evoui asset", e); }
    fetching = false; failed = !got;
    if (!stopped) { if (got) load(); else root.replaceChildren(missing()); }
  };
  load();
  const off = hub.add({
    themeChanged: () => { frame?.retheme(); },
    // A written asset needs a redraw, if this block is waiting for it.
    assetWritten: (written) => { if (written === id && !fetching && root.querySelector(".evowiki-embed-missing") !== null) load(); },
  });
  return () => { stopped = true; void stopFrame(); off(); };
}
