/** Bot, prompt and assessment sections mounted in the shared Obsidian settings UI. */
import { btn, el } from "../ui/ew";
import { switchButton } from "../ui/settings-rows";
import { type WikiActions, writeBlocked } from "./actions";
import { settingsSection } from "./bot/render-settings";
import { WIKI_COPY } from "./copy";
import { ENGINE_COPY } from "./engine-copy";
import { PROMPT_COPY } from "./prompt-copy";
import { qTip } from "./q-tip";
import { failLine } from "./render-common";
import type { EngineOut, PromptStepOut } from "./schema";
import type { Load, WikiState } from "./state";

export interface EngineSettingsHost {
  state(): WikiState;
  subscribe(fn: () => void): () => void;
  actions: WikiActions;
  load(): void;
}
const append = (x: HTMLElement, line: HTMLElement | null): void => { if (line) x.append(line); };

/** A `.ew-muted` line, with 重试 as a link button when `retry` is given. */
function mutedLine(text: string, retry?: { action: string; run: () => void; disabled: boolean }): HTMLDivElement {
  const d = el("div", "muted");
  d.append(el("span", "", text));
  if (retry) {
    const b = btn(() => (WIKI_COPY.retry), "link", () => retry.run());
    b.dataset.action = retry.action;
    b.disabled = retry.disabled;
    d.append(b);
  }
  return d;
}

/** The params come from GET /wiki/engine: while it is not there the section says why (and offers 重试). */
function engineGate(x: HTMLElement, s: WikiState, a: WikiActions): EngineOut | null {
  const e: Load<EngineOut> = s.engine;
  if (e.kind === "ready") return e.value;
  if (e.kind === "error") append(x, failLine(s, "engine-error", () => a.loadEngine(), "engine-reload"));
  else x.append(mutedLine(WIKI_COPY.loading));
  return null;
}

/** 自动改进讲解和助手: the heading with its ? and the switch (like 开启 Bot); the switch shows only what the server answered. */
function rsiSection(x: HTMLElement, s: WikiState, a: WikiActions): void {
  const heading = el("h2", "row");
  heading.style.justifyContent = "space-between";
  const name = el("span", "row", () => (ENGINE_COPY.rsiTitle));
  name.append(qTip(() => (ENGINE_COPY.rsiHelp), () => (ENGINE_COPY.rsiHelpLabel)));
  heading.append(name);
  x.append(heading);
  const st = s.rsiSwitch;
  if (st.kind === "error") { append(x, failLine(s, "rsi-switch-error", () => a.loadEngine(), "rsi-switch-reload")); return; }
  if (st.kind !== "ready") { x.append(mutedLine(WIKI_COPY.loading)); return; }
  const sw = switchButton(st.value, () => (ENGINE_COPY.rsiTitle), (on) => {
    sw.setAttribute("aria-checked", String(st.value));
    a.setRsiSwitch(on);
  });
  sw.id = "rsi-enabled";
  sw.disabled = writeBlocked(s);
  heading.append(sw);
}

/** 提示词设置: a row per step (「改过」 when the user changed it, 「格式改了」 when its wording may not fit); a row opens its
 * editor; its ? says what the step does (the backend's description, PromptStepOut.description). */
function promptsSection(x: HTMLElement, s: WikiState, a: WikiActions): void {
  x.append(el("h2", "", () => (PROMPT_COPY.title)));
  const row = (st: PromptStepOut): void => {
    const r = el("div", "prow");
    const b = el("button", "pz-go pz-plain");
    b.type = "button";
    b.dataset.prompt = st.step;
    b.append(el("span", "", st.name));
    r.append(b, el("span", "faint", () => (st.edited ? PROMPT_COPY.edited : "")));
    if (st.format_note !== "") { const t = el("span", "faint", () => (PROMPT_COPY.formatChanged)); t.dataset.role = "format-note"; r.append(t); }
    r.onclick = () => a.openPrompt(st.step);
    const tip = qTip(st.description, () => (PROMPT_COPY.helpLabel(st.name)));
    tip.addEventListener("click", (e) => e.stopPropagation());
    r.append(tip);
    x.append(r);
  };
  const p: Load<PromptStepOut[]> = s.prompts;
  if (p.kind === "ready") { for (const st of p.value) row(st); return; }
  if (p.kind === "error") append(x, failLine(s, "prompts-error", () => a.loadEngine(), "prompts-reload"));
  else x.append(mutedLine(WIKI_COPY.loading));
}

/** 掌握程度评估参数: a row per θ_infer path, the name and the value right-aligned, its ? the backend's description. */
function paramsSection(x: HTMLElement, s: WikiState, a: WikiActions): void {
  x.append(el("h2", "", () => (ENGINE_COPY.paramsTitle)));
  const e = engineGate(x, s, a);
  if (e === null) return;
  for (const p of e.params) {
    const r = el("div", "srow");
    r.dataset.param = p.path;
    const l = el("div");
    l.append(el("div", "n", p.label));
    const v = el("div", "row", p.value);
    v.setAttribute("style", "justify-self:end;font-variant-numeric:tabular-nums;font-weight:600");
    v.append(qTip(p.description, () => (ENGINE_COPY.paramHelpLabel(p.label))));
    r.append(l, v);
    x.append(r);
  }
}


export function mountEngineSettings(box: HTMLElement, host: EngineSettingsHost): { dispose(): void } {
  let alive = true;
  let previous: WikiState | null = null;
  const bot = el("section"), rsi = el("section"), prompts = el("section"), params = el("section");
  box.append(bot, rsi, prompts, params);
  const refresh = (): void => {
    if (!alive) return;
    const s = host.state(), p = previous;
    const gateChanged = p === null || s.conn !== p.conn || s.configured !== p.configured || s.busy !== p.busy;
    if (gateChanged || s.bot.settings !== p?.bot.settings) {
      const focused = document.activeElement instanceof HTMLInputElement && bot.contains(document.activeElement) ? document.activeElement : null;
      const id = focused?.id, start = focused?.selectionStart ?? null, end = focused?.selectionEnd ?? null;
      bot.replaceChildren(); settingsSection(bot, s, host.actions);
      const next = id ? bot.querySelector<HTMLInputElement>(`[id="${id}"]`) : null;
      if (next && !next.disabled) { next.focus({ preventScroll: true }); if (start !== null && end !== null) next.setSelectionRange(start, end); }
    }
    if (gateChanged || s.rsiSwitch !== p?.rsiSwitch) { rsi.replaceChildren(); rsiSection(rsi, s, host.actions); }
    if (gateChanged || s.prompts !== p?.prompts) { prompts.replaceChildren(); promptsSection(prompts, s, host.actions); }
    if (gateChanged || s.engine !== p?.engine) { params.replaceChildren(); paramsSection(params, s, host.actions); }
    previous = s;
  };
  refresh();
  const unsubscribe = host.subscribe(refresh);
  host.load();
  return { dispose: () => { alive = false; unsubscribe(); bot.remove(); rsi.remove(); prompts.remove(); params.remove(); } };
}
