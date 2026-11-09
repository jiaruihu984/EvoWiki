/**
 * The demo fixture's interactive (fixtures-demo.ts, asset `demo-ix`): app.js `interactive()` of the approved full demo
 * (the product specification) as one standalone page, for the test vault
 * (`EvoWiki/assets/demo-ix.html`, the scripts/ demo-vault script) and the fixture's assetHtml.
 *
 * Only the inside of the demo's box: the plugin's embedded-html block (embed/embed-block.ts) already draws the `.ix` box
 * (`.evowiki-embed`: border, radius, padding, gap) and its title from the block's `title:` line (`.evowiki-embed-title`
 * = `.ix .t`), with the iframe as the box's last child. So this page holds the rest of app.js interactive() — `.how`,
 * `.grid2` with the svg and the side column (slider, big text, status) — in a borderless `.ix` column with the demo's
 * 10px gap, and the box + title + page together look like the demo's box once. Same copy and the demo's CSS values:
 * monochrome chrome, the data palette for the data (A v line, dot and label and the slider in --viz-1, the 对齐 line in
 * --known). The title stays as the document <title> (for opening the file on its own).
 *
 * Theme: inside the plugin's iframe the host injects `:root { --bg-0: …; --viz-1: …; …; color-scheme: light|dark }`
 * before this file (embed-block.ts themeStyle, read at the note's `.evowiki-page`). This file's own token values are the
 * demo's light / dark values under `:where(…)` (no specificity), so an injected value always wins and a missing one falls
 * back to the demo's; the script reads color-scheme and sets data-theme. Opened on its own, prefers-color-scheme decides.
 * The box's outer margin (`margin:18px 0` in the demo) belongs to the note around the iframe, so it is 0 here.
 *
 * Size: the page reports its body's height to the plugin (`parent.postMessage({ evowikiFrameHeight })`, embed-block.ts
 * frameHeightOf) on load and whenever the body resizes, so the frame is exactly the content, as the demo's box is.
 */
export const DEMO_IX_HTML = `<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>转动箭头，找出不被掰弯的方向</title>
<style>
:where(:root){
  --bg-0:#ffffff; --bg-1:#f6f6f6; --bg-2:#ebebeb; --border:#e0e0e0;
  --tx-1:#222222; --tx-2:#5c5c5c; --tx-3:#8a8a8a;
  --known:#2f9e5b; --viz-1:#2f6fdf;
  --font-ui:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
}
@media (prefers-color-scheme:dark){:where(:root:not([data-theme])){
  --bg-0:#1e1e1e; --bg-1:#262626; --bg-2:#303030; --border:#363636;
  --tx-1:#dadada; --tx-2:#a3a3a3; --tx-3:#6f6f6f;
  --known:#4cc27a; --viz-1:#6ea2ff; color-scheme:dark}}
:where(:root[data-theme="dark"]){
  --bg-0:#1e1e1e; --bg-1:#262626; --bg-2:#303030; --border:#363636;
  --tx-1:#dadada; --tx-2:#a3a3a3; --tx-3:#6f6f6f;
  --known:#4cc27a; --viz-1:#6ea2ff; color-scheme:dark}
html,body{margin:0;overflow:hidden;background:var(--bg-0);color:var(--tx-1);font:14px/1.6 var(--font-ui)}
.ix{font-family:var(--font-ui);display:flex;flex-direction:column;gap:10px;font-size:14px;line-height:1.6}
.ix .how{font-size:13px;color:var(--tx-2)}
.ix .grid2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,190px);gap:14px;align-items:center}
.ix svg{width:100%;max-width:320px;height:auto;background:var(--bg-1);border-radius:8px}
.ix label{display:flex;flex-direction:column;gap:2px;font-size:13px;color:var(--tx-2)}
.ix input[type=range]{accent-color:var(--viz-1);width:100%}
.ix .big{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
.ix .ok{color:var(--known);font-weight:600}
</style>
</head>
<body>
<div class="ix">
<div class="how">拖动滑块改变 v 的方向，看 A v（蓝色）和 v（灰色）是不是在同一条线上。A = [[2, 1], [1, 2]]。</div>
<div class="grid2"><div id="svgw"></div><div style="display:flex;flex-direction:column;gap:8px"><label><span id="lt"></span><input type="range" min="0" max="180" step="1" value="20" id="ix-angle"></label><div class="big" id="big"></div><div class="how" id="st"></div></div></div>
</div>
<script>
(function () {
  var cs = getComputedStyle(document.documentElement).colorScheme || "";
  if (cs === "dark" || cs === "light") document.documentElement.setAttribute("data-theme", cs);
  var r = document.getElementById("ix-angle"), svgw = document.getElementById("svgw"), lt = document.getElementById("lt"),
    big = document.getElementById("big"), st = document.getElementById("st");
  var NS = "http://www.w3.org/2000/svg";
  // The same svg app.js writes as markup, built node by node.
  function node(tag, attrs, text) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, String(attrs[k]));
    if (text != null) e.textContent = text;
    return e;
  }
  function draw() {
    var a = (+r.value * Math.PI) / 180, vx = Math.cos(a), vy = Math.sin(a), ax = 2 * vx + vy, ay = vx + 2 * vy;
    var S = 34, ox = 160, oy = 120, X = function (x) { return ox + x * S; }, Y = function (y) { return oy - y * S; };
    var ang = Math.abs(Math.atan2(vx * ay - vy * ax, vx * ax + vy * ay));
    var same = ang < 0.035, lam = Math.hypot(ax, ay);
    var svg = node("svg", { viewBox: "0 0 320 240", "aria-label": "v 和 A v 的箭头" });
    var av = { x1: ox, y1: oy, x2: X(ax), y2: Y(ay), stroke: "var(--viz-1)", "stroke-width": 3, "stroke-linecap": "round" };
    if (!same) av["stroke-dasharray"] = "7 5";
    svg.append(
      node("line", { x1: 0, y1: oy, x2: 320, y2: oy, stroke: "var(--border)" }),
      node("line", { x1: ox, y1: 0, x2: ox, y2: 240, stroke: "var(--border)" }),
      node("line", { x1: ox, y1: oy, x2: X(vx * 1.6), y2: Y(vy * 1.6), stroke: "var(--tx-3)", "stroke-width": 3, "stroke-linecap": "round" }),
      node("line", av),
      node("circle", { cx: X(ax), cy: Y(ay), r: 4, fill: "var(--viz-1)" }),
      node("text", { x: X(vx * 1.6) + 6, y: Y(vy * 1.6) - 6, "font-size": 12, fill: "var(--tx-2)" }, "v"),
      node("text", { x: X(ax) + 6, y: Y(ay) - 6, "font-size": 12, fill: "var(--viz-1)" }, "A v"));
    svgw.replaceChildren(svg);
    lt.textContent = "v 的方向：" + r.value + "°";
    big.textContent = same ? "λ = " + lam.toFixed(1) : "差 " + Math.round((ang * 180) / Math.PI) + "°";
    if (same) { var ok = document.createElement("span"); ok.className = "ok"; ok.textContent = "在同一条线上：v 是特征向量"; st.replaceChildren(ok); }
    else st.textContent = "还没对齐，继续转";
  }
  r.addEventListener("input", draw);
  draw();
  // The height handshake (opt-in, embed-block.ts): the body's height, on load and whenever it changes.
  function report() {
    if (window.parent === window) return;
    parent.postMessage({ evowikiFrameHeight: Math.ceil(document.documentElement.scrollHeight) }, "*");
  }
  if (typeof ResizeObserver === "function") new ResizeObserver(report).observe(document.body);
  window.addEventListener("load", report);
  report();
})();
</script>
</body>
</html>
`;
