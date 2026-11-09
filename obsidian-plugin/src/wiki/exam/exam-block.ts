/**
 * 真题练习 at the bottom of a wiki note = app.js `examBlock`: `div.ew-examb` with h3 真题练习 + the ? 「来自《X》的往年题」,
 * the quiz (`div.ew-examq`: the EvoUI paged quiz of the page's exam asset, mounted as `asset: <id>` once its JSON is in
 * the vault; its answers go to POST /wiki/assets/{id}/results like any asset block), then one `div.ew-exi` card per other
 * question (exam-model.ts says what each shows). Drawn by the plugin above the 读完了 block in Reading view and Live
 * Preview (end-block-view.ts), never written into the note. Nothing is drawn while there is nothing to show; a failed read
 * keeps what was shown (logged). A self-report turns its question's buttons off until the answer; on 409 / duplicate the server
 * kept an earlier report, so this pick is not shown: the block is read again (今天做过了), no error; any other failure says
 * 没记上，请重试.
 */
import { btn, el } from "../../ui/ew";
import { qTip } from "../q-tip";
import type { ExamSelfOut, PageExamOut } from "../schema";
import {
  EXAM_COPY, type ExamItemUi, type ExamItemView, type ExamResult, examView, freshItemUi, reportOutcome,
} from "./exam-model";

/** What the block calls: WikiController satisfies it. */
export interface ExamSource {
  /** The page's 真题练习; null = not offered here (the demo, not set up). Throws when it cannot be read. */
  pageExam(pageId: string): Promise<PageExamOut | null>;
  examSelf(itemId: string, result: ExamResult): Promise<ExamSelfOut>;
}
/** The current controller (null before the wiki is used) and its change notifications (end-block.ts EndHub). */
export interface ExamHub { readonly current: ExamSource | null; subscribe(fn: () => void): () => void }
export interface ExamPort {
  hub: ExamHub;
  /** Draws the quiz asset `assetId` into `el` (main.ts: fetched into the vault first, then mountEvouiBlock); returns the cleanup. */
  mountQuiz(el: HTMLElement, assetId: string): () => void;
  /** Each question's state for this plugin session (a redrawn note keeps 看答案 and the pressed choice). */
  ui: Map<string, ExamItemUi>;
}

export interface ExamBlock { el: HTMLElement; refresh(): Promise<void>; destroy(): void }

/** Builds the block for `pageId` (empty until refresh() has read it). */
export function createExamBlock(pageId: string, port: ExamPort): ExamBlock {
  const host = el("div");
  host.className = "ew ew-inline evowiki-exam-host";
  host.dataset.page = pageId;
  const b = el("div", "examb");
  let data: PageExamOut | null = null;
  let quizId: string | null = null;
  let quizBox: HTMLElement | null = null;
  let unmountQuiz: (() => void) | null = null;
  let seq = 0;
  let stopped = false;
  /** Questions whose 题目材料 is unfolded (kept across redraws). */
  const opened = new Set<string>();

  const uiOf = (id: string): ExamItemUi => {
    let u = port.ui.get(id);
    if (u === undefined) { u = freshItemUi(); port.ui.set(id, u); }
    return u;
  };

  const dropQuiz = (): void => { unmountQuiz?.(); unmountQuiz = null; quizBox = null; quizId = null; };

  function card(v: ExamItemView): HTMLElement {
    const c = el("div", "exi");
    c.dataset.item = v.id;
    if (v.heading !== "" || v.cite !== "") {
      const h = el("div", "exh");
      if (v.heading !== "") h.append(el("b", "", v.heading));
      if (v.cite !== "") h.append(el("span", "faint", v.cite));
      c.append(h);
    }
    if (v.context !== null) {
      const d = el("details", "exctx");
      d.open = opened.has(v.id);
      d.addEventListener("toggle", () => { if (d.open) opened.add(v.id); else opened.delete(v.id); });
      d.append(el("summary", "", () => (EXAM_COPY.context)), el("div", "", v.context));
      c.append(d);
    }
    c.append(el("div", "stem", v.stem));
    if (v.options.length > 0) {
      const o = el("div", "exopts");
      for (const x of v.options) o.append(el("div", "", x));
      c.append(o);
    }
    if (v.answer !== null) {
      const a = el("div", "exans");
      a.append(el("div", "", () => (EXAM_COPY.answer(v.answer!))));
      if (v.explanation !== null) a.append(el("div", "exwhy", v.explanation));
      c.append(a);
    }
    const row = el("div", "row");
    if (v.reveal) {
      const s = btn(() => (EXAM_COPY.show), "round", () => { uiOf(v.id).shown = true; draw(); });
      s.dataset.k = "show";
      row.append(s);
    }
    for (const x of v.buttons) {
      const result = x.result;
      const k = btn(x.label, "round", result === null ? null : () => { void report(v.id, result); });
      k.dataset.k = result ?? "today";
      k.disabled = x.disabled;
      if (x.pressed) k.setAttribute("aria-pressed", "true");
      row.append(k);
    }
    c.append(row);
    if (v.error !== null) {
      const e = el("div", "err", v.error);
      e.setAttribute("role", "alert");
      c.append(e);
    }
    return c;
  }

  function draw(): void {
    if (stopped) return;
    const v = examView(data, uiOf);
    if (v === null) { dropQuiz(); host.replaceChildren(); return; }
    if (b.parentElement !== host) host.replaceChildren(b);
    const head = el("div", "thead");
    head.append(el("h3", "", () => (EXAM_COPY.title)));
    if (v.tip !== null) head.append(qTip(v.tip, () => (EXAM_COPY.sourcesLabel)));
    if (v.assetId !== quizId) {
      dropQuiz();
      quizId = v.assetId;
      if (quizId !== null) { quizBox = el("div", "examq"); unmountQuiz = port.mountQuiz(quizBox, quizId); }
    }
    // the quiz box is moved, not rebuilt: an answered question stays answered
    b.replaceChildren(head, ...(quizBox === null ? [] : [quizBox]), ...v.items.map(card));
  }

  async function report(itemId: string, result: ExamResult): Promise<void> {
    const u = uiOf(itemId);
    const src = port.hub.current;
    if (u.sending !== null || u.pick !== null) return;
    if (src === null) { u.failed = true; draw(); return; }
    u.sending = result; u.failed = false;
    draw();
    let r: { ok: true; status: ExamSelfOut["status"] } | { ok: false; error: unknown };
    try { r = { ok: true, status: (await src.examSelf(itemId, result)).status }; } catch (error) { r = { ok: false, error }; }
    u.sending = null;
    const outcome = reportOutcome(r);
    if (outcome !== "failed") {
      if (outcome === "recorded") u.pick = result;
      draw();
      await refresh();
      return;
    }
    if (!r.ok) console.warn("EvoWiki: exam self-report not recorded", r.error);
    u.failed = true;
    draw();
  }

  async function refresh(): Promise<void> {
    const src = port.hub.current;
    const mine = ++seq;
    if (src === null) { data = null; draw(); return; }
    try {
      const got = await src.pageExam(pageId);
      if (stopped || mine !== seq) return;
      data = got;
      // The server's done is per (local) day: a pick from an earlier day, or one it no longer has, is dropped.
      for (const it of got?.open_items ?? []) {
        const u = port.ui.get(it.item_id);
        if (u !== undefined && !it.done && u.sending === null && u.pick !== null) u.pick = null;
      }
    } catch (e) {
      if (stopped || mine !== seq) return;
      console.warn("EvoWiki: 真题练习 not read; keeping what is shown", e);
    }
    draw();
  }

  return { el: host, refresh, destroy: () => { stopped = true; dropQuiz(); host.remove(); } };
}

/** Puts the block into `parent` (before `before`) and reads it; read again when the controller changes. Returns the cleanup. */
export function mountExamBlock(parent: HTMLElement, pageId: string, port: ExamPort, before: Node | null = null): () => void {
  const block = createExamBlock(pageId, port);
  parent.insertBefore(block.el, before);
  void block.refresh();
  let seen = port.hub.current;
  const unsub = port.hub.subscribe(() => {
    if (port.hub.current === seen) return;
    seen = port.hub.current;
    void block.refresh();
  });
  return () => { unsub(); block.destroy(); };
}

/**
 * The quiz asset for the block: `fetchAsset` makes sure `<notes>/Wiki/assets/<id>.json` is in the vault (a failure is logged;
 * the evoui block then offers 取回 itself), then `mount` draws `asset: <id>` into `el`. Returns the cleanup.
 */
export function mountQuizAsset(el: HTMLElement, assetId: string, fetchAsset: (assetId: string) => Promise<unknown>,
  mount: (el: HTMLElement, source: string) => () => void): () => void {
  let stopped = false;
  let off: (() => void) | null = null;
  const go = (): void => { if (!stopped) off = mount(el, `asset: ${assetId}`); };
  let pending: Promise<unknown>;
  try { pending = fetchAsset(assetId); } catch (e) { pending = Promise.reject(e instanceof Error ? e : new Error(String(e))); }
  void pending.catch((e: unknown) => { console.warn("EvoWiki: exam quiz asset not fetched", e); }).then(go);
  return () => { stopped = true; off?.(); };
}
