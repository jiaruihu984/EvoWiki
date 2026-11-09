import { tr } from "../i18n/index";
import { localizedAttribute } from "../i18n/index";
/**
 * 学点新的's diagnosis in the chat (asst.js roughCard / diagCard, the approved 掌握度 design):
 * - 大体摸底 (pause kind `ask_rough`, payload {draft_id, blocks: [{id, name}]}): one grid, a row per block, 掌握 / 熟悉 /
 *   不会 one tap each (`.ew-dg-mat` of `.ew-dg-row`, `.ew-dg-cell`), 提交 once a block is marked (the rest: no statement); each tap colours its
 *   block in 学点新的 at once. Sent: his message is the readable line (「分类与鉴别 掌握、水质与环境 不会」) with
 *   `reply_data {marks}`. Once sent: a row of squares in the blocks' colours and 已提交 (`.ew-dg-mini`).
 * - 细问 (pause kind `ask_check` with `block_id` / `block_name`): the block (a square + its name), the question, its options
 *   (不知道 last, quieter) as thick buttons; an option is sent as his message. 👍 / 👎 under it are the chat's own rating row
 *   (RatingRow, the compact reason card), passed in by the renderer.
 */
import { el } from "../ui/ew";
import type { RoughMark } from "../wiki/learn/learn-view";

export const ROUGH_KIND = "ask_rough";
export const CHECK_KIND = "ask_check";

export const DIAG_COPY = {
  /** The grid's columns, as the 知识图谱 says them. */
  get cols() { return [["know", tr("assistant.diag-cards.DIAG_COPY.cols.1")], ["partly", tr("assistant.diag-cards.DIAG_COPY.cols.2")], ["dont", tr("assistant.diag-cards.DIAG_COPY.cols.3")]] as const; },
  get submit() { return tr("assistant.diag-cards.DIAG_COPY.submit.4"); },
  get submitted() { return tr("assistant.diag-cards.DIAG_COPY.submitted.5"); },
  get notRecorded() { return tr("assistant.diag-cards.DIAG_COPY.notRecorded.6"); },
  get sending() { return tr("assistant.diag-cards.DIAG_COPY.sending.7"); },
  get idk() { return tr("assistant.diag-cards.DIAG_COPY.idk.8"); },
  cellLabel: (block: string, word: string) => `${block}：${word}`,
};
const WORD: Record<RoughMark, string> = { get know() { return tr("assistant.diag-cards.WORD.know.9"); }, get partly() { return tr("assistant.diag-cards.WORD.partly.10"); }, get dont() { return tr("assistant.diag-cards.WORD.dont.11"); } };
const SQ: Record<RoughMark, string> = { know: "know", partly: "some", dont: "gap" };

export interface RoughBlock { id: string; name: string }
/** The grid as the card shows it: its blocks, his taps so far, whether it was sent. */
export interface RoughState { blocks: RoughBlock[]; marks: Record<string, RoughMark>; sent: boolean; outcome?: "sending" | "failed" }

/** An ask_rough payload's blocks ([] when it has none it can show). */
export function roughBlocks(payload: Record<string, unknown>): RoughBlock[] {
  const b = payload.blocks;
  if (!Array.isArray(b)) return [];
  return b.flatMap((x): RoughBlock[] => {
    if (x === null || typeof x !== "object") return [];
    const { id, name } = x as { id?: unknown; name?: unknown };
    return typeof id === "string" && id !== "" && typeof name === "string" ? [{ id, name }] : [];
  });
}

/** His message for the grid: each marked block with its word, in the grid's order (「分类与鉴别 掌握、水质与环境 不会」). */
export function roughText(st: RoughState): string {
  return st.blocks.filter((b) => st.marks[b.id] !== undefined).map((b) => `${b.name} ${WORD[st.marks[b.id]!]}`).join("、");
}

/** The grid can be sent once every block is marked. */
export const roughReady = (st: RoughState): boolean => st.blocks.some((b) => st.marks[b.id] !== undefined);

export interface RoughHooks { mark(id: string, v: RoughMark): void; submit(): void }

export function buildRoughCard(st: RoughState, h: RoughHooks, live: boolean): HTMLElement {
  if (st.sent || !live) {
    // he answered in words, or the marks are not kept after a reload: his message says what he said, no empty squares
    if (Object.keys(st.marks).length === 0) return el("span");
    const s = el("div", "dg-mini");
    for (const b of st.blocks) {
      const m = st.marks[b.id];
      const q = el("span", "dg-sq" + (m !== undefined ? " " + SQ[m] : ""));
      q.title = b.name;
      s.append(q);
    }
    s.append(el("span", "dg-mt", () => (st.outcome === "failed" ? DIAG_COPY.notRecorded : st.outcome === "sending" ? DIAG_COPY.sending : DIAG_COPY.submitted)));
    return s;
  }
  const c = el("div", "dg-mat"), hd = el("div", "dg-row hd");
  hd.append(el("span"));
  for (const [, t] of DIAG_COPY.cols) hd.append(el("span", null, t));
  c.append(hd);
  for (const b of st.blocks) {
    const r = el("div", "dg-row");
    r.append(el("span", null, b.name));
    for (const [v, t] of DIAG_COPY.cols) {
      const on = st.marks[b.id] === v;
      const p = el("button", "dg-cell" + (on ? " " + SQ[v] : ""));
      p.type = "button";
      localizedAttribute(p, "aria-label", () => (DIAG_COPY.cellLabel(b.name, t)));
      p.setAttribute("aria-pressed", String(on));
      p.onclick = () => h.mark(b.id, v);
      r.append(p);
    }
    c.append(r);
  }
  const go = el("div", "dg-go"), s = el("button", "dg-btn pri", () => (DIAG_COPY.submit));
  s.type = "button"; s.disabled = !roughReady(st);
  s.onclick = () => { if (roughReady(st)) h.submit(); };
  go.append(s); c.append(go);
  return c;
}

/** An ask_check payload's block (the 细问 card's head, 学点新的's 正在问); null when the payload names none. */
export function checkBlock(payload: Record<string, unknown>): { id: string; name: string } | null {
  const id = payload.block_id, name = payload.block_name;
  return typeof id === "string" && id !== "" ? { id, name: typeof name === "string" ? name : "" } : null;
}

export interface CheckCard { blockName: string; question: string; options: string[]; waiting: boolean }

/** 细问: the block, the question, the options while it waits; `rating` (the 👍 / 👎 row) goes last, in `.ew-dg-foot`. */
export function buildCheckCard(q: CheckCard, pick: (option: string) => void, rating: HTMLElement | null): HTMLElement {
  const c = el("div", "dg-q");
  if (q.blockName !== "") {
    const h = el("div", "dg-qk");
    h.append(el("span", "dg-sq" + (q.waiting ? " asking" : "")), q.blockName);
    c.append(h);
  }
  c.append(el("div", "dg-qq", q.question));
  if (q.waiting) for (const o of q.options) {
    const b = el("button", "dg-ob" + (o === DIAG_COPY.idk ? " idk" : ""), o);
    b.type = "button";
    b.onclick = () => pick(o);
    c.append(b);
  }
  if (rating !== null) { const f = el("div", "dg-foot"); f.append(rating); c.append(f); }
  return c;
}
