/**
 * 「生成内容的语言」 (设置 → 学习, app.js renderSettings): an `.ew-srow` with a select (简体中文 / English). The value is
 * the catalog's settings.language (read once if the catalog is not loaded). R7.4: the language lives on the server only;
 * the plugin keeps no copy. A change goes through WikiController.setLanguage (PUT {language}, then the
 * catalog is read again): saved → the demo's notice 已保存。; refused → the select goes back, the reason in the row's
 * `.ew-muted` line and a notice.
 */
import { SETTINGS_COPY } from "../copy";
import { ewNotice } from "../ui/ew-notice";
import { selectBox, setStatus, settingRow, statusLine } from "../ui/settings-rows";
import { LANGUAGE_NAMES, LANGUAGES, type Language } from "./schema";
import type { WikiState } from "./state";

export interface LanguageHost {
  state(): WikiState;
  subscribe(fn: () => void): () => void;
  /** Null when saved (or unchanged), else the plain reason. */
  setLanguage(lang: Language): Promise<string | null>;
  /** Reads the catalog if it is not loaded or loading (the language lives in its settings). */
  load(): void;
}

const isLanguage = (v: string): v is Language => (LANGUAGES as readonly string[]).includes(v);

export function mountLanguageSetting(box: HTMLElement, host: LanguageHost): { dispose(): void } {
  const select = selectBox(LANGUAGES.map((l) => [l, LANGUAGE_NAMES[l]] as const), null, () => (SETTINGS_COPY.languageName));
  select.dataset.field = "language";
  const { left } = settingRow(box, () => (SETTINGS_COPY.languageName), null, select);
  const status = left.appendChild(statusLine());
  /** A change on its way: the select keeps the chosen value until it is answered. */
  let pending = false;
  /** This row's own failure (cleared when another change starts). */
  let failed: string | null = null;
  let disposed = false;

  const draw = (): void => {
    const s = host.state();
    const cat = s.catalog;
    const ready = cat.kind === "ready";
    // The demo wiki (its fake) would make a change look saved: shown, never changed.
    select.disabled = pending || !ready || s.conn === "offline" || s.demo;
    if (ready && !pending) select.value = cat.value.settings.language;
    setStatus(status, s.demo ? SETTINGS_COPY.demoNoEdit
      : !s.configured ? SETTINGS_COPY.needsSettings
      : cat.kind === "error" ? cat.message
      : !ready ? SETTINGS_COPY.languageWaiting
      : failed ?? "");
  };
  select.addEventListener("change", () => {
    const s = host.state();
    const prev = s.catalog.kind === "ready" ? s.catalog.value.settings.language : null;
    const v = select.value;
    if (!isLanguage(v) || prev === null || s.demo) { if (prev !== null) select.value = prev; return; }
    pending = true;
    failed = null;
    draw();
    void host.setLanguage(v).then((err) => {
      if (disposed) return;
      pending = false;
      failed = err;
      draw();
      if (err === null) { ewNotice(SETTINGS_COPY.saved); return; }
      ewNotice(err);
      // A refused change: back to the value the server still has (draw already shows the catalog's value).
      if (host.state().catalog.kind !== "ready") select.value = prev;
    });
  });
  draw();
  const off = host.subscribe(draw);
  host.load();
  return { dispose: () => { disposed = true; off(); } };
}
