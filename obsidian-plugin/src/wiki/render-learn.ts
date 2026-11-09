import { localizedAttribute } from "../i18n/index";
/**
 * 今天's 你想学什么？: `.ew-card.ew-feature` > `.ew-lhead` (title only),
 * a `.ew-row` (no wrap) with the topic box and the primary 开始 (Enter too), then `.ew-rec`: up to three ways forward
 * from what the user already knows (starmap/frontier.ts; the demo fixture fixes them via state.recommendations), each `<b>name</b><span>reason</span>`. 开始 and a
 * recommendation open the 学点新的 flow with the topic (openLearnNew). K failed → a `.ew-muted` line with 重试 in place of
 * the recommendations. Concept names and reasons are text nodes.
 */
import { btn, el } from "../ui/ew";
import { type WikiActions, writeBlockedReason } from "./actions";
import { failLine } from "./render-common";
import { mutedLine } from "./render-next";
import type { KGridOut } from "./schema";
import { buildStarMap, frontier, type GoalMembership, type Recommendation } from "./starmap";
import type { WikiState } from "./state";
import { TODAY_COPY } from "./today-copy";
import { WIKI_COPY } from "./copy";

const NO_GOALS: GoalMembership = { goals: [], membership: {} };
const cache = new WeakMap<KGridOut, Recommendation[]>();

/** The recommendations for one K read (frontier of the map built from it; cached per read, the map is pure). */
function recommendations(k: KGridOut): Recommendation[] {
  let recs = cache.get(k);
  if (recs === undefined) {
    recs = frontier(buildStarMap(k, NO_GOALS), { max: 3 });
    cache.set(k, recs);
  }
  return recs;
}

export function renderLearn(s: WikiState, a: WikiActions): HTMLDivElement {
  const c = el("div", "card feature");
  c.dataset.section = "learn";
  const hd = el("div", "lhead");
  hd.append(el("h2", "", () => (WIKI_COPY.learnTitle)));
  c.append(hd);
  // a page being written does not hold up a new goal: the goal goes to the chat, the write goes on
  const reason = writeBlockedReason({ ...s, busy: null });
  const r = el("div", "row");
  r.style.flexWrap = "nowrap";
  const i = el("input");
  i.type = "text";
  localizedAttribute(i, "placeholder", () => (TODAY_COPY.learnPlaceholder));
  i.dataset.action = "learn-topic";
  i.value = s.learnTopic;
  const go = (): void => {
    if (reason !== null) return;
    const topic = i.value.trim();
    i.value = ""; a.setLearnTopic("");
    a.openLearnNew(topic);
  };
  const start = btn(() => (WIKI_COPY.learnStart), "primary", go);
  start.dataset.action = "learn-start";
  start.disabled = reason !== null;
  // Local only: the box is never rebuilt while typing (the controller keeps the text without notifying).
  i.addEventListener("input", () => a.setLearnTopic(i.value));
  i.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); go(); } });
  r.append(i, start);
  c.append(r);
  // Offline the start button is off without a line of its own: the sidebar's 未连接到服务器 banner says why.
  if (reason !== null && s.conn !== "offline") c.append(mutedLine(reason, "blocked"));
  const k = s.k;
  if (k.kind === "error") {
    const f = failLine(s, "learn-error", () => a.retryConnection());
    if (f) c.append(f);
  } else if (k.kind === "ready") {
    const recs = s.recommendations ?? recommendations(k.value);
    if (recs.length > 0) {
      const rec = el("div", "rec");
      for (const x of recs) {
        const b = el("button");
        b.type = "button";
        b.dataset.action = "recommend";
        b.dataset.concept = x.conceptId;
        b.disabled = reason !== null;
        b.append(el("b", "", x.name), el("span", "", x.reason));
        b.onclick = () => a.openLearnNew(x.name);
        rec.append(b);
      }
      c.append(rec);
    }
  }
  return c;
}
