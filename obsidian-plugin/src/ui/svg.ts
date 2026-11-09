const NS = "http://www.w3.org/2000/svg";
type Child = Node | string | null | undefined | false;

/** SVG element builder, same contract as h(): strings are text nodes, never parsed markup. */
export function s(tag: string, attrs: Record<string, string | number | ((e: Event) => void)> = {}, ...children: Child[]): SVGElement {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (typeof v === "function") el.addEventListener(k.replace(/^on/, ""), v);
    else el.setAttribute(k, String(v));
  }
  for (const c of children) if (c !== null && c !== undefined && c !== false) el.append(typeof c === "string" ? document.createTextNode(c) : c);
  return el;
}

/** app.js `CHECK_SVG`: the small tick of the 答对了 line. */
export function checkSvg(): SVGElement {
  return s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, s("path", { d: "M5 12.5l4.5 4.5L19 7.5" }));
}
