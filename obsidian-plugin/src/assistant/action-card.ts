import { goalLevelLabel } from "../ui/goal-level";
import { labelNode } from "../i18n/index";
import { localizedAttribute, localizedText } from "../i18n/index";
import { tr } from "../i18n/index";
/**
 * The action card in the 助手 thread = the demo's `actCard` (full-demo/src/asst.js 178-194): a `.propcard` with the title, then a
 * draft you can change (drag or Alt + ↑/↓ to reorder, × to drop a page, 加一页) and 确认 / 取消 - or, once the ledger row is done,
 * the pages with their writing status and 撤销. No explanatory sentences; the only texts are the demo's.
 */
import { btn, el } from "../ui/ew";
import { reorderable } from "../ui/reorder";
import { s as svg } from "../ui/svg";
import { cardPhase, cardResult, draftable, LEDGER_STALE, rowWriteLine, type ActionCardState } from "./card-state";
import { ACTION_COPY as T, NOTE_COPY as N, ROW_STATUS, actionWords } from "./copy";
import { GOAL_KIND, REPLACED } from "./goal-chat";
import type { DiffLine } from "../wiki/bot/bot-schema";
import type { WriteOut } from "../wiki/schema";
import { REVISE_COPY as R } from "../wiki/revise/copy";
import { PRACTICE_KIND, REVISE_KIND } from "../wiki/revise/rows";

/** What the card's controls do; the renderer wires them to the controller. */
export interface ActionCardHooks {
  confirm: (actionId: string) => void; cancel: (actionId: string) => void; undo: (actionId: string) => void;
  editRuleText?: (actionId: string, text: string) => void;
  /** `refocus`: the key moved a row: the renderer puts the focus back on that row after the redraw. */
  move: (actionId: string, from: number, to: number, refocus: boolean) => void;
  remove: (actionId: string, k: number) => void;
  /** The 加一页 box: the redraw keeps the focus in it. */
  add: (actionId: string, title: string) => void;
  showPage: (pageId: string) => void;
  showInParams: (actionId: string) => void;
  /** 查看详情: the action's result (cardResult). */
  showResult?: (actionId: string) => void;
  /** 草稿在页面里 ›: open the page in review mode at its first mark (falls back to showPage). */
  showRevise?: (pageId: string) => void;
  /** 重试 of an in-page draft that no longer fits its page (written again for the page as it is now); absent: none. */
  redraft?: (actionId: string) => void;
}

const MARK = { add: "+", del: "−", ctx: "…" } as const;
/** app.js `diffEl`. */
export function diffEl(lines: readonly DiffLine[]): HTMLDivElement {
  const d = el("div", "diff");
  localizedAttribute(d, "aria-label", () => (T.diffLabel));
  for (const ln of lines) {
    const l = el("div", "dl " + ln.kind);
    l.append(el("span", "mk", MARK[ln.kind]), el("span", "", ln.text));
    d.append(l);
  }
  return d;
}

const NODE_SPLIT_KIND = "node_split";
/** A reorder writes nothing, a restyle only marks the pages (they are rewritten when opened): a page row of such a card says something only while that page's run is on, never ✓ 已完成 on pages nobody wrote. */
const PATH_EDIT_KIND = "path_edit";
const noWrite = (kind: string): boolean => kind === PATH_EDIT_KIND || kind === "restyle_pages";
const MEMORY_FORGET_KIND = "memory_forget";
const runOn = (w: WriteOut | undefined): boolean => w !== undefined && ["queued", "planning", "writing", "failed"].includes(w.state);

/** asst.js draftList `a.unit`: a goal's draft lists blocks (块), every other draft pages (页). */
const unitOf = (c: Pick<ActionCardState, "kind">): string => (c.kind === GOAL_KIND ? tr("assistant.action-card.unitOf.1") : c.kind === NODE_SPLIT_KIND ? tr("assistant.action-card.unitOf.2") : tr("assistant.action-card.unitOf.3"));

/** asst.js `draftList`: `div.draft` > `ol.alist.drag` (draggable rows) + `div.row.aadd` (the input and 加上). */
function draftList(c: ActionCardState, h: ActionCardHooks): HTMLElement {
  const d = c.draft;
  const ol = el("ol", "alist drag");
  const id = c.actionId;
  d.forEach((it, k) => {
    const li = el("li");
    li.draggable = true; li.tabIndex = 0; li.dataset.k = String(k);
    localizedAttribute(li, "aria-label", () => (T.rowLabel(k + 1, it.title, unitOf(c))));
    li.append(el("span", "n", String(k + 1).padStart(2, "0")));
    const t = el("div");
    t.append(el("span", "t", it.title));
    if (it.level) t.append(el("span", "tg", () => goalLevelLabel(it.level)));
    if (it.covers) t.append(el("div", "s", it.covers));
    const x = el("button", "x", "×");
    x.type = "button"; localizedAttribute(x, "aria-label", () => (T.dropRow(it.title)));
    x.onclick = () => h.remove(id, k);
    li.append(t, x);
    ol.append(li);
  });
  // drag (SortableJS in pointer mode, no HTML5 drag, so the panel's own drop of files / library text never sees a row) or
  // Alt + ↑ / ↓; the controller's draft is changed and the card drawn again. The instance goes with the list it is on.
  reorderable(ol, { move: (from, to, byKey) => h.move(id, from, to, byKey) });
  const add = el("div", "row aadd");
  const inp = el("input");
  inp.type = "text"; localizedAttribute(inp, "placeholder", () => (T.addPage(unitOf(c)))); localizedAttribute(inp, "aria-label", () => (T.addPage(unitOf(c))));
  const go = (): void => { const v = inp.value.trim(); if (v !== "") h.add(id, v); };
  inp.onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) go(); };
  add.append(inp, btn(() => (T.addGo), "", go));
  const w = el("div", "draft");
  w.append(ol, add);
  return w;
}

/** asst.js `plist`: a finished card's pages: title (a click opens the page), what it covers, the status (no numbers). */
function pageList(c: ActionCardState, h: ActionCardHooks): HTMLElement {
  const ol = el("ol", "alist");
  for (const it of c.items) {
    const li = el("li");
    const t = el("div");
    const pid = it.page_id ?? null;
    if (pid !== null) {
      const b = el("button", "t", it.title);
      b.type = "button"; b.onclick = () => h.showPage(pid);
      t.append(b);
    } else t.append(el("span", "t", it.title));
    if (it.covers) t.append(el("div", "s", it.covers));
    const ws = el("span", "ws");
    // a split point is no page: nothing is written for it, so it has no write status
    if (pid !== null) { ws.dataset.w = pid; if (noWrite(c.kind)) ws.dataset.quiet = "1"; } else if (c.kind !== NODE_SPLIT_KIND && c.kind !== PATH_EDIT_KIND && c.kind !== MEMORY_FORGET_KIND) ws.textContent = rowWriteLine(undefined).text;
    li.append(t, ws);
    ol.append(li);
  }
  return ol;
}

/** asst.js FILE_ICON (a page with its corner folded) and REV_GO (›), drawn as SVG nodes (no markup strings). */
const FILE_ICON = (): SVGElement => svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
  svg("path", { d: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" }), svg("path", { d: "M14 3v5h5" }));
const REV_GO = (): SVGElement => svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, svg("path", { d: "M9 6l6 6-6 6" }));
const icon = (cls: string, draw: () => SVGElement): HTMLElement => { const s = el("span", cls); s.append(draw()); return s; };

/**
 * app.js revActCard: a materials draft in the chat is one line that points at the page (the decision is made there):
 * 草稿在页面里 · 改了 N 节 › (opens the page at the first mark); then 已更新 N 节 · 撤销 (+ 打开这一页 when another page is
 * open), or 改了 N 节的草稿 with 已取消 / 已撤销 / 有新的草稿. While the draft is written: 生成中 with the spinner and 取消.
 * `page`: the page the assistant is on now (null: none).
 */
export function buildReviseCard(c: ActionCardState, h: ActionCardHooks, page: string | null): HTMLElement {
  const r = c.revise, n = r?.n ?? 0, pid = r?.pageId ?? "";
  const phase = cardPhase(c.status);
  if (phase === "wait" && r !== null && r.state === "ready" && !r.newer) {
    const b = el("button", "propcard revmini go");
    b.type = "button";
    b.dataset.action = c.actionId;
    b.onclick = () => (h.showRevise ?? h.showPage)(pid);
    b.append(icon("ri", FILE_ICON), el("span", "rt", () => (R.inPage(n))), icon("rg", REV_GO));
    return b;
  }
  const card = el("div", "propcard revmini");
  card.dataset.action = c.actionId;
  card.append(icon("ri", FILE_ICON));
  if (phase === "wait" && !(r?.newer ?? false) && r?.state !== "failed") {
    const t = el("span", "rt");
    t.append(el("i", "spin"), labelNode(() => (R.writing)));
    // a draft still being written can be called off (the ledger's reject; a run that never ends must not hold the page)
    const ra = el("span", "ra");
    const x = btn(() => (R.cancel), "link", () => h.cancel(c.actionId));
    x.disabled = c.busy !== null;
    x.dataset.op = "reject";
    ra.append(x);
    card.append(t, ra);
  } else if (phase === "live" && !(r?.newer ?? false)) {
    card.append(el("span", "rt", () => (R.live(n))));
    const ra = el("span", "ra");
    if (pid !== "" && page !== pid) ra.append(btn(() => (R.openPage), "link", () => h.showPage(pid)));
    if (c.canUndo) {
      const u = btn(() => (R.undo), "link", () => h.undo(c.actionId));
      u.disabled = c.busy !== null;
      ra.append(u);
    }
    card.append(ra);
  } else {
    card.classList.add("ew-done");
    const what = r?.newer ? R.newer : phase === "rejected" ? R.cancelled : phase === "undone" ? R.undoneState : R.failed;
    card.append(el("span", "rt", () => (phase === "failed" || r?.state === "failed" ? R.failed : R.draft(n))));
    if (what !== R.failed) card.append(el("span", "faint ra", what));
  }
  if (c.error !== "") card.append(el("div", "muted", c.error));
  return card;
}

/** A preference said in the chat, adopted at once (the ledger kind `stated_rule`). */
export const STATED_KIND = "stated_rule";

/** A card drawn as `buildNoteLine` (not while it would wait for a yes, which a stated_rule row never does). */
export const isNoteLine = (c: Pick<ActionCardState, "kind" | "status">): boolean => c.kind === STATED_KIND && cardPhase(c.status) !== "wait";

/**
 * A preference the assistant recorded from the learner's own words is one quiet line, not a card:
 * 记下了：<principle> · 撤销; after an undo 已撤销：<principle>. The title is the server's 「记下了：…」.
 */
export function buildNoteLine(c: ActionCardState, h: ActionCardHooks): HTMLElement {
  const line = el("div", "noteline");
  line.dataset.action = c.actionId;
  const phase = cardPhase(c.status);
  const text = c.title.startsWith(N.done) ? c.title.slice(N.done.length) : c.title;
  if (phase === "live") {
    line.append(el("span", "nt", () => (N.done + text)));
    if (c.canUndo) {
      const u = btn(() => (N.undo), "link", () => h.undo(c.actionId));
      u.disabled = c.busy !== null;
      line.append(u);
    }
  } else {
    line.classList.add("ew-done");
    line.append(el("span", "nt", () => (phase === "undone" ? N.undone + text : c.detail !== "" ? c.detail : N.failed)));
  }
  if (c.error !== "") line.append(el("div", "muted", c.error));
  return line;
}

type SyncCard = HTMLElement & { _sync?: (writes: ReadonlyMap<string, WriteOut>, written?: ReadonlySet<string>) => void };

export function buildActionCard(c: ActionCardState, h: ActionCardHooks, page: string | null = null): HTMLElement {
  if (c.kind === REVISE_KIND) return buildReviseCard(c, h, page);
  if (isNoteLine(c)) return buildNoteLine(c, h);
  const card: SyncCard = el("div", "propcard");
  card.dataset.action = c.actionId;
  const phase = cardPhase(c.status);
  const id = c.actionId;
  const words = actionWords(c.kind, c.title, c.draft.length);
  if (phase === "wait") {
    card.append(el("b", "", () => actionWords(c.kind, c.title, c.draft.length).ask));
    if (words.sub !== "" && c.kind !== "rule") {
      const sub = el("div", "muted subrow", () => actionWords(c.kind, c.title, c.draft.length).sub);
      // a practice draft is already in the page, playable (full-demo app.js actCard `a.prac`): the card points at it, at
      // the end of the 加交互 line, so the buttons keep their row (确认替换 / 取消 / 请重试) in a narrow sidebar
      if (c.kind === PRACTICE_KIND && c.revise?.state === "ready" && c.revise.pageId !== "") {
        const pid = c.revise.pageId;
        const go = btn(() => (R.seeInPage), "link", () => (h.showRevise ?? h.showPage)(pid));
        go.classList.add("ew-seepage");
        go.dataset.op = "see";
        sub.append(go);
      }
      card.append(sub);
    }
    if (draftable(c)) card.append(draftList(c, h));
    else if (c.diff.length > 0) card.append(diffEl(c.diff));
    const r = el("div", "row");
    const ok = btn(words.yes, "primary", () => h.confirm(id));
    const no = btn(words.no, "", () => h.cancel(id));
    ok.disabled = c.busy !== null || (draftable(c) && c.draft.length < (c.kind === NODE_SPLIT_KIND ? 2 : 1)) || (c.kind === "rule" && !c.ruleText?.trim());
    if (c.kind === "rule") {
      const input = el("textarea", "rule-draft");
      input.value = c.ruleText ?? "";
      input.rows = 3;
      localizedAttribute(input, "aria-label", () => tr("assistant.copy.FORGET_COPY.rule.120"));
      input.disabled = c.busy !== null || c.ruleText === null;
      input.oninput = (event) => {
        const text = (event.currentTarget as HTMLTextAreaElement).value;
        h.editRuleText?.(id, text);
        ok.disabled = c.busy !== null || text.trim() === "";
      };
      card.append(input);
    }
    no.disabled = c.busy !== null;
    // the page changed where the draft goes: it cannot go in as it is, so no 确认替换 and no sentence, only 取消 and a
    // quiet 重试 that writes it again for the page as it is now (app.js draftBtns `a.dead`)
    if (c.revise?.stale === true && h.redraft !== undefined) r.append(no, redraftLink(c, h));
    else r.append(ok, no);
    if (c.retry === "approve" || c.retry === "reject" || (c.retry === "redraft" && c.revise?.stale !== true)) r.append(retryLink(c, h));
    card.append(r);
  } else if (phase === "failed" && c.kind === PRACTICE_KIND && h.redraft !== undefined) {
    // its 确认 came after the page moved where it goes: the same quiet 重试 (written again; it then waits again)
    card.append(el("b", "", () => actionWords(c.kind, c.title, c.draft.length).ask));
    const r = el("div", "row");
    r.append(c.retry === "redraft" ? retryLink(c, h) : redraftLink(c, h));
    card.append(r);
  } else if (phase !== "live") {
    // a goal's draft that a newer draft replaced (app.js blocksReply): 已换成新的分法
    card.append(el("b", "", () => actionWords(c.kind, c.title, c.draft.length).ask), el("div", "res", () => phase === "rejected" && c.kind === GOAL_KIND && c.detail === REPLACED ? T.replaced : c.detail === LEDGER_STALE ? tr("assistant.card-state.LEDGER_STALE.1") : T.result(phase, c.detail)));
  } else {
    // Done: head = doing while pages are still written, then did; the head, pill and count update in place (see `paintWrites`).
    const head = el("div", "thead");
    const hb = el("b", "", () => actionWords(c.kind, c.title, c.draft.length).did), chip = el("span", "chip", () => (T.chipDone));
    head.append(hb, chip);
    card.append(head);
    if (c.kind === "rule" && c.ruleText !== null) card.append(el("div", "rule-text", c.ruleText));
    // a goal's blocks are not pages: the done card lists nothing (app.js blocksAct has no items)
    if (c.items.length > 0 && c.kind !== GOAL_KIND) card.append(pageList(c, h));
    // a done row the server says more about (a merge that stopped because an original changed meanwhile)
    if (c.detail !== "") card.append(el("div", "muted", c.detail));
    const foot = el("div", "afoot");
    const cnt = el("span", "faint");
    card._sync = (writes, written) => {
      const items = noWrite(c.kind) ? c.items.filter((i) => i.page_id && runOn(writes.get(i.page_id))) : c.items;
      const rows = items.flatMap((i) => (i.page_id ? [rowWriteLine(writes.get(i.page_id), written === undefined || written.has(i.page_id))] : []));
      const busy = !noWrite(c.kind) && rows.some((r) => r.busy);
      localizedText(hb, () => busy ? actionWords(c.kind, c.title, c.draft.length).doing : actionWords(c.kind, c.title, c.draft.length).did);
      const unwritten = !busy && rows.some((r) => r.key === "unwritten");
      chip.hidden = unwritten;
      localizedText(chip, () => (busy ? T.chipBusy : rows.some((r) => r.key === "failed") ? ROW_STATUS.failed : T.chipDone));
      const total = c.kind === GOAL_KIND || c.kind === NODE_SPLIT_KIND || noWrite(c.kind) ? 0 : items.length;
      const k = items.length - rows.filter((r) => !r.done).length;
      // nothing written and nothing on its way: 已完成 0/n would read as if something were under way
      localizedText(cnt, () => (total > 0 && !(unwritten && k === 0) ? T.counter(k, total) : ""));
    };
    card.dataset.sync = "1";
    card._sync(new Map(), new Set());
    foot.append(cnt);
    const acts = el("span", "aacts");
    // 查看详情 opens what the action made or changed; an action with nothing to show has none
    if (cardResult(c) !== null) {
      const d = btn(() => (T.details), "link", () => (h.showResult ? h.showResult(id) : h.showInParams(id)));
      d.dataset.action = "details";
      acts.append(d);
    }
    if (c.canUndo) {
      const u = btn(() => (T.undo), "link", () => h.undo(id));
      u.disabled = c.busy !== null;
      acts.append(u);
    }
    if (c.retry === "undo") acts.append(retryLink(c, h));
    foot.append(acts);
    card.append(foot);
  }
  // a request that did not go through says nothing but 请重试 (drawn above); a refusal says why
  if (c.error !== "" && c.retry === null) card.append(el("div", "muted", c.error));
  return card;
}

/** app.js draftBtns: the quiet 请重试 after the buttons of a decision that did not go through; it sends the same again. */
function retryLink(c: ActionCardState, h: ActionCardHooks): HTMLElement {
  const op = c.retry, id = c.actionId;
  const r = btn(() => (R.retryDecision), "link", () => (op === "approve" ? h.confirm(id) : op === "reject" ? h.cancel(id)
    : op === "redraft" ? h.redraft?.(id) : h.undo(id)));
  r.classList.add("ew-retry");
  r.dataset.op = "retry";
  r.disabled = c.busy !== null;
  return r;
}

/** app.js draftBtns `a.dead`: the quiet 重试 of a draft that no longer fits its page. */
function redraftLink(c: ActionCardState, h: ActionCardHooks): HTMLElement {
  const r = btn(() => (R.redraft), "link", () => h.redraft?.(c.actionId));
  r.classList.add("ew-retry");
  r.dataset.op = "redraft";
  r.disabled = c.busy !== null;
  return r;
}

/** asst.js `paintWrites`: the status of every `.ew-ws[data-w]` under `root`, and each live card's head / pill / count, in place (no rebuild). */
export function paintWrites(root: ParentNode, writes: ReadonlyMap<string, WriteOut>, written?: ReadonlySet<string>): void {
  for (const ws of root.querySelectorAll<HTMLElement>(".ew-ws[data-w]")) {
    const pid = ws.dataset.w ?? "";
    if (ws.dataset.quiet && !runOn(writes.get(pid))) { ws.dataset.state = ""; ws.replaceChildren(); continue; }
    const line = rowWriteLine(writes.get(pid), written === undefined || written.has(pid));
    if (ws.dataset.state === line.key) continue;
    ws.dataset.state = line.key;
    ws.replaceChildren();
    if (line.pulse) ws.append(document.createElement("i"));
    ws.append(line.text);
  }
  for (const card of root.querySelectorAll<SyncCard>("[data-sync]")) card._sync?.(writes, written);
}
