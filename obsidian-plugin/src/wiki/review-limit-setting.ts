/**
 * 「每天最多复习几张」 (设置 → 学习, app.js renderSettings): an `.ew-srow` with a text input. The value is the catalog's
 * review_per_day; a change goes through the existing setReviewPerDay (P9: an integer from 0, else nothing is sent; one
 * PUT /wiki/settings, then the catalog and today's list are read again). Only the note this row's own change produced
 * is told: as a notice once the change is over, and a failure also in the row's `.ew-muted` line.
 */
import { SETTINGS_COPY } from "../copy";
import { ewNotice } from "../ui/ew-notice";
import { setStatus, settingRow, statusLine, textInput } from "../ui/settings-rows";
import { writeBlocked } from "./actions";
import type { ResultNote, WikiState } from "./state";

export interface ReviewLimitHost {
  state(): WikiState;
  subscribe(fn: () => void): () => void;
  setReviewPerDay(n: number): void;
  /** Reads the catalog if it is not loaded or loading (the limit lives in its settings). */
  load(): void;
}

export function mountReviewLimitSetting(box: HTMLElement, host: ReviewLimitHost): { dispose(): void } {
  const input = textInput("", () => (SETTINGS_COPY.reviewPerDayName));
  input.inputMode = "numeric";
  const { left } = settingRow(box, () => (SETTINGS_COPY.reviewPerDayName), null, input);
  const status = left.appendChild(statusLine());
  /** The review note before this row's last change: only a note that change produced is told. */
  let before: ResultNote | null | undefined;
  let told: ResultNote | null = null;
  let shown: number | null = null;

  const draw = (): void => {
    const s = host.state();
    const cat = s.catalog;
    const ready = cat.kind === "ready";
    input.disabled = !ready || writeBlocked(s);
    if (ready) {
      const v = cat.value.settings.review_per_day;
      // A value typed but not yet committed is kept; a new value from the server replaces it.
      if (v !== shown || input.ownerDocument.activeElement !== input) input.value = String(v);
      shown = v;
    }
    const note = s.review.note;
    const mine = before !== undefined && note !== null && note !== before ? note : null;
    if (mine !== null && mine !== told && s.busy === null) { told = mine; ewNotice(mine.text); }
    if (mine !== null && mine.tone === "error") input.setAttribute("aria-invalid", "true");
    else input.removeAttribute("aria-invalid");
    setStatus(status, !s.configured ? SETTINGS_COPY.needsSettings
      : cat.kind === "error" ? cat.message
      : !ready ? SETTINGS_COPY.reviewPerDayWaiting
      : mine !== null && mine.tone === "error" ? mine.text
      : "");
  };
  input.addEventListener("change", () => {
    before = host.state().review.note;
    // A blank box is not 0 (Number("") === 0): NaN reaches the controller, which says reviewPerDayInvalid.
    host.setReviewPerDay(input.value.trim() === "" ? NaN : Number(input.value));
    draw();
  });
  draw();
  const off = host.subscribe(draw);
  host.load();
  return { dispose: () => { off(); } };
}
