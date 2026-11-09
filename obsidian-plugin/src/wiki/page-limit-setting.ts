/**
 * 用量 · 「每页最多花（美元）」 (single writer: SettingsIO.page_limit_usd, null = no cap): an `.ew-srow` with a text input,
 * the same look and parsing as 每天最多花 / 每月最多花 (usage-section.ts; empty = no limit, a plain decimal else, flagged
 * aria-invalid and not sent when it is not one or lies outside 0.05–5). The value is the catalog's settings (read once if
 * the catalog is not loaded); a change goes through WikiController.setPageLimit (PUT, then the catalog is read again):
 * saved → notice 已保存。; refused → the input goes back, the reason in the row's `.ew-muted` line and a notice.
 */
import { SETTINGS_COPY } from "../copy";
import { ewNotice } from "../ui/ew-notice";
import { setStatus, settingRow, statusLine, textInput } from "../ui/settings-rows";
import { writeBlocked } from "./actions";
import { PAGE_LIMIT_USD } from "./schema";
import type { WikiState } from "./state";
import { parseLimit } from "./usage";

export interface PageLimitHost {
  state(): WikiState;
  subscribe(fn: () => void): () => void;
  /** Null when saved (or unchanged), else the plain reason. */
  setPageLimit(limit: number | null): Promise<string | null>;
  /** Reads the catalog if it is not loaded or loading (the limit lives in its settings). */
  load(): void;
}

const limitText = (v: number | null): string => (v === null ? "" : String(v));

export function mountPageLimitSetting(box: HTMLElement, host: PageLimitHost): { dispose(): void } {
  const input = textInput("", () => (SETTINGS_COPY.pageLimitName));
  input.inputMode = "decimal";
  const { left } = settingRow(box, () => (SETTINGS_COPY.pageLimitName), null, input);
  const status = left.appendChild(statusLine());
  let pending = false;
  let failed: string | null = null;
  let disposed = false;
  /** The value last drawn from the server (the input goes back to it after a refused change). */
  let shown: string | null = null;

  const draw = (): void => {
    const s = host.state();
    const cat = s.catalog;
    const ready = cat.kind === "ready";
    input.disabled = pending || !ready || writeBlocked(s) || s.demo;
    if (ready && !pending) {
      const v = limitText(cat.value.settings.page_limit_usd);
      // Redraws never overwrite what is being typed; only a new server value replaces it.
      if (v !== shown) { shown = v; input.value = v; input.removeAttribute("aria-invalid"); }
    }
    setStatus(status, s.demo ? SETTINGS_COPY.demoNoEdit
      : !s.configured ? SETTINGS_COPY.needsSettings
      : cat.kind === "error" ? cat.message
      : !ready ? SETTINGS_COPY.languageWaiting
      : failed ?? "");
  };
  input.addEventListener("change", () => {
    const s = host.state();
    if (s.catalog.kind !== "ready" || s.demo) { input.value = shown ?? ""; return; }
    const v = parseLimit(input.value);
    if (v === undefined || (v !== null && (v < PAGE_LIMIT_USD.min || v > PAGE_LIMIT_USD.max))) {
      input.setAttribute("aria-invalid", "true");
      failed = SETTINGS_COPY.pageLimitInvalid;
      draw();
      ewNotice(SETTINGS_COPY.pageLimitInvalid);
      return;
    }
    input.removeAttribute("aria-invalid");
    pending = true;
    failed = null;
    draw();
    void host.setPageLimit(v).then((err) => {
      if (disposed) return;
      pending = false;
      failed = err;
      if (err !== null) { shown = null; }   // the next draw puts the server's value back
      draw();
      if (err === null) { ewNotice(SETTINGS_COPY.saved); return; }
      ewNotice(err);
    });
  });
  draw();
  const off = host.subscribe(draw);
  host.load();
  return { dispose: () => { disposed = true; off(); } };
}
