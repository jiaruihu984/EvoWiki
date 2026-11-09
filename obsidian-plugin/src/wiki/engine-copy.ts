import { tr } from "../i18n/index";
/**
 * Words of the 个性化 tab's change rows and wait bars (app.js, verbatim) and the θ_infer rows' notes (GET /wiki/engine returns
 * structured fields; the plugin words them). Status words, 因为：… and 撤销 come from WIKI_COPY (spelled the same there); the
 * preference words are personal-copy.ts.
 */
import type { EngineParamOut } from "./schema";

export const ENGINE_COPY = {
  /** A waiting row's yes button: the action's own verb when known (app.js `c.yes`), else 确认; and no. */
  get logApprove() { return tr("wiki.engine-copy.ENGINE_COPY.logApprove.1"); }, get logApproveMerge() { return tr("wiki.engine-copy.ENGINE_COPY.logApproveMerge.2"); }, get logApproveDelete() { return tr("wiki.engine-copy.ENGINE_COPY.logApproveDelete.3"); }, get logReject() { return tr("wiki.engine-copy.ENGINE_COPY.logReject.4"); },
  /** The notice after an undo, by what the change was (app.js `undoneText`). */
  get undoneRestored() { return tr("wiki.engine-copy.ENGINE_COPY.undoneRestored.5"); },
  get undoneRule() { return tr("wiki.engine-copy.ENGINE_COPY.undoneRule.6"); },
  get undonePath() { return tr("wiki.engine-copy.ENGINE_COPY.undonePath.7"); },
  get undoneGeneric() { return tr("wiki.engine-copy.ENGINE_COPY.undoneGeneric.8"); },
  get paramsTitle() { return tr("wiki.engine-copy.ENGINE_COPY.paramsTitle.9"); },
  get rsiTitle() { return tr("wiki.engine-copy.ENGINE_COPY.rsiTitle.10"); },
  get rsiHelp() { return [tr("wiki.engine-copy.ENGINE_COPY.rsiHelp.11"), tr("wiki.engine-copy.ENGINE_COPY.rsiHelp.12")]; },
  get rsiHelpLabel() { return tr("wiki.engine-copy.ENGINE_COPY.rsiHelpLabel.13"); },
  paramHelpLabel: (label: string): string => tr("wiki.engine-copy.ENGINE_COPY.paramHelpLabel.14", { v0: label }),
} as const;

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** "2026-10-09" → "10月9日"; anything else → null (fail closed: no invented date). */
function monthDay(iso: string | null): string | null {
  const m = iso === null ? null : ISO_DATE.exec(iso);
  return m ? tr("wiki.engine-copy..15", { v0: Number(m[2]), v1: Number(m[3]) }) : null;
}

/** 「默认」 / 「M月D日调过 · 因为<why>」 / 「M月D日调过」 / 「调过」 (a why without a date is shown as 「调过 · 因为<why>」). */
export function engineParamNote(p: Pick<EngineParamOut, "default" | "tuned_at" | "why">): string {
  if (p.default) return tr("wiki.engine-copy..16");
  const day = monthDay(p.tuned_at);
  const why = p.why?.trim() ? tr("wiki.engine-copy.why.17", { v0: p.why.trim() }) : "";
  return tr("wiki.engine-copy..18", { v0: day ?? "", v1: why });
}
