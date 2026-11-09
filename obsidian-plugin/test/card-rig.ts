import { vi } from "vitest";
import { AssistantController, type AssistantDeps } from "../src/assistant/controller";
import { FakeAssistantApi } from "../src/assistant/fake-api";
import { AssistantRenderer, type AssistantActions } from "../src/assistant/render";
import type { AssistantSessionStore, Target } from "../src/assistant/session-store";
import type { AssistantEvent } from "../src/assistant/types";

/** The card / quiz controls of AssistantActions as spies (renderer tests that do not look at them). */
export const cardActions = () => ({
  confirmAction: vi.fn(), cancelAction: vi.fn(), undoAction: vi.fn(), moveDraft: vi.fn(), removeDraft: vi.fn(), addDraft: vi.fn(),
  editRuleText: vi.fn(), showProposal: vi.fn(),
  showPage: vi.fn(), showInParams: vi.fn(), answerQuiz: vi.fn(), nextQuiz: vi.fn(), rate: vi.fn(),
}) satisfies Partial<AssistantActions>;

export const PAGE: Target = { kind: "page", pageId: "page_pca", title: "主成分分析" };
export const ID = { server: "http://127.0.0.1:8000", user: "jerry" };

/**
 * A renderer wired to a real controller over the fake API, attached to the document (focus works): what the card tests drive.
 * `ask(events)` runs one turn whose stream is `events` (a `done` is appended).
 */
export function cardRig(extra: { showPage?: (id: string) => void; showInParams?: (id: string) => void; goals?: AssistantDeps["goals"];
  store?: AssistantSessionStore; api?: FakeAssistantApi; decisions?: AssistantDeps["decisions"]; results?: AssistantDeps["results"] } = {}) {
  const api = extra.api ?? new FakeAssistantApi();
  const store: AssistantSessionStore = extra.store ?? { get: () => null, set: () => Promise.resolve() };
  let n = 0;
  const ctl = new AssistantController({ api, store, identity: () => ID, newId: () => `t${++n}`, ...(extra.goals ? { goals: extra.goals } : {}),
    ...(extra.decisions ? { decisions: extra.decisions } : {}), ...(extra.results ? { results: extra.results } : {}) });
  const spies = { showPage: extra.showPage ?? vi.fn(), showInParams: extra.showInParams ?? vi.fn() };
  const actions: AssistantActions = {
    send: () => void ctl.send(), sendEdit: (t) => ctl.sendEdit(t), setDraft: (t) => ctl.setDraft(t), removeQuote: () => ctl.removeQuote(), retry: (id) => void ctl.retry(id),
    undoProposal: (t, p) => void ctl.undoProposal(t, p),
    decide: (t, p, d) => void ctl.decide(t, p, d), newSession: () => void ctl.newSession(), reload: () => void ctl.reload(),
    toggleHistory: () => ctl.toggleHistory(), searchHistory: (q) => ctl.searchHistory(q), reloadHistory: () => void ctl.loadHistory(),
    resume: (id) => void ctl.resume(id), openSource: () => {},
    confirmAction: (id) => void ctl.confirmAction(id), cancelAction: (id) => void ctl.cancelAction(id),
    editRuleText: (id, text) => ctl.editRuleText(id, text),
    showProposal: (turnId, proposalId) => ctl.showProposal(turnId, proposalId),
    redraftAction: (id) => void ctl.redraftAction(id), undoAction: (id) => void ctl.undoAction(id),
    moveDraft: (id, a, b) => ctl.moveDraft(id, a, b), removeDraft: (id, k) => ctl.removeDraft(id, k), addDraft: (id, t) => ctl.addDraft(id, t),
    showPage: spies.showPage, showInParams: spies.showInParams,
    answerQuiz: (id, c) => void ctl.answerQuiz(id, c), nextQuiz: (id) => ctl.nextQuiz(id),
    goalRun: (id) => ctl.goalRun(id), pickGoal: (id, k) => ctl.pickGoal(id, k), skipGoal: (id) => ctl.skipGoal(id), retryGoal: (id) => ctl.retryGoal(id),
    restartGoal: (id) => ctl.restartGoal(id), stopTurn: () => void ctl.stopTurn(),
    rate: (id, next) => void ctl.rate(id, next),
    addFiles: (files) => void ctl.addFiles(files), dropLibrary: () => {}, removeAttachment: (id) => ctl.removeAttachment(id),
  };
  const r = new AssistantRenderer(actions, { renderMarkdown: (el, md) => { el.textContent = md; }, conceptName: (id) => id });
  const host = document.createElement("div");
  host.className = "ew";
  host.append(r.root);
  document.body.append(host);
  ctl.subscribe(() => r.update(ctl.state));
  ctl.show(PAGE);
  r.update(ctl.state);
  const q = <T extends Element = HTMLElement>(sel: string): T | null => r.root.querySelector<T>(sel);
  const all = <T extends Element = HTMLElement>(sel: string): T[] => [...r.root.querySelectorAll<T>(sel)];
  async function ask(events: AssistantEvent[], text = "问一下"): Promise<void> {
    api.messageReplies.push({ events: [...events, { type: "done", message_id: `m${++n}`, tool_calls: 1, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false }] });
    ctl.setDraft(text);
    await ctl.send();
  }
  const dispose = (): void => { ctl.dispose(); host.remove(); };
  return { api, ctl, r, q, all, ask, dispose, spies };
}
