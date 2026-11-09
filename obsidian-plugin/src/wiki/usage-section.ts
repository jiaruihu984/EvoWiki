/**
 * 设置 → 用量 (app.js renderSettings): 「今天花了 $x · 本月 $y」, one `.ew-muted` line per purpose group that spent
 * something (「页面：今天 $… · 本月 $…」), then `.ew-srow`s: 每天最多花（美元） / 每月最多花（美元） (text inputs; empty = no
 * limit). Appended to the section the caller made. Two reads on open; one PATCH per
 * change, carrying only the changed field; single flight while it runs; never retried. Saved → notice 已保存。; a
 * failed read or save → a `.ew-muted` line in the section (with 重新读取 for a read) and a notice.
 */
import { btn, el } from "../ui/ew";
import { ewNotice } from "../ui/ew-notice";
import { setStatus, settingRow, statusLine, textInput } from "../ui/settings-rows";
import { ApiError, OfflineError } from "../api/errors";
import { USAGE_COPY, WIKI_COPY } from "./copy";
import { errorText } from "./format";
import { formatUsd, GROUP_LABEL, parseLimit, type UsageClient } from "./usage";
import { PURPOSE_GROUPS, type UsageOut, type UsageSettingsIn, type UsageSettingsOut } from "../generated/service";

export interface UsageSectionDeps { client: UsageClient; configured: () => boolean; confirmed?: () => void; deferConfirmation?: boolean }
type LimitField = "daily_limit_usd" | "monthly_limit_usd";

const limitText = (v: number | null): string => (v === null ? "" : String(v));

export function renderUsageSection(sec: HTMLElement, deps: UsageSectionDeps): { dispose(): void; settled(): Promise<void> } {
  let alive = true;
  let pendingSave: Promise<void> = Promise.resolve();
  let saveError: Error | null = null;
  const body = el("div");
  sec.append(body);
  const handle = { dispose(): void { alive = false; }, async settled(): Promise<void> { await pendingSave; if (saveError) throw saveError; } };
  if (!deps.configured()) {
    body.append(el("div", "muted", () => (USAGE_COPY.needsSettings)));
    return handle;
  }
  const { client } = deps;

  const readFailed = (target: HTMLElement, e: unknown, again: () => void): void => {
    const r = el("div", "row");
    r.append(el("span", "muted", () => (e instanceof OfflineError ? WIKI_COPY.offline : USAGE_COPY.readFailed)), btn(() => (USAGE_COPY.reread), "link", again));
    target.replaceChildren(r);
  };

  const drawSummary = (target: HTMLElement, u: UsageOut): void => {
    const unpriced = Object.values(u.month.by_group).reduce((count, group) => count + group.unpriced_calls, 0);
    const groups = PURPOSE_GROUPS.filter((g) => (u.today.by_group[g]?.cost_usd ?? 0) > 0 || (u.month.by_group[g]?.cost_usd ?? 0) > 0);
    target.replaceChildren(
      el("div", "", () => (USAGE_COPY.totals(formatUsd(u.today.total_usd), formatUsd(u.month.total_usd)))),
      ...(unpriced > 0 ? [el("div", "muted", () => (USAGE_COPY.unpriced(unpriced)))] : []),
      ...(groups.length === 0 && unpriced === 0 ? [el("div", "muted", () => (USAGE_COPY.noSpend))] : groups.map((g) => el("div", "muted", () => (USAGE_COPY.groupLine(GROUP_LABEL[g], formatUsd(u.today.by_group[g]?.cost_usd ?? 0), formatUsd(u.month.by_group[g]?.cost_usd ?? 0)))))),
    );
  };

  const drawForm = (u: UsageOut, first: UsageSettingsOut): void => {
    let settings = first;
    let busy = false;
    const summary = el("div");
    const rows = el("div");
    const status = statusLine();
    const inputs = {} as Record<LimitField, HTMLInputElement>;
    for (const [f, name] of [["daily_limit_usd", USAGE_COPY.daily], ["monthly_limit_usd", USAGE_COPY.monthly]] as const) {
      const i = textInput("", name);
      i.inputMode = "decimal";
      settingRow(rows, name, null, i);
      inputs[f] = i;
    }
    const confirm = btn(() => (USAGE_COPY.confirmBudget), "primary", () => save({ confirm_budget: true }, () => {}));
    const controls = (): (HTMLInputElement | HTMLButtonElement)[] => [...Object.values(inputs), confirm];
    const fill = (s: UsageSettingsOut): void => {
      for (const f of ["daily_limit_usd", "monthly_limit_usd"] as const) {
        inputs[f].value = limitText(s[f]);
        inputs[f].removeAttribute("aria-invalid");
      }
      confirm.hidden = deps.deferConfirmation === true || s.budget_confirmed;
    };

    const refreshUsage = (): void => {
      client.usage().then((v) => { if (alive) drawSummary(summary, v); },
        (e: unknown) => { if (alive) readFailed(summary, e, refreshUsage); });
    };

    /** One PATCH; everything disabled while it runs; on failure the reason and `revert()`, no retry. */
    const save = (patch: UsageSettingsIn, revert: () => void): void => {
      saveError = null;
      busy = true;
      for (const c of controls()) c.disabled = true;
      setStatus(status, USAGE_COPY.saving);
      const done = (): void => { busy = false; for (const c of controls()) c.disabled = false; };
      pendingSave = client.patch(patch).then((s) => {
        if (!alive) return;
        done();
        settings = s;
        fill(s);
        setStatus(status, "");
        ewNotice(USAGE_COPY.saved);
        refreshUsage();
        if (patch.confirm_budget && s.budget_confirmed) deps.confirmed?.();
      }, (e: unknown) => {
        saveError = e instanceof Error ? e : new Error(String(e));
        if (!alive) return;
        done();
        revert();
        const text = e instanceof ApiError ? e.detail : errorText(e);
        setStatus(status, text);
        ewNotice(text);
      });
    };

    for (const f of ["daily_limit_usd", "monthly_limit_usd"] as const) {
      const input = inputs[f];
      const revert = (): void => { input.value = limitText(settings[f]); };
      input.addEventListener("change", () => {
        if (busy) { revert(); return; }
        const v = parseLimit(input.value);
        if (v === undefined) {
          input.setAttribute("aria-invalid", "true");
          setStatus(status, USAGE_COPY.invalidLimit);
          ewNotice(USAGE_COPY.invalidLimit);
          return;
        }
        input.removeAttribute("aria-invalid");
        save(f === "daily_limit_usd" ? { daily_limit_usd: v } : { monthly_limit_usd: v }, revert);
      });
    }

    fill(settings);
    drawSummary(summary, u);
    body.replaceChildren(summary, el("p", "", () => (USAGE_COPY.billingNotice)), rows, confirm, status);
  };

  const load = (): void => {
    body.replaceChildren(el("div", "muted", () => (USAGE_COPY.loading)));
    Promise.all([client.usage(), client.settings()]).then(([u, s]) => { if (alive) drawForm(u, s); },
      (e: unknown) => { if (alive) readFailed(body, e, load); });
  };
  load();
  return handle;
}
