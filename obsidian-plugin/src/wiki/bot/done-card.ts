import { localizedAttribute } from "../../i18n/index";
import { tr } from "../../i18n/index";
/**
 * The review-finished card on 今天 (app.js `doneCard` / `confetti`): a ring that fills, three stat cells (unit in a small span; wrong answers read as 明日待复习), × to close and, on the first draw, confetti from the ring. Finishing always
 * ends on a win. Celebrations ignore reduced motion (user ruling).
 */
import confettiLib from "canvas-confetti";
import { isFresh, resumeAnimations } from "../../ui/anim";
import { btn, el } from "../../ui/ew";
import { s as svg } from "../../ui/svg";
import type { WikiActions } from "../actions";
import { pendingCount } from "../review-entries";
import type { WikiState } from "../state";
import { TODAY_COPY } from "../today-copy";

type Celebrate = NonNullable<WikiState["bot"]["celebrate"]>;

function ringSvg(): SVGElement {
  return svg("svg", { viewBox: "0 0 64 64", "aria-hidden": "true" },
    svg("circle", { class: "ew-tr", cx: 32, cy: 32, r: 27 }),
    svg("circle", { class: "ew-fg", cx: 32, cy: 32, r: 27, transform: "rotate(-90 32 32)" }),
    svg("path", { class: "ew-ck", d: "M21 33l7.5 7.5L44 25" }));
}

/** How long the ring, check and stats animate after the round finished. */
const DONE_ANIM_MS = 1400;
/** One confetti canvas per celebration: a re-render puts the same canvas back, so the particles run once and keep going. */
const canvases = new WeakMap<object, HTMLCanvasElement>();

/** The card. Pure over the state: it animates while `celebrate.at` is recent (a re-render continues the animations). */
export function renderDoneCard(s: WikiState, a: WikiActions): HTMLDivElement | null {
  const r: Celebrate | null = s.bot.celebrate;
  if (r === null) return null;
  const fresh = isFresh(r.at, DONE_ANIM_MS);
  const c = el("div", "card donec" + (fresh ? " anim" : ""));
  c.dataset.role = "done-card";
  const ring = el("div", "ring");
  ring.append(ringSvg());
  const miss = r.n - r.ok;
  const t = el("div", "dtext");
  t.append(el("h2", "", () => (TODAY_COPY.doneTitle)));
  const hd = el("div", "dhead");
  hd.append(ring, t);
  const streak = s.review.today.kind === "ready" ? s.review.today.value.streak_days : undefined;
  const cells: [string, string, string][] = [[String(r.n), TODAY_COPY.unitCards, TODAY_COPY.doneReviewed],
    miss > 0 ? [String(miss), TODAY_COPY.unitCards, TODAY_COPY.doneTomorrow] : [`${r.ok} / ${r.n}`, "", TODAY_COPY.doneRight]];
  if (streak !== undefined && streak > 0) cells.push([String(streak), TODAY_COPY.unitDays, TODAY_COPY.doneStreak]);
  const st = el("div", "dstats");
  for (const [v, u, l] of cells) {
    const d = el("div");
    const b = el("b", "", v);
    if (u !== "") b.append(el("small", "", u));
    d.append(b, el("span", "", l));
    st.append(d);
  }
  const x = el("button", "dclose", "×");
  x.type = "button";
  localizedAttribute(x, "aria-label", () => (tr("wiki.bot.done-card..1")));
  x.dataset.action = "close-celebrate";
  x.onclick = () => a.closeCelebrate();
  c.append(x, hd, st);
  const today = s.review.today;
  if (today.kind === "ready" && today.value.due > 0 && pendingCount(s) === 0) {
    const acts = el("div", "acts");
    const more = btn(() => (TODAY_COPY.doneMore), "primary", () => a.moreReview());
    more.dataset.action = "review-more";
    acts.append(more);
    c.append(acts);
  }
  let cv = canvases.get(r);
  if (cv === undefined && fresh) {
    cv = el("canvas", "confetti");
    canvases.set(r, cv);
    const own = cv;
    window.setTimeout(() => {
      const ring = own.parentElement?.querySelector<HTMLElement>(".ew-ring");
      if (ring) confetti(own, ring);
    }, 20);
  }
  if (cv !== undefined) c.append(cv);
  if (fresh) resumeAnimations(c, r.at);
  return c;
}

/** The burst, tuned (by simulating both) to travel like the old hand-drawn one: a wide fan up from the ring that falls. */
export const CONFETTI = { particleCount: 36, angle: 90, spread: 138, startVelocity: 10, decay: 0.96, gravity: 1.6, ticks: 90, scalar: 0.9, shapes: ["square" as const] };
/** The delay before the burst (the ring is still filling). */
export const CONFETTI_DELAY_MS = 850;

/**
 * app.js `confetti`: 36 pieces from the ring, starting 850 ms later and fading out over about 1500 ms, drawn by
 * canvas-confetti on this canvas (its animation is paced at 60 fps whatever the screen's refresh rate, and
 * `disableForReducedMotion: false` because celebrations ignore reduced motion by user ruling). Decorative: any failure draws nothing.
 */
export function confetti(cv: HTMLCanvasElement, from: HTMLElement): void {
  if (!cv.isConnected) return;
  const box = cv.getBoundingClientRect();
  const o = from.getBoundingClientRect();
  if (box.width <= 0 || box.height <= 0) return;
  const cs = getComputedStyle(from);
  // canvas-confetti takes hex colours; the theme's --known / --viz-1 / --viz-2 / --shaky are hex
  const colors = ["--known", "--viz-1", "--viz-2", "--shaky"].map((k) => cs.getPropertyValue(k).trim()).filter((c) => /^#[0-9a-f]{3,6}$/i.test(c));
  const x = (o.left - box.left + o.width / 2) / box.width;
  const y = (o.top - box.top + o.height / 2) / box.height;
  window.setTimeout(() => {
    if (!cv.isConnected) return;
    try {
      const fire = confettiLib.create(cv, { resize: true, useWorker: false, disableForReducedMotion: false });
      void Promise.resolve(fire({ ...CONFETTI, origin: { x, y }, ...(colors.length ? { colors } : {}), zIndex: 0 })).catch(() => undefined);
    } catch { /* a canvas without a 2D context (jsdom): no confetti */ }
  }, CONFETTI_DELAY_MS);
}
