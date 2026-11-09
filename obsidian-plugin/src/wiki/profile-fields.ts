/** Editable sections over the same profile note; unchanged lines retain hidden source identities. */
export const PROFILE_FIELD_HEADINGS = { who: ["我是谁", "About me"], want: ["想学什么", "What I want to learn"], how: ["怎么学比较好", "How I learn best"] } as const;
export type ProfileField = keyof typeof PROFILE_FIELD_HEADINGS;
export type ProfileFields = Record<ProfileField, string>;
const clean = (line: string): string => line.replace(/^\s*[-*+]\s+/, "").trim();
function chunks(text: string): { heading: string; lines: string[] }[] {
  const sections = [{ heading: "", lines: [] as string[] }];
  for (const line of text.split("\n")) {
    const heading = /^##\s+(.+?)\s*#*\s*$/.exec(line)?.[1];
    if (heading) sections.push({ heading, lines: [] }); else sections[sections.length - 1]!.lines.push(line);
  }
  return sections;
}
export function readProfileFields(text: string): ProfileFields {
  const result: ProfileFields = { who: "", want: "", how: "" };
  for (const section of chunks(text)) for (const key of Object.keys(PROFILE_FIELD_HEADINGS) as ProfileField[]) {
    if ((PROFILE_FIELD_HEADINGS[key] as readonly string[]).includes(section.heading)) result[key] = section.lines.filter((line) => !/^\s*<!--/.test(line)).map(clean).filter(Boolean).join("\n");
  }
  return result;
}
export function writeProfileFields(text: string, fields: ProfileFields, language: "zh" | "en"): string {
  const sections = chunks(text); const previous = readProfileFields(text);
  for (const key of Object.keys(PROFILE_FIELD_HEADINGS) as ProfileField[]) {
    if (fields[key] === previous[key]) continue;
    let section = sections.find((part) => (PROFILE_FIELD_HEADINGS[key] as readonly string[]).includes(part.heading));
    if (!section) { section = { heading: PROFILE_FIELD_HEADINGS[key][language === "zh" ? 0 : 1], lines: [] }; sections.push(section); }
    const originals = new Map<string, string[]>(); let markers: string[] = [];
    for (const line of section.lines) {
      if (/^\s*<!--/.test(line)) { markers.push(line); continue; }
      if (clean(line)) originals.set(clean(line), [...markers, line]); markers = [];
    }
    section.lines = fields[key].split("\n").map(clean).filter(Boolean).flatMap((line) => originals.get(line) ?? [`- ${line}`]);
  }
  return sections.map((section) => [section.heading ? `## ${section.heading}` : "", ...section.lines].join("\n").trim()).filter(Boolean).join("\n\n") + "\n";
}
