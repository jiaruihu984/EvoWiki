/**
 * A chat answer's citations as the backend stores them (sources/chat_cite.py), shown in place of the raw marks:
 * `〔名 p.印码｜N｜source_id：「片段」〕` opens the original at PDF page N, `〔名｜source_id：「片段」〕` (no pages) is text,
 * `〔名 p.印码｜N｜此前读取｜source_id：「片段」〕` likewise, and says this turn did not read it,
 * `〔名 p.N｜未核对…〕` says it was not checked, and a material deleted or cleaned up by now says so. The quote is the hover.
 */
import { ASSISTANT_COPY as C } from "./copy";
import { localizedText } from "../i18n";

const NOT_CHECKED = "未核对";
const MARK = /〔([^〔〕｜\n]*?)(?:\s*p\.(\d+))?(?:｜(\d+))?(｜此前读取)?｜([^｜：〔〕\n]+)(?:：「([^〕\n]*)」)?〕/g;

interface Mark { name: string; print: string | undefined; pdf: string | undefined; earlier: boolean; tag: string; quote: string }

function replaceMarks(text: string, to: (m: Mark) => string): string {
  return text.replace(MARK, (_all, name: string, print: string | undefined, pdf: string | undefined, earlier: string | undefined, tag: string, quote: string | undefined) =>
    to({ name: name.trim(), print, pdf, earlier: earlier !== undefined, tag, quote: quote ?? "" }));
}

/** Every character Markdown, math or HTML could act on, as an entity: the text stays what it says inside inline HTML. */
const esc = (s: string): string => s.replace(/[&<>"'`*_~$|[\]\\#^=%]/g, (c) => `&#${c.codePointAt(0)!};`);
const label = (m: Mark): string => (m.print === undefined ? m.name : `${m.name} p.${m.print}`.trim()) + (m.earlier ? C.citeEarlier : "");

/** The answer's Markdown with its citations as inline HTML; `gone`: a cited material deleted or cleaned up by now. */
export function citationsToMarkdown(text: string, gone: (sourceId: string) => boolean): string {
  return replaceMarks(text, (m) => {
    const hover = m.quote === "" ? "" : ` title="${esc(m.quote)}"`;
    const state = m.tag === NOT_CHECKED ? "miss" : gone(m.tag) ? "gone" : "checked";
    const base = m.print === undefined ? m.name : `${m.name} p.${m.print}`.trim();
    const binding = ` data-ew-cite="${esc(JSON.stringify({ base, earlier: m.earlier, state }))}"`;
    if (state === "miss") return `<span class="ew-cite ew-miss"${binding}${hover}>${esc(label(m))}${m.name === "" && m.print === undefined ? "" : "："}${C.citeMissed}</span>`;
    if (state === "gone") return `<span class="ew-cite ew-gone"${binding}${hover}>${esc(label(m))}${C.citeDeleted}</span>`;
    if (m.pdf === undefined) return `<span class="ew-cite ew-text"${binding}${hover}>${esc(label(m))}</span>`;
    return `<a class="ew-cite" data-source="${esc(m.tag)}" data-page="${m.pdf}"${binding}${hover}>${esc(label(m))}</a>`;
  });
}
/** Only metadata emitted above is bound; the answer body is never translated. */
export function bindCitationLabels(root: HTMLElement): void {
  for (const element of root.querySelectorAll<HTMLElement>(".ew-cite[data-ew-cite]")) {
    let data: { base: string; earlier: boolean; state: string };
    try { data = JSON.parse(element.dataset.ewCite!) as typeof data; } catch { continue; }
    if (!data || typeof data.base !== "string" || typeof data.earlier !== "boolean" || !["miss", "gone", "checked"].includes(data.state)) continue;
    localizedText(element, () => data.base + (data.earlier ? C.citeEarlier : "") + (data.state === "miss" ? `${data.base ? "：" : ""}${C.citeMissed}` : data.state === "gone" ? C.citeDeleted : ""));
  }
}

/** The answer as words: what 复制 copies and what shows before the Markdown is ready. */
export function plainCitations(text: string): string {
  return replaceMarks(text, (m) => m.tag === NOT_CHECKED ? `（${label(m)}：${C.citeMissed}）` : `（${label(m)}）`);
}
