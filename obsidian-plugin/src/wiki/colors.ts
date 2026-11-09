/**
 * The band colours and scores the plugin's cards and the 知识图谱 cloud share: the colour tokens read from the `.ew` root,
 * hex / mix / rgb, the band scores and the demo's colour of a mix of bands (colorOfBands / colorOfMix) and its word.
 */
import type { Band } from "./schema";

export type RGB = [number, number, number];
export const COLOR_KEYS = ["known", "likely", "shaky", "gap", "none", "tx-1", "tx-2", "tx-3", "edge", "accent"] as const;
export type CloudColors = Record<(typeof COLOR_KEYS)[number], string>;

/** The colour tokens (set on the `.ew` root, not on :root). */
export function readColors(from: Element): CloudColors {
  const cs = getComputedStyle(from);
  const C = {} as CloudColors;
  for (const k of COLOR_KEYS) C[k] = cs.getPropertyValue("--" + k).trim();
  return C;
}

export const hex = (h: string): RGB => {
  h = h.replace("#", "");
  if (h.length === 3) h = [...h].map((c) => c + c).join("");
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as RGB;
};
export const mix = (a: RGB, b: RGB, t: number): RGB => a.map((v, i) => Math.round(v + (b[i]! - v) * t)) as RGB;
/** A colour token not read yet (hex("") is NaN) is transparent. */
export const rgb = (c: RGB, al = 1): string => (c.some(Number.isNaN) ? "rgba(0,0,0,0)" : `rgba(${c[0]},${c[1]},${c[2]},${al})`);

export const SCORE: Record<Exclude<Band, "none">, number> = { known: 1, likely: 0.78, shaky: 0.45, gap: 0.08 };

export function ramp(s: number, C: CloudColors): RGB {
  const st: [number, RGB][] = [[0.08, hex(C.gap)], [0.45, hex(C.shaky)], [0.78, hex(C.likely)], [1, hex(C.known)]];
  if (s <= st[0]![0]) return st[0]![1];
  for (let i = 1; i < st.length; i++) if (s <= st[i]![0]) { const [a, ca] = st[i - 1]!, [b, cb] = st[i]!; return mix(ca, cb, (s - a) / (b - a)); }
  return st[st.length - 1]![1];
}

/** The demo's colour of a group of bands: none mixed towards the ramp by the share with evidence. */
export function colorOfBands(bands: Band[], C: CloudColors): RGB {
  const ev = bands.filter((b): b is Exclude<Band, "none"> => b !== "none"); if (!ev.length) return hex(C.none);
  const avg = ev.reduce((s, b) => s + SCORE[b], 0) / ev.length; return mix(hex(C.none), ramp(avg, C), ev.length / bands.length);
}

type Mix = Partial<Record<Band, number>>;
const EVIDENCED = ["known", "likely", "shaky", "gap"] as const;
/** The evidenced share of a mix and its average score ({} = no evidence). */
function evidence(m: Mix): { ev: number; avg: number } {
  const ev = EVIDENCED.reduce((s, b) => s + (m[b] ?? 0), 0);
  return { ev, avg: ev <= 1e-9 ? 0 : EVIDENCED.reduce((s, b) => s + (m[b] ?? 0) * SCORE[b], 0) / ev };
}

/** A map node's colour from its band mix (colorOfBands on shares). */
export function colorOfMix(m: Mix, C: CloudColors): RGB {
  const { ev, avg } = evidence(m);
  if (ev <= 1e-9) return hex(C.none);
  return mix(hex(C.none), ramp(avg, C), Math.min(1, ev));
}

/** 掌握 / 熟悉 / 不会 from the average score of the evidence; "" without evidence. */
export function mixWord(m: Mix, words: { known: string; shaky: string; gap: string }): string {
  const { ev, avg } = evidence(m);
  if (ev <= 1e-9) return "";
  return avg > 0.65 ? words.known : avg > 0.3 ? words.shaky : words.gap;
}
