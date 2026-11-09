/**
 * 设置 → 样板页 (app.js renderSettings): one `.ew-rule` per exemplar (「库里的笔记：…」 /
 * 「Wiki 页面：…」 + 删除 link). Adding one is said in the assistant (照这一页的写法写), not here. Opening reads the spec
 * once (TeachModel.open). The section holds its own TeachModel: a redraw for a new server / user id disposes the old
 * one, so nothing read for the old identity stays on screen or lands later. The model's result line goes to a notice;
 * a failure also stays as a `.ew-muted` line in the section.
 */
import { SETTINGS_COPY, TEACH_COPY } from "../../copy";
import { btn, el } from "../../ui/ew";
import { ewNotice } from "../../ui/ew-notice";
import type { Exemplar } from "../schema";
import type { ResultNote } from "../state";
import { TeachModel, type TeachPort, type TeachState } from "./model";

/** Wiki 页面 / 库里的笔记：<path>. */
export function exemplarMeta(x: Exemplar): string {
  return x.kind === "wiki_page" ? TEACH_COPY.exemplarWikiPage : TEACH_COPY.exemplarVaultNote(x.ref);
}

/** The exemplar's one line (app.js 「库里的笔记：PCA 读书笔记.md」 / 「Wiki 页面：pca（2/4）」). */
export function exemplarLine(x: Exemplar): string {
  return x.kind === "wiki_page" ? `${TEACH_COPY.exemplarWikiPage}：${x.title}` : TEACH_COPY.exemplarVaultNote(x.ref);
}

export function mountExemplarSection(exemplarsSec: HTMLElement, port: TeachPort): { dispose(): void } {
  const exBody = exemplarsSec.appendChild(el("div"));
  // The demo wiki is open: nothing is read or written here (its fake would make every change look saved).
  if (port.demo === true) {
    exBody.replaceChildren(el("div", "muted", () => (SETTINGS_COPY.demoNoEdit)));
    return { dispose: () => undefined };
  }
  const m = new TeachModel(port);

  /** The line, each exemplar with 删除 (or loading / error), and the failure line. */
  const drawExemplars = (s: TeachState): void => {
    const busy = s.busy !== null;
    const out: HTMLElement[] = [];
    const sp = s.spec;
    if (sp.kind === "loading") out.push(el("div", "muted", () => (TEACH_COPY.loading)));
    else if (sp.kind === "error") {
      const r = el("div", "row");
      r.append(el("span", "muted", sp.message));
      if (port.configured()) {
        const b = btn(() => (TEACH_COPY.retry), "link", () => void m.open());
        b.dataset.action = "retry-teach";
        r.append(b);
      }
      out.push(r);
    } else {
      if (sp.spec.exemplars.length === 0) out.push(el("div", "muted", () => (TEACH_COPY.exemplarsEmpty)));
      for (const x of sp.spec.exemplars) {
        const r = el("div", "rule");
        r.dataset.exemplar = x.exemplar_id;
        const del = btn(() => (TEACH_COPY.exemplarRemove), "link", () => { void m.deleteExemplar(x.exemplar_id); });
        del.dataset.action = "delete-exemplar";
        del.disabled = busy;
        r.append(el("div", "", exemplarLine(x)), del);
        out.push(r);
      }
    }
    if (s.note !== null && s.note.tone === "error") out.push(noteLine(s.note));
    exBody.replaceChildren(...out);
  };

  /** Told once each: the model's result line and a failed read. */
  let toldNote: ResultNote | null = null;
  let toldLoad: TeachState["spec"] | null = null;
  const draw = (): void => {
    const s = m.state;
    drawExemplars(s);
    // A failed read (not the setup line: that is no failure) is told once as well.
    if (s.spec.kind === "error" && s.spec !== toldLoad && port.configured()) { toldLoad = s.spec; ewNotice(s.spec.message); }
    if (s.note !== null && s.note !== toldNote) { toldNote = s.note; ewNotice(s.note.sub ? `${s.note.text} ${s.note.sub}` : s.note.text); }
  };
  draw();
  const off = m.subscribe(draw);
  void m.open();
  return { dispose: () => { off(); m.dispose(); } };
}

function noteLine(n: ResultNote): HTMLElement {
  const d = el("div", "muted", n.sub !== undefined ? `${n.text} ${n.sub}` : n.text);
  d.setAttribute("aria-live", "polite");
  d.dataset.note = n.tone;
  return d;
}
