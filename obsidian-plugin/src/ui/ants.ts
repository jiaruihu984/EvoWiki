/** app.js `antsFrame(e)`: a dashed frame whose dashes travel round `row` for 6 s, then the frame fades out (CSS `.ew-ants`). */
export function antsFrame(row: HTMLElement): void {
  document.querySelectorAll(".ew-ants").forEach((x) => x.remove());
  const w = row.offsetWidth + 10, h = row.offsetHeight + 10, NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "ew-ants");
  svg.setAttribute("width", String(w));
  svg.setAttribute("height", String(h));
  svg.setAttribute("aria-hidden", "true");
  const r = document.createElementNS(NS, "rect");
  for (const [k, v] of [["x", 1], ["y", 1], ["width", w - 2], ["height", h - 2], ["rx", 13]] as const) r.setAttribute(k, String(v));
  svg.append(r);
  row.append(svg);
  window.setTimeout(() => { svg.classList.add("ew-out"); window.setTimeout(() => svg.remove(), 900); }, 6000);
}
