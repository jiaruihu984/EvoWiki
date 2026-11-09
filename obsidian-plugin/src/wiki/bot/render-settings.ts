import { localizedAttribute } from "../../i18n/index";
/**
 * 参数 · 开启 Bot: heading with the master switch, 「Bot 权限」 (the kind switches, then the six levels 执行前确认 / 自动执行),
 * 「提醒规则」 (sentence rows with their numbers, and the daily check time). Every change is a patch of only the touched fields (botSaveSettings; the controller merges it onto the latest settings and PUTs the whole); an invalid number or time goes
 * back without a request and says why in one line under its row; a refused PUT is redrawn from the unchanged settings by the controller.
 * No 时区 row: the Bot's day follows this computer's zone (BotController.followSystemZone, ruling 2026-10-28).
 */
import { el } from "../../ui/ew";
import { switchButton } from "../../ui/settings-rows";
import { type WikiActions, writeBlocked } from "../actions";
import { WIKI_COPY } from "../copy";
import { failLine } from "../render-common";
import type { WikiState } from "../state";
import { BOT_KIND, SETTINGS_COPY as C } from "./bot-copy";
import type { BotKind, Level, Scope, SettingsPatch } from "./bot-schema";

/** Demo switch key -> the backend kinds it flips together. */
const SWITCHES: readonly (readonly [string, readonly BotKind[]])[] = [
  ["review", ["review_due"]], ["remedial", ["wrong_answers", "open_question"]], ["goal", ["goal_idle"]],
  ["prefetch", ["idle_write"]], ["weekly", ["goal_review"]], ["source", ["source_added", "card_stale"]]];
/** The demo's six level rows -> the scopes each sets. */
const LEVELS: readonly (readonly Scope[])[] = [["page_body"], ["new_page"], ["delete_merge"], ["structure"], ["teaching_params", "rsi"], ["path"]];
/** The pydantic bounds of BotSettings, with 0 left out (the demo accepts only > 0). */
const NUM = { today_cap: [1, 50], wrong_count: [2, 20], wrong_days: [1, 60], idle_days: [1, 60] } as const;
type NumKey = keyof typeof NUM;

export function settingsSection(x: HTMLElement, s: WikiState, a: WikiActions): void {
  x.id = "bot-settings";
  const heading = el("h2", "row");
  heading.style.justifyContent = "space-between";
  heading.append(el("span", "", () => (C.title)));
  x.append(heading);
  const st = s.bot.settings;
  if (st.kind === "idle" || st.kind === "loading") { x.append(el("div", "muted", () => (WIKI_COPY.loading))); return; }
  if (st.kind === "error") {
    const f = failLine(s, "settings-error", () => a.loadEngine());
    if (f) x.append(f);
    return;
  }
  const cur = st.value;
  // Timed pauses can remain in settings after they expire; match effective_paused on the server.
  const paused = cur.paused === "always" || (cur.paused === "until" && cur.paused_until !== null && Date.parse(cur.paused_until) > Date.now());
  const enabled = switchButton(!paused, () => (C.title), (on) => {
    // Show only confirmed server state: a refused request must not leave an optimistic switch flipped.
    enabled.setAttribute("aria-checked", String(!paused));
    a.botPause(on ? "off" : "forever");
  });
  enabled.id = "bot-enabled";
  enabled.disabled = writeBlocked(s);
  heading.append(enabled);
  const save = (patch: SettingsPatch): void => a.botSaveSettings(patch);
  const row = (name: string, ctl: HTMLElement): void => {
    const r = el("div", "srow");
    const l = el("div");
    l.append(el("div", "n", name));
    r.append(l, ctl);
    x.append(r);
  };
  const sub = (t: string): void => { x.append(el("div", "sub", t)); };
  /** The refusal line under the row of `input` (replaced on each refusal, removed on a good value). */
  const hint = (input: HTMLElement, text: string | null): void => {
    const r = input.closest(".ew-srow") ?? input;
    const next = r.nextElementSibling;
    if (next instanceof HTMLElement && next.dataset.hintFor === input.id) next.remove();
    if (text === null) return;
    const h = el("div", "muted", text);
    h.dataset.hintFor = input.id;
    h.setAttribute("role", "alert");
    r.after(h);
  };
  const pair = (...parts: (HTMLElement | string)[]): HTMLElement => {
    const p = el("div", "row");
    p.style.cssText = "gap:6px;flex-wrap:wrap;font-size:13px;color:var(--tx-2)";
    for (const part of parts) {
      if (typeof part !== "string") { p.append(part); continue; }
      const phrase = el("span", "", part);
      phrase.style.whiteSpace = "nowrap";
      p.append(phrase);
    }
    return p;
  };
  const group = (...parts: (HTMLElement | string)[]): HTMLElement => {
    const phrase = el("span");
    phrase.style.cssText = "display:inline-flex;align-items:center;gap:6px;white-space:nowrap";
    phrase.append(...parts);
    return phrase;
  };
  const num = (key: NumKey, id: string, label: string): HTMLInputElement => {
    const i = el("input", "num");
    i.type = "text";
    i.inputMode = "numeric";
    i.id = id;
    i.setAttribute("aria-label", label);
    i.value = String(cur[key]);
    i.onchange = () => {
      const t = i.value.trim();
      const v = /^\d+$/.test(t) ? parseInt(t, 10) : NaN;
      const [lo, hi] = NUM[key];
      if (v >= lo && v <= hi) { hint(i, null); if (v !== cur[key]) save({ [key]: v }); } else { i.value = String(cur[key]); hint(i, C.badNumber(lo, hi)); }
    };
    return i;
  };

  sub(C.permSub);
  for (const [k, kinds] of SWITCHES) {
    const label = BOT_KIND[kinds[0]!]!;
    const sw = el("button", "switch");
    sw.type = "button";
    sw.id = "bot-kind-" + k;
    sw.setAttribute("role", "switch");
    if (!kinds.every((b) => cur.kinds[b] !== undefined)) continue;   // a kind this backend lacks: no row
    const on = kinds.every((b) => cur.kinds[b] === true);
    sw.setAttribute("aria-checked", String(on));
    sw.setAttribute("aria-label", label);
    sw.onclick = () => save({ kinds: Object.fromEntries(kinds.map((b) => [b, !on])) });
    row(label, sw);
  }
  LEVELS.forEach((scopes, i) => {
    if (!scopes.every((sc) => cur.levels[sc] !== undefined)) return;   // a scope this backend lacks: no row
    const sel = el("select");
    sel.id = "bot-level-" + i;
    // A row covering two scopes (讲解规则和学习状态评估) shows ask when either is ask (the stricter), else the first scope's level.
    const lv = scopes.map((sc) => cur.levels[sc]);
    const now: Level = lv.includes("ask") ? "ask" : lv[0]!;
    if (now !== "ask" && now !== "auto_undo") {
      const odd = el("option", "", now);
      odd.value = now;
      odd.disabled = true;
      odd.selected = true;
      sel.append(odd);
    }
    for (const [v, label] of [["ask", C.levelAsk], ["auto_undo", C.levelAuto]] as const) {
      const op = el("option", "", label);
      op.value = v;
      op.selected = v === now;
      sel.append(op);
    }
    sel.onchange = () => {
      if (sel.value === now) return;
      save({ levels: Object.fromEntries(scopes.map((sc) => [sc, sel.value as Level])) });
    };
    row(C.levels[i]!, sel);
  });

  sub(C.everSub);
  // app.js `line`: a sentence with its numbers inline (one `.sline` row, no name column)
  const line = (...parts: (HTMLElement | string)[]): void => {
    const r = el("div", "srow sline");
    r.append(pair(...parts));
    x.append(r);
  };
  line(C.perDayPre, group(num("today_cap", "bot-perday", C.perDayLabel), C.perDayPost));
  line(group(num("wrong_days", "bot-wrong-days", C.wrongDaysLabel), C.wrongDays), C.wrongPre,
    group(num("wrong_count", "bot-wrong-n", C.wrongCountLabel), C.wrongPost));
  line(group(C.idlePre, num("idle_days", "bot-idle", C.idleDaysLabel), C.idleDays), C.idlePost);
  const at = el("input", "num");
  at.type = "text";
  at.id = "bot-check-at";
  localizedAttribute(at, "aria-label", () => (C.checkAt));
  const shown = `${String(cur.daily_hour).padStart(2, "0")}:${String(cur.daily_minute).padStart(2, "0")}`;
  at.value = shown;
  at.style.width = "60px";
  at.onchange = () => {
    const m = /^(\d{1,2}):(\d{2})$/.exec(at.value.trim());
    const h = m ? parseInt(m[1]!, 10) : NaN;
    const mi = m ? parseInt(m[2]!, 10) : NaN;
    if (h >= 0 && h <= 23 && mi >= 0 && mi <= 59) {
      hint(at, null);
      if (h !== cur.daily_hour || mi !== cur.daily_minute) save({ daily_hour: h, daily_minute: mi });
    } else { at.value = shown; hint(at, C.badTime); }
  };
  row(C.checkAt, at);
}
