import { tr, uiLanguage } from "../i18n";
/** Local-time dates the plugin shows or keys by: one day key, one parse of the server's timestamps, one time label. */

const two = (n: number): string => String(n).padStart(2, "0");

/** The local calendar day of `d` as `YYYY-MM-DD` (hide keys, 今天 dots, reading counts, the 「我的笔记」 move heading). */
export const localDayKey = (d: Date): string => `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}`;

/** An ISO timestamp; one without a zone designator is UTC (the server writes UTC). null if unparseable. */
export function parseWhen(iso: string): Date | null {
  const d = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso) || !/T/.test(iso) ? iso : `${iso}Z`);
  return Number.isNaN(d.getTime()) ? null : d;
}

const dayStart = (d: Date): number => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
/** Whole local calendar days from `d` to `now` (0 = same day; a future time counts as today). */
export function dayDiff(d: Date, now: Date): number {
  return Math.max(0, Math.round((dayStart(now) - dayStart(d)) / 86_400_000));
}

/**
 * A server timestamp in local time, "" if unparseable. The two places the demo shows one:
 * - `list` (the assistant's 历史对话 rows, asst.js): 今天 HH:MM / 昨天 HH:MM / M月D日 (this year) / YYYY年M月D日;
 * - `stamp` (参数 · Bot 最近做了什么, app.js `when`): 今天 H:MM / 昨天 H:MM / M月D日 H:MM.
 */
export function formatWhen(iso: string, now: Date, form: "list" | "stamp"): string {
  const d = parseWhen(iso);
  if (!d) return "";
  const n = dayDiff(d, now);
  const day = n === 0 ? tr("assistant.history.HISTORY_LABEL.today.1") : n === 1 ? tr("assistant.history.HISTORY_LABEL.yesterday.2") : null;
  const md = new Intl.DateTimeFormat(uiLanguage(), { month: "short", day: "numeric" }).format(d);
  if (form === "stamp") return `${day ?? md} ${d.getHours()}:${two(d.getMinutes())}`;
  if (day !== null) return `${day} ${two(d.getHours())}:${two(d.getMinutes())}`;
  return d.getFullYear() === now.getFullYear() ? md : new Intl.DateTimeFormat(uiLanguage(), { year: "numeric", month: "short", day: "numeric" }).format(d);
}
