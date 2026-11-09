/** What 「⋯ → 问助手」 hands the assistant: the section's heading and its opening, as a quote. Pure. */
import type { SectionOut } from "./schema";

/** Characters of the opening paragraph that go into the quote. */
const OPENING_CHARS = 200;

export function sectionQuote(x: Pick<SectionOut, "heading" | "body">): { text: string; heading: string } {
  const first = x.body.split(/\n\s*\n/).map((p) => p.trim()).find((p) => p !== "") ?? "";
  const opening = [...first].slice(0, OPENING_CHARS).join("");
  return { text: opening === "" ? x.heading : `${x.heading}\n${opening}`, heading: x.heading };
}
