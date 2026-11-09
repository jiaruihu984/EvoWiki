/**
 * The reading-assistant panel as one DOM node: the renderer bound to the shared AssistantController (one per plugin,
 * Ruling R8.1) plus the active-note tracking. Mounted by the wiki sidebar's 助手 tab and by the old AssistantView; both
 * show the same conversation. Showing a note sends nothing (a stored session is read once, by the controller).
 * The root is the demo's `.ew-asst`; whoever mounts it puts it inside an element with class `ew`.
 */
import type { Component } from "obsidian";
import { ewNotice } from "../ui/ew-notice";
import type { AssistantController } from "./controller";
import { ASSISTANT_COPY } from "./copy";
import type { AssistantHost } from "./host";
import { AssistantRenderer, type ChipSettings, visible } from "./render";
import type { ActionOut, SuggestionOut } from "../wiki/bot/bot-schema";
import type { ActiveNote } from "./session-store";
import { onLanguageChange } from "../i18n";

/** The Bot's page chips, from main.ts (backed by BotController): the list, the settings gate, a click and a change feed. */
export interface AssistantChips {
  list(pageId: string): SuggestionOut[];
  settings(): ChipSettings | null;
  onChip(s: SuggestionOut): void;
  subscribe(fn: () => void): () => void;
  /** The Bot ledger the action cards share with 个性化: its rows now (null until loaded) and 在个性化里看 (`act:<id>` is the row's key). */
  ledger?: { rows(): ActionOut[] | null; show(actionId: string): void };
}

export class AssistantPanel {
  readonly root: HTMLElement;
  private readonly renderer: AssistantRenderer;
  private unsubs: (() => void)[] = [];
  private opened = false;
  /** Bumped per resolve and on close: only the newest resolve of an open panel is shown. */
  private token = 0;

  /** `owner`: the view that owns rendered Markdown (MarkdownRenderer needs a Component). */
  constructor(private readonly controller: AssistantController, private readonly host: AssistantHost, owner: Component, private readonly chips?: AssistantChips) {
    const ctl = controller;
    this.renderer = new AssistantRenderer({
      send: () => void ctl.send(),
      setDraft: (text) => ctl.setDraft(text),
      sendEdit: (text) => ctl.sendEdit(text),
      removeQuote: () => ctl.removeQuote(),
      retry: (turnId) => void ctl.retry(turnId),
      decide: (turnId, proposalId, d) => void ctl.decide(turnId, proposalId, d),
      undoProposal: (turnId, proposalId) => void ctl.undoProposal(turnId, proposalId),
      newSession: () => void ctl.newSession(),
      reload: () => void ctl.reload(),
      toggleHistory: () => ctl.toggleHistory(),
      searchHistory: (q) => ctl.searchHistory(q),
      reloadHistory: () => void ctl.loadHistory(),
      resume: (id) => void ctl.resume(id),
      openSource: (src) => host.openSource?.(src),
      addFiles: (files) => void ctl.addFiles(files),
      addPastedText: (text) => void ctl.addPastedText(text),
      dropLibrary: (text) => void this.dropLibrary(text),
      removeAttachment: (id) => ctl.removeAttachment(id),
      confirmAction: (id) => void ctl.confirmAction(id),
      editRuleText: (id, text) => ctl.editRuleText(id, text),
      showProposal: (turnId, proposalId) => ctl.showProposal(turnId, proposalId),
      cancelAction: (id) => void ctl.cancelAction(id),
      redraftAction: (id) => void ctl.redraftAction(id),
      undoAction: (id) => void ctl.undoAction(id),
      moveDraft: (id, from, to) => ctl.moveDraft(id, from, to),
      removeDraft: (id, k) => ctl.removeDraft(id, k),
      addDraft: (id, title) => ctl.addDraft(id, title),
      openCitation: (id, page) => ctl.openCitation(id, page),
      citationGone: (id) => ctl.citationGone(id),
      showPage: (pageId) => host.openSource?.({ title: "", page_id: pageId, url: null, segment_id: null }),
      showInParams: (id) => this.chips?.ledger?.show(id),
      // 查看详情: what the action made or changed; an RSI row's result is its row in 个性化 · 活动记录
      showResult: (id) => { if (!ctl.showResult(id)) this.chips?.ledger?.show(id); },
      showRevise: (pageId) => { if (host.openRevise) host.openRevise(pageId); else host.openSource?.({ title: "", page_id: pageId, url: null, segment_id: null }); },
      answerQuiz: (id, choice) => void ctl.answerQuiz(id, choice),
      answerAsk: (callId, text) => void ctl.answerAsk(callId, text),
      markRough: (callId, block, v) => { ctl.markRough(callId, block, v); },
      submitRough: (callId) => void ctl.submitRough(callId),
      goalRun: (id) => ctl.goalRun(id), pickGoal: (id, k) => ctl.pickGoal(id, k), skipGoal: (id) => ctl.skipGoal(id),
      retryGoal: (id) => ctl.retryGoal(id), restartGoal: (id) => ctl.restartGoal(id), stopGoal: (id) => ctl.stopGoal(id), stopTurn: () => void ctl.stopTurn(),
      nextQuiz: (id) => ctl.nextQuiz(id),
      reviewCard: () => host.reviewCard?.() ?? null,
      reviewLive: () => host.reviewLive?.() ?? false,
      rate: (turnId, next) => void ctl.rate(turnId, next),
      toggleModels: (open) => ctl.toggleModels(open),
      openSettings: host.openSettings === undefined ? undefined : () => host.openSettings?.(),
      pickModel: (pick) => void ctl.pickModel(pick),
      openForget: (id) => void ctl.openForget(id), pickForget: (kind, id, on) => ctl.pickForget(kind, id, on),
      closeForget: () => ctl.closeForget(), confirmForget: () => void ctl.confirmForget(),
    }, {
      renderMarkdown: (el, md) => host.renderMarkdown(el, md, owner),
      conceptName: (id) => host.conceptName(id),
    });
    this.root = this.renderer.root;
  }

  /** Starts following the controller and the active note (idempotent). */
  open(): void {
    if (this.opened) return;
    this.opened = true;
    const ctl = this.controller;
    this.paint();
    // The writing status is read only while this panel is on screen.
    this.unsubs = [ctl.observeVisible(() => visible(this.renderer.root)), ctl.subscribe(() => this.paint()), this.host.onActiveChange(() => void this.refresh())];
    this.unsubs.push(onLanguageChange(() => { this.renderer.languageChanged(); this.paint(); }));
    // The Bot's state changed (a chip, a ledger row decided in 个性化): the page chips and the cards follow.
    if (this.chips) this.unsubs.push(this.chips.subscribe(() => { this.syncLedger(); this.paint(); }));
    this.syncLedger();
    void this.refresh();
  }

  private syncLedger(): void {
    const rows = this.chips?.ledger?.rows();
    if (rows) this.controller.syncLedger(rows);
  }

  private paint(): void {
    const st = this.controller.state;
    this.renderer.update(st);
  }

  /** A library file dragged from Obsidian's file explorer: read through the vault, then added like any file. */
  private async dropLibrary(text: string): Promise<void> {
    let got: File | "unreadable" | null = "unreadable";
    try { got = (await this.host.readDropped?.(text)) ?? "unreadable"; } catch { got = "unreadable"; }
    // no vault file (a web link, plain text): into the draft, never dropped silently
    if (got === null) { this.controller.appendToDraft(text); return; }
    if (got === "unreadable") { ewNotice(ASSISTANT_COPY.dropFromComputer); return; }
    await this.controller.addFiles([got]);
  }

  /** Detaches only: a running answer keeps streaming into the controller's conversation. */
  close(): void {
    for (const off of this.unsubs) off();
    this.unsubs = [];
    this.opened = false;
    this.token++;
  }

  private async refresh(): Promise<void> {
    const mine = ++this.token;
    let active: ActiveNote;
    // Fail closed: if the active note cannot be read, no conversation is shown (never the previous note's).
    try { active = await this.host.activeNote(); } catch { active = { kind: "none" }; }
    if (mine !== this.token || !this.opened) return;
    this.controller.show(active);
  }
}
