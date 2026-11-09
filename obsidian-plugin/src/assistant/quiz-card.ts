import { labelNode } from "../i18n/index";
/**
 * The one-question card under 「这个我早会了」: `.check.qz`, one question at a time,
 * the answer is shown right away (the key lights up, a right answer pops and draws its tick), then 下一题 / 看结果; the result is
 * 答对 x / n and each asked point with its band.
 */
import { btn, el } from "../ui/ew";
import { checkSvg } from "../ui/svg";
import { bandOf, pointRight, type QuizState } from "./card-state";
import { QUIZ_COPY as T } from "./copy";

export interface QuizCardHooks {
  /** `choice`: the option index, -1 = 不知道. */
  answer: (quizId: string, choice: number) => void;
  next: (quizId: string) => void;
}

/** app.js `okLine(true)`: the tick draws itself once. */
function okLine(fresh: boolean): HTMLDivElement {
  const d = el("div", fresh ? "okline anim" : "okline");
  d.append(checkSvg(), labelNode(() => (T.right)));
  return d;
}

const left = (b: HTMLButtonElement): HTMLButtonElement => { b.style.alignSelf = "flex-start"; return b; };

export function buildQuizCard(q: QuizState, h: QuizCardHooks): HTMLElement {
  const c = el("div", "check qz");
  c.dataset.quiz = q.quizId;
  const n = q.items.length;
  if (q.i >= n) {
    const ok = q.correctCount ?? q.res.filter((r) => r === true).length;
    c.append(el("div", "h", () => (T.score(ok, n))));
    const ul = el("div", "qres");
    for (const m of q.mastery ?? []) {
      const { color, word } = bandOf(m.band);
      const d = el("div");
      const dot = el("i", "dotw");
      dot.style.background = `var(--${color})`;
      d.append(dot, `${m.name} · ${word}`, el("span", "faint", () => (pointRight(q, m.node) ? T.rightTag : T.wrongTag)));
      ul.append(d);
    }
    c.append(ul);
    return c;
  }
  const x = q.items[q.i]!;
  c.append(el("div", "rv-src", () => (T.counter(q.i + 1, n))), el("div", "stem", x.stem));
  const o = el("div", "opts");
  const answered = q.pick !== null;
  x.options.forEach((t, i) => {
    const b = el("button", "o", `${"ABCDEFGH"[i] ?? String(i + 1)}. ${t}`);
    b.type = "button"; b.disabled = answered || q.busy;
    if (answered) {
      if (i === q.key) { b.classList.add("ew-right"); if (q.fresh) b.classList.add("ew-pop"); }
      else if (i === q.pick) b.classList.add("ew-wrong");
    }
    b.onclick = () => h.answer(q.quizId, i);
    o.append(b);
  });
  const idk = el("button", "o idk", () => (T.idk));
  idk.type = "button"; idk.disabled = answered || q.busy;
  if (q.pick === -1) idk.classList.add("ew-wrong");
  idk.onclick = () => h.answer(q.quizId, -1);
  o.append(idk);
  c.append(o);
  if (answered) {
    if (q.res[q.i] === true) c.append(okLine(q.fresh));
    c.append(left(btn(() => (q.i + 1 < n ? T.next : T.seeResult), "primary", () => h.next(q.quizId))));
  }
  if (q.error !== "") c.append(el("div", "muted", q.error));
  return c;
}
