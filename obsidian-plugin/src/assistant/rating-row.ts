import { localizedAttribute, localizedText } from "../i18n/index";
/**
 * The quiet icon row under a finished answer (app.js `rateRow`): `.ew-arow` > `.ew-rbtns` (复制, 👍, 👎 as `.ew-qtip` >
 * `button.ew-ib` + `span.ew-tipbox[role=tooltip]`), then, while 👎 is on, a light card (`.ew-rpanel`, its caret under the 👎)
 * of the single-select reason chips (`.ew-chips` of `.ew-pill`) and the optional one-line note (`input.ew-rnote`). The
 * tooltips open below the icons (styles.css EXACT block). The row is built once per answer and repainted in place, so a
 * click never re-renders the answer and the note keeps what is being typed. 👍 / 👎 / a chip post at once (a chip keeps the
 * card open and gives the note the focus); the note posts on Enter or when the focus leaves the card, never per keystroke.
 * Once a chip or a note is in and he has left the card (Enter, a click elsewhere), it folds away: the pressed 👎 is the
 * only trace. With `lead` (the line of a question the turn ended on, app.js `rateRow(m, lead)`): no
 * 复制, the thumbs sit at the right end of that line (`.ew-arow.ew-ask` > `.ew-askq` > lead + `.ew-rbtns`, no row of
 * their own) and 👎 offers a question's reasons.
 */
import { el } from "../ui/ew";
import { ewNotice } from "../ui/ew-notice";
import { s } from "../ui/svg";
import { attachTip } from "../wiki/q-tip";
import { RATING_COPY as R } from "./copy";
import type { Rating } from "./turn";
import { ANSWER_CHIPS, QUESTION_CHIPS, RATING_NOTE_MAX, type RatingChip, type RatingValue } from "./types";
import { tr } from "../i18n";
const chipKey: Record<RatingChip, string> = { 不对: "wrong", 没帮上忙: "unhelpful", 太啰嗦: "verbose", 其他: "other", 问过了: "repeated", 我说过不会: "unknown", 问得看不懂: "unclear", 没必要问: "unnecessary" };

/** app.js THUP_ICON / THDN_ICON / COPY_ICON, built as SVG nodes (no markup is parsed). */
const upIcon = (): SVGElement => s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
  s("path", { d: "M7 11v9H4v-9zM7 11l4-7a2 2 0 0 1 2 2v4h5.5a1.5 1.5 0 0 1 1.5 1.8l-1.3 6.5A2 2 0 0 1 16.7 20H7" }));
const downIcon = (): SVGElement => s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
  s("path", { d: "M7 13V4H4v9zM7 13l4 7a2 2 0 0 0 2-2v-4h5.5a1.5 1.5 0 0 0 1.5-1.8l-1.3-6.5A2 2 0 0 0 16.7 4H7" }));
const copyIcon = (): SVGElement => s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
  s("rect", { x: 9, y: 9, width: 11, height: 11, rx: 2 }), s("path", { d: "M5 15V6a2 2 0 0 1 2-2h9" }));

/** How long the copy tooltip says 已复制 (app.js: later(1500)). */
export const COPIED_MS = 1500;

/** The note as the server stores it: whitespace collapsed, clipped. */
const tidy = (note: string): string => note.split(/\s+/).filter((w) => w !== "").join(" ").slice(0, RATING_NOTE_MAX);

let seq = 0;

export class RatingRow {
  readonly root: HTMLDivElement;
  private readonly btns = el("div", "rbtns");
  private readonly thumbs = new Map<RatingValue, HTMLButtonElement>();
  private readonly panel = el("div", "rpanel");
  private readonly chips = el("div", "chips");
  private readonly pills = new Map<RatingChip, HTMLButtonElement>();
  private readonly note: HTMLInputElement;
  private readonly copyTip: HTMLSpanElement | null = null;
  private copyTimer: ReturnType<typeof setTimeout> | null = null;
  private cur: Rating = { value: null, chips: [], note: "" };
  private sig = "";
  private box = true;
  /** The card has folded away (a reason stored and the focus gone), until 👎 is pressed afresh. */
  private folded = false;

  /**
   * `text()`: what 复制 copies (the answer's Markdown). `rate`: the row's next rating (shown and posted by the controller).
   * `lead`: a question's own line, rated in place (no 复制).
   */
  constructor(private readonly text: () => string, private readonly rate: (next: Rating) => void, private readonly lead?: HTMLElement) {
    this.root = el("div", lead ? "arow ask" : "arow");
    if (!lead) {
      const [cw, cb, ct] = this.iconButton(copyIcon(), R.copy, R.copy);
      cb.onclick = () => void this.copy();
      this.copyTip = ct;
      this.btns.append(cw);
    }
    const tip = lead ? R.rateAsk : R.rate;
    for (const [k, icon] of [["up", upIcon()], ["down", downIcon()]] as const) {
      const [w, b] = this.iconButton(icon, tip, tip);
      b.setAttribute("aria-pressed", "false");
      // Clicking the highlighted thumb clears it; the other one switches (a 👍 drops 👎's reason and note).
      b.onclick = (e) => {
        this.rate(this.cur.value === k ? { value: null, chips: [], note: "" } : k === "down" ? { value: "down", chips: [], note: "" } : { value: "up", chips: [], note: "" });
        // A pointer click lets go of the focus (app.js redraws the row, so the 评价回复 tooltip goes once the pointer
        // leaves instead of staying open over the chips); Enter / Space (detail 0) keeps it on the button.
        if (e.detail > 0) b.blur();
      };
      this.thumbs.set(k, b);
      this.btns.append(w);
    }
    const inp = el("input", "rnote");
    for (const k of lead ? QUESTION_CHIPS : ANSWER_CHIPS) {
      const p = el("button", "pill", () => tr(`runtime.chip.${chipKey[k]}`));
      p.type = "button";
      p.setAttribute("aria-pressed", "false");
      // Single-select: another chip replaces it, the chosen one again clears it. The note goes along as typed. The card
      // stays open and the note takes the focus, so a line can still be added (or the focus leaving folds it).
      p.onclick = () => {
        this.rate({ value: "down", chips: this.cur.chips.includes(k) ? [] : [k], note: this.note.value });
        if (this.panel.isConnected) this.note.focus();
      };
      this.pills.set(k, p);
      this.chips.append(p);
    }
    inp.type = "text";
    localizedAttribute(inp, "placeholder", () => (R.notePlaceholder));
    inp.maxLength = RATING_NOTE_MAX;
    inp.onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); this.leave(); } };
    this.note = inp;
    this.panel.append(this.chips, inp);
    // The focus leaving the card (a click elsewhere, Tab out, the window) posts a changed note; with a reason stored the
    // card folds away. Moving between its own chips and the note does neither.
    this.panel.addEventListener("focusout", (e) => {
      const to = e.relatedTarget;
      if (to instanceof Node && this.panel.contains(to)) return;
      // a button pressed elsewhere: folding now moves it from under the mouse and its click is lost (不知道 after 👎)
      if (to instanceof HTMLButtonElement) {
        const once = (): void => { to.removeEventListener("click", once); to.removeEventListener("blur", once); this.leave(); };
        to.addEventListener("click", once); to.addEventListener("blur", once);
        return;
      }
      this.leave();
    });
    if (lead) {
      const q = el("div", "askq");
      q.append(lead, this.btns);
      this.root.append(q);
    } else this.root.append(this.btns);
  }

  /**
   * Shows `r` (no-op when nothing changed). The note box is only written while it is not being typed in. `box` false:
   * 👎's reasons stay folded (a question he has since answered: only the pressed thumb shows in the history).
   */
  paint(r: Rating, box = true): void {
    const sig = JSON.stringify([r.value, r.chips, r.note, box]);
    if (sig === this.sig) return;
    // 👎 freshly on: the card opens empty; a 👎 that comes with a stored reason (a resumed session) stays folded.
    if (r.value === "down" && (this.sig === "" || this.cur.value !== "down")) this.folded = r.chips.length > 0 || r.note !== "";
    this.sig = sig;
    this.box = box;
    this.cur = { value: r.value, chips: [...r.chips], note: r.note };
    this.root.classList.toggle("ew-rated", r.value !== null);
    for (const [k, b] of this.thumbs) b.setAttribute("aria-pressed", String(r.value === k));
    for (const [k, p] of this.pills) p.setAttribute("aria-pressed", String(r.chips.includes(k)));
    if (r.value === "down" && this.note.ownerDocument.activeElement !== this.note) this.note.value = r.note;
    if (r.value !== "down") this.note.value = "";
    this.layout();
  }

  /** The card shows while 👎 is on, until a reason is stored and he has left it (or the question is answered). */
  private layout(): void {
    const open = this.cur.value === "down" && this.box && !this.folded;
    if (open && this.panel.parentElement !== this.root) this.root.append(this.panel);
    if (!open) this.panel.remove();
  }

  /** Enter in the note, or the focus leaving the card: a changed note posts; with a chip or a note in, the card folds. */
  private leave(): void {
    if (this.folded || this.cur.value !== "down") return;
    this.commitNote();
    if (this.cur.chips.length === 0 && tidy(this.note.value) === "") return;
    this.folded = true;
    // the note goes with its card: let go of the focus first so the fold does not leave it on a detached input
    if (this.note.ownerDocument.activeElement === this.note) this.note.blur();
    this.layout();
  }

  /** The note posts when it differs from what is stored (as the server would store it). */
  private commitNote(): void {
    if (this.cur.value !== "down" || tidy(this.note.value) === tidy(this.cur.note)) return;
    this.rate({ value: "down", chips: [...this.cur.chips], note: this.note.value });
  }

  private async copy(): Promise<void> {
    try {
      if (!navigator.clipboard) throw new Error("no clipboard");
      await navigator.clipboard.writeText(this.text());
    } catch (e) {
      console.warn("EvoWiki assistant: could not copy the answer", e);
      ewNotice(R.copyFailed);
      return;
    }
    const tip = this.copyTip;
    if (tip === null) return;
    localizedText(tip, () => (R.copied));
    if (this.copyTimer !== null) clearTimeout(this.copyTimer);
    this.copyTimer = setTimeout(() => { this.copyTimer = null; localizedText(tip, () => (R.copy)); }, COPIED_MS);
  }

  /**
   * app.js: span.qtip > button.ib (icon, aria-label, aria-describedby) + span.tipbox[role=tooltip]; the tip is placed as the
   * circled ?'s is (wiki/q-tip.ts attachTip: floating-ui, flipped / shifted at the pane's edges, one at a time, Esc hides it).
   */
  private iconButton(icon: SVGElement, label: string, tip: string): [HTMLSpanElement, HTMLButtonElement, HTMLSpanElement] {
    const w = el("span", "qtip");
    const b = el("button", "ib");
    b.type = "button";
    b.append(icon);
    b.setAttribute("aria-label", label);
    const t = el("span", "tipbox", tip);
    t.setAttribute("role", "tooltip");
    t.id = `ew-rtip-${++seq}`;
    b.setAttribute("aria-describedby", t.id);
    w.append(b, t);
    attachTip(w, t);
    return [w, b, t];
  }
}
