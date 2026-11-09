import { tr, onLanguageChange } from "../i18n/index";
/**
 * A busy LLM provider (server code `llm_busy`; HTTP 503 with that code on JSON endpoints) and a model request that timed
 * out (`llm_timeout`; HTTP 504): the server sends the text to show, 「请切换模型重试」 / 「请切换模型重试」
 * (llm/providers.py BUSY_TEXT / TIMEOUT_TEXT; they never name the provider, and never say to switch model: there is no
 * model choice). Only those exact lines are shown; anything else falls back to the same line.
 */
import { ApiError } from "./errors";

export let BUSY_FALLBACK = tr("api.busy.BUSY_FALLBACK.1");
onLanguageChange(() => { BUSY_FALLBACK = tr("api.busy.BUSY_FALLBACK.1"); });
export let TIMEOUT_TEXT = tr("api.busy.TIMEOUT_TEXT.2");
onLanguageChange(() => { TIMEOUT_TEXT = tr("api.busy.TIMEOUT_TEXT.2"); });

/** The busy line to show for a server detail. */
export function busyText(): string { return BUSY_FALLBACK; }

/** A JSON endpoint's 503 whose detail is the busy text. */
export function isBusyApiError(e: unknown): e is ApiError {
  return e instanceof ApiError && e.status === 503 && e.code === "llm_busy";
}

/** A JSON endpoint's 504 whose detail is the timeout text. */
export function isTimeoutApiError(e: unknown): e is ApiError {
  return e instanceof ApiError && e.status === 504 && e.code === "llm_timeout";
}
