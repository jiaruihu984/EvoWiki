/**
 * 个性化 (full-demo app.js `engineView`): `.ew-side-body.ew-pz` with, top to bottom,
 * - 待确认的记忆 (`#pz-conflicts`, a `.ew-card.ew-task`, only while some group of possibly conflicting memories is open, or
 *   the read failed): the title with 「N 组」, one line, each group's items (the text and the "?": where it is written, date
 *   · source, his words), one 处理 (the 助手 settles them one by one); a failed read is one line with 重试;
 * - 待确认的偏好 (`#pz-pending`, only when some rule waits): the "?" in the header line; each rule as an editable textarea, then
 *   设为我的偏好 / 不采用 pills;
 * - 待确认的档案内容 (`#pz-facts`, a `.ew-card.ew-task`, only while a fact he said about himself waits): each one with its "?",
 *   确认 / 取消;
 * - 我 (`#pz-me`, a `.ew-pz-entry`): opens his profile note;
 * - 我的学习偏好 (`#pz-rules`, a `.ew-pz-box`): one `.ew-pz-it` per rule — the text (bold), its tag (我的要求 / AI 优化) and the
 *   "?" (why) — then the foot 「共 N 条」 + 管理偏好;
 * - 活动记录 (`button.ew-pz-entry#pz-log`, collapsed by default): expanded in place, the code changes waiting for review (采用 /
 *   不要), then the rows by day (今天 / 昨天), the older ones under a 「更早」 toggle. A change in effect that can be undone has
 *   撤销; one no longer in effect is struck with 已撤销 / 已取消 (an automatic rollback: struck only). A Bot run's title is a link
 *   to what it is about;
 * - 资料 (a `.ew-pz-box`, sources/render-sources.ts).
 * 管理偏好 replaces all of it (`‹ 个性化` goes back): each rule with 修改 (inline textarea, 保存 / 取消) and 删除, then
 * 「没采用和撤回的」 with 恢复. Reasons live only behind the circled "?".
 */
import { btn, el } from "../ui/ew";
import { type WikiActions, writeBlocked } from "./actions";
import { changeTarget, reviseChangeTip, undoButton } from "./bot/render-changes";
import { runItem } from "./bot/render-runs";
import { WIKI_COPY } from "./copy";
import { type ActivityItem, activityItems, dayOf, type Day, historyRows, pendingRules, shownRules } from "./personal-rules";
import { PERSONAL_COPY as C } from "./personal-copy";
import { qTip } from "./q-tip";
import { CONFLICT_COPY as K } from "./memory-conflicts";
import { factText, pendingFacts } from "./page-waits";
import { failLine } from "./render-common";
import { autoUndone } from "./rsi-changes";
import type { TeachSpecOut } from "./schema";
import { sourcesAddControl, sourcesSection } from "./sources/render-sources";
import type { Load, ResultNote, WikiState } from "./state";

/** Buttons are off while any write is out, offline, unconfigured, or in the demo. */
const blockedFor = (s: WikiState): boolean => writeBlocked(s) || s.demo;

function box(id: string | null, title: string, tip?: HTMLElement): HTMLDivElement {
  const b = el("div", "pz-box");
  if (id !== null) b.id = id;
  const hd = el("div", "pz-hd");
  hd.append(el("h2", "", title));
  if (tip) hd.append(tip);
  b.append(hd);
  return b;
}

/** app.js prefRow: the text (bold, plus `extra` under it), the tag and the "?". */
function prefRow(text: string, tag: string | null, why: readonly string[], extra?: HTMLElement): HTMLDivElement {
  const r = el("div", "pz-it");
  const x = el("div", "pz-t");
  x.append(el("b", "", text));
  if (extra) x.append(extra);
  r.append(x);
  if (tag !== null) r.append(el("span", "pz-tag", tag));
  if (why.length > 0) r.append(qTip(why, () => (C.prefWhy)));
  return r;
}

const ownTag = (own: "user" | "ai"): string => (own === "user" ? C.ownUser : C.ownAi);

/** The spec, or the loading / failure line in its place (offline: nothing; the sidebar's banner says it). */
function specGate(x: HTMLElement, s: WikiState, a: WikiActions): TeachSpecOut | null {
  const p: Load<TeachSpecOut> = s.prefs;
  if (p.kind === "ready") return p.value;
  if (p.kind === "error") { const f = failLine(s, "prefs-error", () => a.loadEngine(), "prefs-reload"); if (f) x.append(f); }
  else x.append(el("div", "pz-it muted", () => (WIKI_COPY.loading)));
  return null;
}

/** app.js's folding entry (活动记录 / 我的学习偏好 / 资料): the bold title (`title`: its own node), the ›; a click toggles. */
function entry(title: HTMLElement, open: boolean, toggle: () => void): HTMLButtonElement {
  const ent = el("button", "pz-entry");
  ent.type = "button";
  ent.setAttribute("aria-expanded", String(open));
  ent.append(title, el("span", "pz-chev", "›"));
  ent.onclick = toggle;
  return ent;
}

/** app.js prefsBox: folded by default; opened, the rules (or 还没有偏好) and 「共 N 条」 + 管理偏好. */
function prefsBox(b: HTMLElement, s: WikiState, a: WikiActions): void {
  const x = el("div", "pz-box");
  x.id = "pz-rules";
  b.append(x);
  const open = s.personal.rules;
  x.append(entry(el("b", "", () => (C.prefsTitle)), open, () => a.personalView({ rules: !open })));
  if (!open) return;
  const spec = specGate(x, s, a);
  if (spec === null) return;
  const rules = shownRules(spec);
  for (const r of rules) x.append(prefRow(r.item.text, ownTag(r.own), r.why));
  if (rules.length === 0) x.append(el("div", "pz-it muted", () => (C.prefsEmpty)));
  const ft = el("div", "pz-foot");
  const m = btn(() => (C.manage), "pz-link", () => a.personalView({ manage: true }));
  m.dataset.action = "prefs-manage";
  ft.append(el("span", "", () => (C.count(rules.length))), m);
  x.append(ft);
}

/** app.js memoriesCard: the groups (read only), one 处理; none open → no card; a failed read → 没能读到，重试 (offline nothing: the sidebar's one banner says it). */
function memoriesCard(b: HTMLElement, s: WikiState, a: WikiActions): void {
  const c = s.conflicts;
  const failed = c.kind === "error" ? failLine(s, "conflicts-failed", () => a.reloadConflicts(), "conflicts-reload") : null;
  if (failed === null && (c.kind !== "ready" || c.value.length === 0)) return;
  const card = el("div", "card task");
  card.id = "pz-conflicts";
  const head = el("div", "thead");
  head.append(el("h2", "", () => (K.title)));
  if (c.kind === "ready") head.append(el("span", "chip", () => (K.groups(c.value.length))));
  card.append(head);
  if (failed !== null) {
    card.append(failed);
    b.append(card);
    return;
  }
  if (c.kind !== "ready") return;
  card.append(el("div", "muted", () => (K.lead)));
  const list = el("div", "pz-mem");
  for (const g of c.value) {
    const grp = el("div", "pz-grp");
    grp.dataset.conflict = g.id;
    for (const x of g.rows) {
      const r = el("div", "pz-it");
      r.append(el("div", "pz-t", x.text), qTip(x.tip, () => (K.tipLabel)));
      grp.append(r);
    }
    list.append(grp);
  }
  card.append(list);
  const acts = el("div", "acts");
  acts.style.justifyContent = "flex-end";
  const go = btn(() => (K.resolve), "primary", () => a.resolveConflicts());
  go.dataset.action = "conflicts-resolve";
  go.disabled = blockedFor(s);
  acts.append(go);
  card.append(acts);
  b.append(card);
}

/** The facts he said about himself that wait for a yes / no: the card 待确认的记忆 has, one row and two pills each. */
function factsCard(b: HTMLElement, s: WikiState, a: WikiActions): void {
  const facts = pendingFacts(s);
  if (facts.length === 0) return;
  const card = el("div", "card task");
  card.id = "pz-facts";
  const head = el("div", "thead");
  head.append(el("h2", "", () => (C.factsTitle)));
  card.append(head);
  const off = blockedFor(s);
  for (const f of facts) {
    const r = el("div", "pz-it");
    r.dataset.wait = f.action_id;
    const t = el("div", "pz-t", factText(f));
    const acts = el("div", "pz-acts");
    const ok = btn(() => (C.factOk), "pz-pill primary", () => a.botDecide(f.action_id, "approve"));
    ok.dataset.action = "bot-approve";
    ok.disabled = off;
    const no = btn(() => (C.factNo), "pz-pill", () => a.botDecide(f.action_id, "reject"));
    no.dataset.action = "bot-reject";
    no.disabled = off;
    acts.append(ok, no);
    t.append(acts);
    r.append(t);
    if (f.why.trim() !== "") r.append(qTip(f.why.trim(), () => (C.factWhy)));
    card.append(r);
  }
  b.append(card);
}

/** 我: his profile note, the same entry as the folding ones. */
function meBox(b: HTMLElement, a: WikiActions): void {
  const x = el("div", "pz-box");
  const ent = entry(el("b", "", () => (C.meTitle)), false, () => a.openProfile());
  ent.id = "pz-me";
  ent.removeAttribute("aria-expanded");
  x.append(ent);
  b.append(x);
}

/** A textarea whose typed text survives a redraw (kept in personal.drafts by `key`, without a redraw of its own). */
function draftArea(s: WikiState, a: WikiActions, key: string, text: string, label: string): HTMLTextAreaElement {
  const ta = el("textarea", "pz-edit");
  ta.rows = 2;
  ta.value = s.personal.drafts[key] ?? text;
  ta.setAttribute("aria-label", label);
  ta.dataset.draft = key;
  ta.oninput = () => a.prefDraft(key, ta.value);
  return ta;
}

function pendingBox(b: HTMLElement, s: WikiState, a: WikiActions): void {
  if (s.prefs.kind !== "ready") return;
  const pending = pendingRules(s.prefs.value);
  if (pending.length === 0) return;
  const x = box("pz-pending", C.pendingTitle, qTip(() => (C.pendingWhy), () => (C.pendingWhyLabel)));
  const off = blockedFor(s);
  for (const q of pending) {
    const ta = draftArea(s, a, `p:${q.item.item_id}`, q.item.text, C.pendingAria);
    const row = el("div", "pz-acts");
    const ok = btn(() => (C.adopt), "pz-pill primary", () => a.prefAdopt(q.item.item_id, ta.value));
    ok.dataset.action = "pref-adopt";
    ok.disabled = off;
    const no = btn(() => (C.reject), "pz-pill", () => a.prefReject(q.item.item_id));
    no.dataset.action = "pref-reject";
    no.disabled = off;
    row.append(ok, no);
    x.append(ta, row);
  }
  b.append(x);
}

/** The result line of a failed 撤销 / 采用 (and its second line). */
function noteLine(n: ResultNote): HTMLDivElement {
  const d = el("div", "muted", n.text);
  d.dataset.role = "rsi-note";
  d.setAttribute("aria-live", "polite");
  if (n.sub !== undefined) d.append(el("div", "", n.sub));
  return d;
}

/** One change of 活动记录 (app.js logItem): its text (a link to its page while in effect), "?", then 撤销 / 采用 · 不要 / a faint word. */
function changeItem(s: WikiState, it: Extract<ActivityItem, { type: "change" }>, a: WikiActions): HTMLDivElement {
  const { card, action } = it;
  const off = blockedFor(s);
  const r = el("div", "pz-it");
  r.dataset.chg = it.key;
  r.dataset.rsiState = card.state;
  const t = el("div", "pz-t" + (card.state === "live" || card.state === "review" ? "" : " off"));
  const go = card.state === "live" ? changeTarget(action, a) : null;
  if (go !== null) {
    const g = el("button", "pz-go pz-plain", card.summary);
    g.type = "button";
    g.dataset.action = "go-change";
    g.onclick = () => go();
    t.append(g);
  } else t.append(card.summary);
  // A rule / ledger change's pages still out of date (rewritten when opened) or whose rewrite stopped retrying.
  for (const [role, line] of [["pages-waiting", card.waiting], ["pages-failed", card.failed]] as const) {
    if (line === null) continue;
    const p = el("div", "muted", line);
    p.dataset.role = role;
    t.append(p);
  }
  r.append(t);
  const why = reviseChangeTip(action, card.why) ?? card.why;
  if (why !== null) r.append(qTip(why, () => (C.changeWhy)));
  const id = card.change.patch_id;
  if (card.state === "review") {
    const acts = el("div", "pz-acts");
    const ok = btn(() => (WIKI_COPY.rsiAdopt), "pz-pill primary", () => a.approvePatch(id));
    ok.dataset.action = "patch-approve";
    ok.disabled = off;
    const no = btn(() => (WIKI_COPY.rsiDiscard), "pz-pill", () => a.discardPatch(id));
    no.dataset.action = "patch-discard";
    no.disabled = off;
    acts.append(ok, no);
    t.append(acts);
  } else if (card.state === "live") {
    if (card.undoable) r.append(undoButton(off, () => a.undoPatch(id)));
  } else if (!autoUndone(card.change)) {
    r.append(el("span", "pz-tag", () => (card.state === "declined" ? C.declined : C.undone)));
  }
  const n = s.rsiNote;
  if (n !== null && n.patchId === id) t.append(noteLine(n.note));
  return r;
}

/** An RSI change waiting for his yes: what it would change, sentence by sentence, then 采用 / 不要 (its reason behind the "?"). */
function waitItem(s: WikiState, it: Extract<ActivityItem, { type: "wait" }>, a: WikiActions): HTMLDivElement {
  const off = blockedFor(s);
  const r = el("div", "pz-it");
  r.dataset.chg = it.key;
  r.dataset.rsiState = "ask";
  const t = el("div", "pz-t", it.action.summary);
  const lines = el("div", "pz-diff");
  for (const d of it.action.diff) {
    if (d.kind === "ctx" || d.text === it.action.summary) continue;
    const ln = el("div", `pz-diff-${d.kind}`, d.text);
    lines.append(ln);
  }
  t.append(lines);
  const acts = el("div", "pz-acts");
  const ok = btn(() => (WIKI_COPY.rsiAdopt), "pz-pill primary", () => a.botDecide(it.action.action_id, "approve"));
  ok.dataset.action = "rsi-approve";
  ok.disabled = off;
  const no = btn(() => (WIKI_COPY.rsiDiscard), "pz-pill", () => a.botDecide(it.action.action_id, "reject"));
  no.dataset.action = "rsi-reject";
  no.disabled = off;
  acts.append(ok, no);
  t.append(acts);
  r.append(t);
  if (it.action.why) r.append(qTip(it.action.why, () => (C.changeWhy)));
  return r;
}

const logItem = (s: WikiState, it: ActivityItem, a: WikiActions): HTMLDivElement =>
  it.type === "change" ? changeItem(s, it, a) : it.type === "wait" ? waitItem(s, it, a) : runItem(s, it.run, a, it.key);

const DAY_WORD: Record<Exclude<Day, "older">, string> = { get today() { return C.today; }, get yesterday() { return C.yesterday; } };

/** One card (app.js logBox): its title row is the toggle; opened, the timeline grows inside the card under it. */
function logBox(b: HTMLElement, s: WikiState, a: WikiActions, now: Date, newTools: number): void {
  const open = s.personal.log;
  const box = el("div", "pz-box pz-logbox");
  b.append(box);
  const { review, items } = activityItems(s);
  // app.js logBox: the red count of the code changes waiting for his review (采用 / 不要) and the tools that went live and he has not seen, on the title's top right
  const ttl = el("b", "pz-ttl", () => (C.logTitle));
  if (review.length + newTools > 0) ttl.append(el("span", "cnt", String(review.length + newTools)));
  const ent = entry(ttl, open, () => a.personalView({ log: !open }));
  ent.id = "pz-log";
  box.append(ent);
  if (!open) return;
  const log = el("div", "pz-log");
  box.append(log);
  const append = (line: HTMLElement | null): void => { if (line) log.append(line); };
  // A read that failed with nothing shown says so (offline: the sidebar's banner does).
  if (s.bot.actions.kind === "error") append(failLine(s, "bot-actions-error", () => a.loadEngine(), "bot-actions-reload", s.busy !== null));
  if (s.bot.recent.kind === "error") append(failLine(s, "runs-error", () => a.loadEngine(), "runs-reload", s.busy !== null));
  if (s.rsi.kind === "error") append(failLine(s, "rsi-error", () => a.reloadRsi(), "rsi-reload", s.busy !== null));
  else if (s.rsi.kind !== "ready") log.append(el("div", "pz-it muted", () => (WIKI_COPY.rsiLoading)));
  const n = s.rsiNote;
  // A result about the list, or about a change no longer drawn, shows first.
  if (n !== null && (n.patchId === null || ![...review, ...items].some((x) => x.type === "change" && x.card.change.patch_id === n.patchId))) log.append(noteLine(n.note));
  for (const x of review) log.append(logItem(s, x, a));
  for (const d of ["today", "yesterday"] as const) {
    const day = items.filter((x) => dayOf(x.at, now) === d);
    if (day.length === 0) continue;
    log.append(el("div", "pz-when", DAY_WORD[d]));
    for (const x of day) log.append(logItem(s, x, a));
  }
  const older = items.filter((x) => dayOf(x.at, now) === "older");
  if (older.length > 0) {
    const m = el("button", "pz-more");
    m.type = "button";
    m.append(el("span", "", () => (C.older)), el("span", "pz-chev", "›"));
    m.setAttribute("aria-expanded", String(s.personal.older));
    m.onclick = () => a.personalView({ older: !s.personal.older });
    log.append(m);
    if (s.personal.older) for (const x of older) log.append(logItem(s, x, a));
  }
  if (review.length + items.length === 0 && s.rsi.kind === "ready") log.append(el("div", "pz-it muted", () => (WIKI_COPY.rsiEmpty)));
}

/** app.js sourcesBox: folded by default; opened, the materials (sources/render-sources.ts) 10px under the entry. */
function sourcesBox(b: HTMLElement, s: WikiState, a: WikiActions): void {
  const x = el("div", "pz-box");
  b.append(x);
  const open = s.personal.sources;
  x.classList.add("ew-src-box");
  x.append(entry(el("b", "", () => (C.sourcesTitle)), open, () => a.personalView({ sources: !open })), sourcesAddControl(s, a));
  if (!open) return;
  const inner = el("div");
  inner.style.marginTop = "10px";
  sourcesSection(inner, s, a);
  x.append(inner);
}

/** 管理偏好 (app.js manageView). */
function manageView(b: HTMLElement, s: WikiState, a: WikiActions): void {
  const back = btn(() => (C.back), "pz-back", () => a.personalView({ manage: false }));
  back.dataset.action = "prefs-back";
  b.append(back);
  const x = box("pz-manage", C.manageTitle);
  b.append(x);
  const spec = specGate(x, s, a);
  if (spec === null) return;
  const off = blockedFor(s);
  const rules = shownRules(spec);
  for (const r of rules) {
    const id = r.item.item_id;
    if (s.personal.edit === id) {
      const w = el("div", "pz-it");
      const t = el("div", "pz-t");
      const ta = draftArea(s, a, `e:${id}`, r.item.text, C.editAria);
      const acts = el("div", "pz-acts");
      const save = btn(() => (C.save), "pz-pill primary", () => a.prefSave(id, ta.value));
      save.dataset.action = "pref-save";
      save.disabled = off;
      const cancel = btn(() => (C.cancel), "pz-pill", () => a.personalView({ edit: null }));
      cancel.dataset.action = "pref-cancel";
      acts.append(save, cancel);
      t.append(ta, acts);
      w.append(t);
      x.append(w);
      continue;
    }
    const ops = el("div", "pz-ops");
    const edit = btn(() => (C.edit), "link", () => { a.prefDraft(`e:${id}`, r.item.text); a.personalView({ edit: id }); });
    edit.dataset.action = "pref-edit";
    const del = btn(() => (C.del), "link", () => a.prefRemove(id));
    del.dataset.action = "pref-delete";
    del.disabled = off;
    ops.append(edit, del);
    const row = prefRow(r.item.text, ownTag(r.own), r.why, ops);
    row.dataset.rule = id;
    x.append(row);
  }
  if (rules.length === 0) x.append(el("div", "pz-it muted", () => (C.prefsEmpty)));
  const hist = historyRows(spec, s.rsi.kind === "ready" ? s.rsi.value : null);
  if (hist.length === 0) return;
  const h = box("pz-history", C.historyTitle);
  for (const row of hist) {
    const it = prefRow(row.text, row.how, row.why.length > 0 ? row.why : [row.how]);
    it.dataset.hist = row.key;
    it.querySelector(".ew-pz-t")?.classList.add("ew-off");
    const r = btn(() => (C.restore), "sm", () => a.prefRestore(row.text));
    r.dataset.action = "pref-restore";
    r.disabled = off;
    it.append(r);
    h.append(it);
  }
  b.append(h);
}

/** The 个性化 tab's own part (the Bot / Prompt / 评估 settings are drawn after it by render-engine.ts). */
export function renderPersonal(b: HTMLElement, s: WikiState, a: WikiActions, now: Date = new Date(), newTools = 0): void {
  if (s.personal.manage) { manageView(b, s, a); return; }
  memoriesCard(b, s, a);
  factsCard(b, s, a);
  pendingBox(b, s, a);
  meBox(b, a);
  prefsBox(b, s, a);
  logBox(b, s, a, now, newTools);
  sourcesBox(b, s, a);
}
