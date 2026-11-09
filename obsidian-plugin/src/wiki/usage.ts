import { tr } from "../i18n/index";
/**
 * The usage API (src/evowiki/service/routes_usage.py): spend today and this month by purpose group, spending limits. GET /usage, GET /usage/settings, PATCH /usage/settings (partial: an absent field is kept, a null
 * limit removes it).
 */
import { jsonCall } from "../api/client";
import type { WikiConfig, WikiHttp } from "./api";
import { USAGE_COPY } from "./copy";
import type { GroupSpendOut, PurposeGroup, UsageOut, UsageSettingsIn, UsageSettingsOut } from "../generated/service";

export const GROUP_LABEL: Record<PurposeGroup, string> = { get page() { return tr("wiki.usage.GROUP_LABEL.page.1"); }, get assistant() { return tr("wiki.usage.GROUP_LABEL.assistant.2"); }, get curator() { return tr("wiki.usage.GROUP_LABEL.curator.3"); }, get diagnostic() { return tr("wiki.usage.GROUP_LABEL.diagnostic.4"); }, get review() { return tr("wiki.usage.GROUP_LABEL.review.5"); }, get other() { return tr("wiki.usage.GROUP_LABEL.other.6"); }, get proactive() { return tr("wiki.usage.GROUP_LABEL.proactive.7"); }, get rsi() { return tr("wiki.usage.GROUP_LABEL.rsi.8"); } };

export class UsageClient {
  constructor(private readonly config: () => WikiConfig, private readonly http: WikiHttp) {}
  usage(): Promise<UsageOut> { return jsonCall(this.http, this.config(), "GET", "/usage"); }
  settings(): Promise<UsageSettingsOut> { return jsonCall(this.http, this.config(), "GET", "/usage/settings"); }
  patch(body: UsageSettingsIn): Promise<UsageSettingsOut> { return jsonCall(this.http, this.config(), "PATCH", "/usage/settings", body); }
}

/** "" → null (no limit); a plain non-negative decimal → the number; anything else → undefined (invalid, not sent). */
export function parseLimit(text: string): number | null | undefined {
  const t = text.trim();
  if (t === "") return null;
  if (!/^\d+(\.\d+)?$/.test(t)) return undefined;
  const n = Number(t);
  return Number.isFinite(n) ? n : undefined;
}

/** 0 → "$0"; < 0.01 → "不到 $0.01"; else "$" + 2 decimals. */
export function formatUsd(v: number): string {
  if (v === 0) return "$0";
  if (v < 0.01) return USAGE_COPY.lessThanCent;
  return `$${v.toFixed(2)}`;
}

const spend = (cost_usd: number, calls: number): GroupSpendOut =>
  ({ cost_usd, input_tokens: calls * 1000, output_tokens: calls * 400, calls, replayed_calls: 0, unpriced_calls: 0, cache_read_tokens: 0, cache_write_tokens: 0 });

/** A GET /usage answer (typed against the generated UsageOut; the section's tests serve it). */
export const USAGE_FIXTURE: UsageOut = {
  today: { since: "2026-10-09T00:00:00+08:00", total_usd: 0.12,
    by_group: { page: spend(0.1, 2), assistant: spend(0.004, 1), review: spend(0.016, 1) } },
  month: { since: "2026-10-01T00:00:00+08:00", total_usd: 3.4,
    by_group: { page: spend(3, 40), assistant: spend(0.38, 12), review: spend(0.02, 2) } },
  daily_limit_usd: 2,
  monthly_limit_usd: null,
};

/** A GET /usage/settings answer with spending limits and budget confirmation. */
export const SETTINGS_FIXTURE: UsageSettingsOut = {
  budget_confirmed: true,
  daily_limit_usd: 2,
  monthly_limit_usd: null,
};
