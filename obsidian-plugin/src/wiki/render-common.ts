/** Small helpers shared by the render-*.ts files. No UI copy here. */
import { h } from "../ui/dom";
import { btn, el } from "../ui/ew";
import { withSettingsLink } from "../ui/settings-link";
import type { WikiActions } from "./actions";
import { WIKI_COPY } from "./copy";
import type { ResultNote, WikiState } from "./state";

/** A, B, C… for option labels. */
export const letter = (i: number): string => String.fromCharCode(65 + i);

/**
 * The one offline line (offline only, else null): `.ew-muted` 未连接到服务器 + `.ew-btn.ew-link` 重试. The sidebar shows
 * it at its very top for all three tabs (wiki-renderer.ts); the 知识图谱 page shows the same line. 重试 reloads everything.
 */
export function offlineBanner(offline: boolean, retry: () => void): HTMLDivElement | null {
  if (!offline) return null;
  const d = el("div", "muted");
  d.dataset.role = "offline";
  d.setAttribute("role", "status");
  const b = btn(() => (WIKI_COPY.retry), "link", retry);
  b.dataset.action = "retry-connection";
  d.append(el("span", "", () => (WIKI_COPY.notConnected)), " ", b);
  return d;
}

/**
 * A section's read failed: offline nothing (the sidebar's one 未连接到服务器 banner says it, and 重试 there reloads
 * everything); otherwise the one short 「没能读到，重试」 line (`.ew-muted` + `.ew-btn.ew-link`). `action`: the 重试
 * button's data-action.
 */
export function failLine(s: WikiState, role: string, retry: () => void, action = "retry", disabled = false): HTMLDivElement | null {
  if (s.conn === "offline") return null;
  const d = el("div", "muted");
  d.dataset.role = role;
  const b = btn(() => (WIKI_COPY.retry), "link", retry);
  b.dataset.action = action;
  b.disabled = disabled;
  d.append(el("span", "", () => (WIKI_COPY.readFailedShort)), b);
  return d;
}

export function errorLine(text: string | null): HTMLElement | null {
  return text === null ? null : h("div", { class: "evowiki-wiki-error" }, text);
}

/** The refresh-failed line under a write's result (ResultNote.sub), or null. */
function subLine(n: ResultNote): HTMLElement | null {
  return n.sub === undefined ? null : h("div", { class: "evowiki-wiki-sub evowiki-wiki-error" }, n.sub);
}

/** A result line (ok / error), with a retry button when the note carries a retry action, then its refresh line. */
export function resultLine(n: ResultNote, cls: string, a: WikiActions, blocked: boolean): HTMLElement {
  const retry = n.retry;
  return h("div", { class: `${cls} ${n.tone === "error" ? "evowiki-wiki-error" : "evowiki-wiki-ok"}` },
    h("span", {}, ...withSettingsLink(n.text, () => a.openSettings())),
    retry ? h("button", { "data-action": "retry", disabled: blocked, onclick: () => a.retry(retry) }, () => (WIKI_COPY.retry)) : null,
    subLine(n));
}

/** A multi-line note: one div per line, then its refresh line. */
export function multiLine(n: ResultNote, cls: string): HTMLElement {
  return h("div", { class: `${cls} ${n.tone === "error" ? "evowiki-wiki-error" : "evowiki-wiki-ok"}` },
    ...n.text.split("\n").map((line) => h("div", {}, line)), subLine(n));
}

export function chip(text: string, cls = ""): HTMLElement {
  return h("span", { class: cls ? `evowiki-wiki-chip ${cls}` : "evowiki-wiki-chip" }, text);
}

/** Why a write button is off, or null. The demo, an unset connection, offline and another write in flight each say so. */
export function blockedReason(s: WikiState): string | null {
  if (s.demo) return WIKI_COPY.attnBlockedDemo;
  if (!s.configured) return WIKI_COPY.needsSettings;
  if (s.conn === "offline") return WIKI_COPY.offline;
  if (s.busy !== null) return s.busy.what === "rsi" ? WIKI_COPY.attnWorking : WIKI_COPY.attnBlockedBusy;
  return null;
}

/** The one muted line that says why the buttons below it are off (nothing when they are on). */
export function reasonLine(s: WikiState): HTMLElement | null {
  const reason = blockedReason(s);
  return reason === null ? null : h("p", { class: "evowiki-muted evowiki-wiki-reason", "data-role": "blocked", role: "status" }, reason);
}
