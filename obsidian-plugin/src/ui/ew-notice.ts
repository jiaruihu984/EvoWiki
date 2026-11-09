/**
 * The demo's notices (app.js `notice`, shell.html `.notices` / `.notice`), used instead of Obsidian's Notice for every
 * EvoWiki message from the new UI. One `div.ew.ew-layer` on document.body (created once, reused; re-attached if something
 * removed it) holds `div.ew-notices` (aria-live=polite). A notice stays 4200 ms, or 7000 ms when it carries an action.
 */
import { el } from "./ew";

export const NOTICE_MS = 4200;
export const NOTICE_ACTION_MS = 7000;

export interface EwNoticeAction { label: string; run: () => void }

let layer: HTMLDivElement | null = null;
let notices: HTMLDivElement | null = null;

/** The `.ew-notices` container inside the shared `.ew.ew-layer` on document.body. */
export function noticeHost(): HTMLDivElement {
  if (layer === null || notices === null) {
    layer = document.createElement("div");
    layer.className = "ew ew-layer";
    notices = el("div", "notices");
    notices.setAttribute("aria-live", "polite");
    layer.append(notices);
  }
  if (!layer.isConnected) document.body.append(layer);
  return notices;
}

/** app.js `notice(text, action)`: span + one `.ew-lk` button per action (one or a list); clicking an action runs it and removes the notice. */
export function ewNotice(text: string, action?: EwNoticeAction | readonly EwNoticeAction[]): HTMLDivElement {
  const n = el("div", "notice");
  n.append(el("span", "", text));
  const acts: readonly EwNoticeAction[] = action === undefined ? [] : Array.isArray(action) ? action : [action as EwNoticeAction];
  for (const x of acts) {
    const a = el("button", "lk", x.label);
    a.type = "button";
    a.onclick = () => { x.run(); n.remove(); };
    n.append(a);
  }
  noticeHost().append(n);
  window.setTimeout(() => n.remove(), acts.length ? NOTICE_ACTION_MS : NOTICE_MS);
  return n;
}
