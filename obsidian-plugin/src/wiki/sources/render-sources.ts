import { localizedAttribute, localizedText } from "../../i18n/index";
/**
 * 参数 · 资料 (app.js `sourcesSection` of the approved full demo): `.src-list` holding one `.chg.ci.src[data-src]` card per
 * source: `.ci-top` with the name (or the rename field) and the kind · page count, the status line `.ci-s.src-st` (err /
 * warn), the reading bar, the server's note, and 打开 / 重命名 / 删除 (delete asks first). Materials are added in the
 * assistant (drop a file into the chat), not here. A
 * progress item changes a reading card's status text and bar IN PLACE (updateSourceCard), never the panel; a card that
 * changes state redraws the section. No caption under the heading and no 「用」 buttons.
 */
import { btn, el } from "../../ui/ew";
import { withSettingsLink } from "../../ui/settings-link";
import { s as svg } from "../../ui/svg";
import { type WikiActions, writeBlocked } from "../actions";
import { failLine } from "../render-common";
import type { SourceOut, SourceProgress } from "../schema";
import type { WikiState } from "../state";
import { EXAM_COPY, examLine } from "../exam/exam-model";
import { qTip } from "../q-tip";
import { KIND_LABEL, pageRanges, sourceProgressText, SOURCES_COPY } from "./sources-copy";

/** Same rule as the other 参数 sections: off while a write is out, offline, unconfigured, in the demo. */
const blockedFor = (s: WikiState): boolean => writeBlocked(s) || s.demo;

/** The chapter count of a finished source, from its last progress item (null: unknown). */
function chaptersOf(p: SourceProgress | null | undefined): number | null {
  return p !== null && p !== undefined && p.total > 0 && p.done >= p.total ? p.total : null;
}

/** app.js `srcStatus`. */
export function statusText(x: SourceOut, p: SourceProgress | null | undefined): string {
  if (x.status === "reading") return p ? sourceProgressText(p) : SOURCES_COPY.readingStart;
  if (x.status === "failed") return x.error ?? SOURCES_COPY.failedFallback;
  if (x.status === "partial") return x.unreadable_pages.length > 0 ? SOURCES_COPY.unreadable(pageRanges(x.unreadable_pages)) : SOURCES_COPY.partialFallback;
  return SOURCES_COPY.ready(chaptersOf(p));
}

/** The latest progress known for a source: the live one, else the one the list carried. */
export const progressOf = (s: WikiState, x: SourceOut): SourceProgress | null => s.sourceProgress[x.id] ?? x.progress;

/** The reading bar's width (a rounded percent). */
const percent = (p: SourceProgress | null | undefined): string => `${p === null || p === undefined || p.total <= 0 ? 0 : Math.round((p.done / p.total) * 100)}%`;

/**
 * A progress item for a reading card already on screen: only its status text and bar change (spec 2.2). Returns false when
 * the card is not there or is not a reading card (the caller then leaves it to the next redraw).
 */
export function updateSourceCard(root: ParentNode, x: SourceOut, p: SourceProgress | null | undefined): boolean {
  const c = [...root.querySelectorAll<HTMLElement>("[data-src]")].find((e) => e.dataset.src === x.id);
  if (c === undefined) return false;
  const st = c.querySelector(".ew-src-st span");
  const bar = c.querySelector<HTMLElement>(".ew-rvbar i");
  if (st === null || bar === null) return false;
  localizedText(st, () => statusText(x, p));
  bar.style.width = percent(p);
  return true;
}

/** The goals a source was used for (「用在：」): only those whose title the client already has (the catalog's big goals by map id, and paths). */
function usedTitles(s: WikiState, x: SourceOut): string[] {
  const paths = s.catalog.kind === "ready" ? s.catalog.value.paths ?? [] : [];
  const maps = s.catalog.kind === "ready" ? s.catalog.value.maps ?? [] : [];
  const out: string[] = [];
  for (const u of x.uses) {
    const t = u.goal_id === null ? undefined
      : maps.find((m) => m.map_id === u.goal_id)?.goal ?? paths.find((p) => p.path_id === u.goal_id)?.goal;
    if (t !== undefined && t !== "" && !out.includes(t)) out.push(t);
  }
  return out;
}

function card(s: WikiState, x: SourceOut, a: WikiActions, off: boolean): HTMLDivElement {
  const ui = s.sourceUi;
  const p = progressOf(s, x);
  const c = el("div", "chg ci src");
  c.dataset.src = x.id;
  const hd = el("div", "ci-top");
  if (ui.rename === x.id) {
    const i = el("input");
    i.type = "text";
    i.value = ui.renameDraft;
    i.id = "src-rename";
    localizedAttribute(i, "aria-label", () => (SOURCES_COPY.renameLabel));
    i.style.flex = "1";
    i.addEventListener("input", () => a.sourceSetRenameDraft(i.value));
    i.addEventListener("keydown", (e) => {
      if (e.key === "Enter") a.sourceCommitRename();
      if (e.key === "Escape") a.sourceCancelRename();
    });
    const save = btn(() => (SOURCES_COPY.save), "sm primary", () => a.sourceCommitRename());
    save.disabled = off;
    hd.append(i, save);
    window.setTimeout(() => { if (i.isConnected && (document.activeElement === null || document.activeElement === document.body)) i.focus(); }, 20);
  } else {
    const label = KIND_LABEL[x.kind];
    const t = el("div", "ci-t src-t", x.title);
    t.append(" ", el("span", "faint", () => (x.pages ? `${label} · ${SOURCES_COPY.pages(x.pages)}` : label)));
    hd.append(t);
  }
  c.append(hd);
  const sl = el("div", "ci-s src-st" + (x.status === "failed" ? " err" : x.status === "partial" ? " warn" : ""));
  const said = el("span");
  if (x.status === "reading") localizedText(said, () => statusText(x, p));
  else said.append(...withSettingsLink(statusText(x, p), () => a.openSettings(), x.error_code === "budget_exceeded"));
  sl.append(said);
  c.append(sl);
  if (x.status === "reading") {
    const b = el("div", "rvbar");
    const f = el("i");
    f.style.width = percent(p);
    b.append(f);
    c.append(b);
  }
  // 真题: 正在找题 / 抽出 N 道题 · M 道没挂上 · K 道读不清 (which ones and why in the ?) / the failure
  const ex = examLine(s.sourceExam[x.id]);
  if (ex !== null) {
    const d = el("div", "ci-s src-exam" + (ex.failed ? " err" : ""));
    d.dataset.role = "src-exam";
    d.append(el("span", "", ex.text));
    if (ex.tip.length > 0) d.append(qTip(ex.tip, () => (EXAM_COPY.lineTipLabel)));
    c.append(d);
  }
  const used = usedTitles(s, x);
  if (used.length > 0) c.append(el("div", "ci-s", () => (SOURCES_COPY.usedIn + used.join("、"))));
  // Late errors and notes from the server (SourceOut.note) are shown, never dropped.
  if (x.note !== null && x.note.trim() !== "" && x.note !== x.error) {
    const n = el("div", "ci-s");
    n.append(...withSettingsLink(x.note, () => a.openSettings(), x.error_code === "budget_exceeded"));
    n.dataset.role = "src-note";
    c.append(n);
  }
  const note = ui.notes[x.id];
  if (note !== undefined) {
    const n = el("div", "ci-s");
    n.dataset.role = "src-line";
    n.append(el("span", "", note.text));
    if (note.retry) {
      const r = btn(() => (SOURCES_COPY.retry), "link", () => a.sourceRetryFollow(x.id));
      r.dataset.action = "src-retry";
      n.append(r);
    }
    c.append(n);
  }
  if (ui.confirm === x.id) {
    c.append(el("div", "ci-s src-warn", () => (SOURCES_COPY.confirmDelete)));
    const r = el("div", "row");
    const yes = btn(() => (SOURCES_COPY.confirmYes), "sm primary", () => a.sourceConfirmDelete(x.id));
    yes.disabled = off;
    r.append(yes, btn(() => (SOURCES_COPY.cancel), "sm", () => a.sourceCancelDelete()));
    c.append(r);
  } else {
    const r = el("div", "row");
    const ren = btn(() => (SOURCES_COPY.rename), "sm", () => a.sourceStartRename(x.id));
    const del = btn(() => (SOURCES_COPY.remove), "sm", () => a.sourceAskDelete(x.id));
    ren.disabled = off;
    del.disabled = off;
    if (x.can_retry) {
      const again = btn(() => (SOURCES_COPY.retry), "sm primary", () => a.sourceRetryImport(x.id));
      again.disabled = off;
      r.append(again);
    }
    r.append(btn(() => (SOURCES_COPY.open), "sm", () => a.sourceOpen(x.id)), ren, del);
    c.append(r);
  }
  return c;
}

/** The small + after the title 资料, beside the fold's button (so it never folds): pick files, also while folded. */
export function sourcesAddControl(s: WikiState, a: WikiActions): HTMLElement {
  const pick = document.createElement("input");      // never in the page: only its file dialog is seen
  pick.type = "file";
  pick.multiple = true;
  pick.onchange = () => { const files = [...(pick.files ?? [])]; pick.value = ""; if (files.length > 0) a.sourcesAdd(files); };
  const plus = el("button", "src-add");
  plus.type = "button";
  localizedAttribute(plus, "title", () => (SOURCES_COPY.add));
  localizedAttribute(plus, "aria-label", () => (SOURCES_COPY.add));
  plus.dataset.action = "src-add";
  plus.disabled = blockedFor(s);
  plus.append(svg("svg", { viewBox: "0 0 12 12", width: 12, height: 12, "aria-hidden": "true" },
    svg("path", { d: "M6 1.5v9M1.5 6h9", fill: "none", stroke: "currentColor", "stroke-width": 1.4, "stroke-linecap": "round" })));
  plus.onclick = () => pick.click();
  return plus;
}

export function sourcesSection(x: HTMLElement, s: WikiState, a: WikiActions): void {
  x.id = "sources";
  const off = blockedFor(s);
  const zone = el("div", "src-list");
  const wanted = (e: DragEvent): boolean => !off && [...(e.dataTransfer?.types ?? [])].includes("Files");
  zone.ondragover = (e) => { if (wanted(e)) { e.preventDefault(); zone.classList.add("ew-drag"); } };
  zone.ondragleave = (e) => { if (!zone.contains(e.relatedTarget as Node | null)) zone.classList.remove("ew-drag"); };
  zone.ondrop = (e) => {
    if (!wanted(e)) return;
    e.preventDefault();
    e.stopPropagation();
    zone.classList.remove("ew-drag");
    a.sourcesAdd([...(e.dataTransfer?.files ?? [])]);
  };
  const list = s.sources;
  if (list.kind === "idle" || list.kind === "loading") {
    zone.append(el("div", "muted", () => (SOURCES_COPY.loading)));
  } else if (list.kind === "error") {
    const f = failLine(s, "sources-error", () => a.sourcesReload(), "sources-reload");
    if (f !== null) zone.append(f);
  } else if (list.value.length === 0) {
    zone.append(el("div", "muted src-empty", () => (SOURCES_COPY.empty)));
  } else {
    for (const it of list.value) zone.append(card(s, it, a, off));
  }
  x.append(zone);
}
