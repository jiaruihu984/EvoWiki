"""Render check (plan rev 3.3 §10.2 step 4): code, not the model, decides whether an interactive works.

Each interactive is opened in headless Chromium twice, at 375 px and at 1280 px wide (a block: at the narrowest real pane,
`NARROW_BLOCK`, and at 1280; height = the draft's own height). It is served sandboxed like the asset route (`CHECK_CSP`: the route's
`ASSET_CSP` without its network blocks, so an attempted request reaches the browser's router), and
every request it makes is counted and aborted — interactives are offline, so any request fails.
Console errors and uncaught exceptions are collected; horizontal overflow is measured. Then every
probe is performed on a fresh load and the text of its `expect_change` region must differ afterwards.
`ok` only when all of that holds at both widths.

An EvoUI block (`check_block`) first goes through `guard.js`, the very script the Obsidian plugin refuses
blocks with: a refused block fails here, with the guard's reason, and is not rendered at all. Then it is
checked the same way on `deep.evoui.harness_html` (the one renderer, evoui.js): no console / page errors, no network, no overflow at either width, and the renderer did not
fall back to the block's text (a block whose expressions fail at runtime shows `.evoui-error`), and no
text inside the block is clipped (an element hiding its horizontal overflow whose text sticks out). Its
probes set states through the renderer's own handle and the block's visible text (leaving ghosts of a
transition excluded) must change; a block without probes gets one per control (slider / input / player
min→max, first→last option, toggle on→off). A block with
no control and no probe (a static chart, a quiz, a timeline) passes on the rendering checks alone. Every
`deep-dive` is opened first (`_OPEN_DEEP_DIVES_JS`), so what it folds away is measured and probed like the rest.

A block with an `app` node (the sandboxed mini-app) is also checked inside the app's frame: the bridge
must have received `evowiki.ready()` (within APP_READY_MS); console errors and uncaught exceptions of the
frame count like the page's; requests to an allowlisted CDN (`APP_CDN_ALLOWLIST`) are let through (one
that fails is reported in `detail`, not a failure: the check may run offline), any other request is
aborted and fails the check, and a request the frame's CSP refused (its console says so) counts as a
network request too; horizontal overflow and clipped content (taller than the frame, cut by a box, text
outside its drawing) are measured inside the frame as well; up to APP_PROBES things the reader can touch
(controls, a canvas, and whatever has a click or pointer handler or a pointer cursor, SVG shapes included)
are clicked one by one and the frame must show something different at least once: its text, its DOM (an
SVG's shapes and attributes) or a canvas's pixels; a message the bridge dropped (handle.rejected()) fails
the check.

`look_block` is the one place that photographs a block (`deep.visual`, `deep.scene`): opening, after each probe its author
gave (a block without an app: after its first controls, or its first mark that can be clicked), narrow and dark.

The check also keeps what the screen said at the widest width (`RenderCheck.readings`): the block's text,
then at each trial operation the lines that are not the same at all of them, plus every number control at its minimum and at its maximum
together. They decide nothing here; the writer compares them with what its text claims.

Both checks also look for a big empty area (`_BLANK_GAP_JS`): at 1280 px, within the content (the page's body
for a component, the block's own box for a block), the tallest vertical gap between things really drawn (text,
images, controls, a canvas with pixels, SVG shapes other than an axis / grid / tick) must stay under
BLANK_MAX_PX — an empty chart or stage, a canvas that drew nothing, a box with nothing in it fails, and the reason
goes back to whoever wrote it.

Playwright is the optional extra `deep` (`uv sync --extra deep`, then `uv run playwright install
chromium`). Without it `PlaywrightChecker.check` returns a failed check saying so (fail closed)."""

import base64
import contextlib
import json
import logging
import os
import re
import signal
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from evowiki.deep.contracts import AppProbe, BlockLook, InteractiveDraft, Probe, RenderCheck
from evowiki.deep.evoui import GUARD_PATH, block_json, controls, harness_html, node_keys
from evowiki.deep.evoui.contracts import (
    APP_CDN_ALLOWLIST,
    APP_READY_MS,
    BOOL_CONTROLS,
    NUMBER_CONTROLS,
    OPTION_CONTROLS,
    EvoUIBlock,
)

log = logging.getLogger("evowiki.deep.render")

WIDTHS = (375, 1280)
# A generated page in real Obsidian, window 375 wide: a block's frame is 238 px and so is an app inside it (measured). The harness
# draws the block as a card (24 px of body padding, 30 of card padding), so this width leaves its content the same 238.
NARROW_BLOCK = 292
BLOCK_WIDTHS = (NARROW_BLOCK, 1280)
# The interactive's own origin while checked; never resolved (the route serves it from memory).
CHECK_URL = "http://interactive.evowiki.invalid/index.html"
# Served on GET /wiki/assets/{id} and used by the check: inline script/style only, no network.
ASSET_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
             "img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; "
             "form-action 'none'; base-uri 'none'; sandbox allow-scripts")
CHECK_CSP = "sandbox allow-scripts"
SETTLE_MS = 300            # after load / after a probe action, before reading the result
CHANGE_TIMEOUT_MS = 1500   # how long a probe's result region may take to change
ACTION_TIMEOUT_MS = 3000
LOAD_TIMEOUT_MS = 10_000
OVERFLOW_TOLERANCE_PX = 1  # sub-pixel rounding
BLANK_MAX_PX = 200         # the tallest vertical gap with nothing drawn the content may have (at 1280 px)
BLANK_WIDTH = 1280
MAX_MESSAGES = 10
MSG_CHARS = 300
CHECK_DEADLINE_S = 180    # a whole check: page.evaluate has no timeout of its own, and a block can hang its page

_OVERFLOW_JS = """() => {
  const d = document.documentElement, b = document.body;
  const w = Math.max(d.scrollWidth, b ? b.scrollWidth : 0);
  return Math.max(0, Math.ceil(w - d.clientWidth));
}"""


# The largest vertical gap (px) between the things the content really draws: visible text, images, form controls,
# a canvas with pixels on it (a library's drawing layer; an empty layer does not count), SVG shapes other than an
# axis / grid / tick. The content box is the root's (the body for a component, the block's own box for a block);
# its top and bottom edges count as edges of the gap, so a tall empty plot or stage under the controls shows up.
_BLANK_GAP_JS = """(sel) => {
  const root = sel ? document.querySelector(sel) : document.body;
  if (!root) return null;
  const box = root.getBoundingClientRect();
  const top = box.top + window.scrollY, bottom = top + (sel ? box.height : Math.max(box.height, root.scrollHeight));
  const shown = (e) => { for (let x = e; x && x !== document; x = x.parentElement) {
    const cs = getComputedStyle(x); if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) return false; }
    return true; };
  const inked = (c) => { try { const w = c.width, h = c.height; if (!w || !h) return false;
      const d = c.getContext('2d') && c.getContext('2d').getImageData(0, 0, w, h).data;
      if (!d) return true; for (let i = 3; i < d.length; i += 16) if (d[i] > 0) return true; return false;
    } catch (e) { return true; } };
  const spans = [];
  const add = (r) => { if (r.height > 0 && r.width > 0) spans.push([r.top + window.scrollY, r.bottom + window.scrollY]); };
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.textContent.trim() || !n.parentElement || !shown(n.parentElement)) continue;
    const rg = document.createRange(); rg.selectNodeContents(n); for (const r of rg.getClientRects()) add(r);
  }
  // an SVG drawing with at least one real shape counts whole (data may sit anywhere in its area, like a canvas
  // with pixels); one with only an axis / grid / tick counts as empty
  const mark = (e) => { const tag = e.tagName.toLowerCase();
    return !['g', 'defs', 'lineargradient', 'radialgradient', 'stop', 'clippath', 'mask', 'title', 'desc'].includes(tag)
      && !/(^|\\s)(axis|grid|tick)(\\s|$)/.test(e.getAttribute('class') || ''); };
  for (const e of root.querySelectorAll('img,video,input,select,button,textarea,iframe,canvas,svg')) {
    if (!shown(e)) continue;
    const tag = e.tagName.toLowerCase();
    if (tag === 'canvas' && !inked(e)) continue;
    if (tag === 'svg' && (e.ownerSVGElement || ![...e.querySelectorAll('*')].some((x) => mark(x) && shown(x)))) continue;
    add(e.getBoundingClientRect());
  }
  spans.sort((a, b) => a[0] - b[0]);
  let gap = 0, reach = top;
  for (const [a, b] of spans) { if (a > reach) gap = Math.max(gap, a - reach); reach = Math.max(reach, b); }
  return Math.round(Math.max(gap, bottom - reach));
}"""


def _blank_note(page: Any, selector: str | None) -> str | None:
    """`有一大片空白` when the content has a vertical gap taller than BLANK_MAX_PX with nothing drawn in it,
    else None (also None when the box cannot be measured: the other checks report that)."""
    gap = page.evaluate(_BLANK_GAP_JS, selector)
    if gap is None or gap <= BLANK_MAX_PX:
        return None
    return f"有一大片空白（{gap}px 高的一块什么都没画）：去掉空的区域，或者让图 / 画布里真的画出东西"


def _clip(xs: list[str]) -> tuple[str, ...]:
    return tuple(x[:MSG_CHARS] for x in dict.fromkeys(xs))[:MAX_MESSAGES]


class _Run:
    """One width's observations (shared across that width's page loads)."""

    def __init__(self) -> None:
        self.console: list[str] = []
        self.errors: list[str] = []
        self.requests: list[str] = []
        self.cdn: list[str] = []            # allowlisted CDN requests let through (app blocks only)
        self.cdn_failed: list[str] = []     # ... those of them that failed (reported, not a violation)


def cdn_allowed(url: str) -> bool:
    """An app's request the render check lets through: an allowlisted CDN (contracts.APP_CDN_ALLOWLIST)."""
    return url.lower().startswith(APP_CDN_ALLOWLIST)


# Chromium's console lines for a request a CSP refused ("Connecting to 'https://…' violates the following
# Content Security Policy directive …", "Fetch API cannot load https://…. Refused to connect because it violates
# the document's Content Security Policy.", "Refused to load the script 'https://…' because …"): the URL
_CSP_URL = re.compile(r"(?:https?|wss?|ftp)://[^\s'\"]+[^\s'\".,]")


def _watch(page: Any, run: _Run, html: str, cdn: bool = False) -> None:
    def route(r: Any) -> None:
        url = r.request.url
        if url == CHECK_URL:
            r.fulfill(status=200, body=html, headers={
                "content-type": "text/html; charset=utf-8", "content-security-policy": CHECK_CSP})
        elif cdn and cdn_allowed(url):
            run.cdn.append(url)
            r.continue_()
        else:
            run.requests.append(url)
            r.abort()

    def console(m: Any) -> None:
        if m.type != "error":
            return
        url = _CSP_URL.search(m.text) if cdn and "Content Security Policy" in m.text else None
        if url:                     # a request the app's CSP refused: a network request, not a console error
            run.requests.append(f"{url.group(0)}（被小程序的 CSP 拦下）")
        else:
            run.console.append(m.text)

    def failed(req: Any) -> None:
        if cdn and req.url in run.cdn:
            run.cdn_failed.append(f"{req.url}（{req.failure or '失败'}）")

    page.route("**/*", route)
    page.on("console", console)
    page.on("pageerror", lambda e: run.errors.append(str(e)))
    page.on("websocket", lambda ws: run.requests.append(ws.url))
    page.on("requestfailed", failed)


def _load(context: Any, run: _Run, html: str, cdn: bool = False) -> Any:
    page = context.new_page()
    _watch(page, run, html, cdn)
    page.goto(CHECK_URL, wait_until="load", timeout=LOAD_TIMEOUT_MS)
    page.wait_for_timeout(SETTLE_MS)
    return page


def _text(page: Any, selector: str) -> str | None:
    loc = page.locator(selector).first
    if loc.count() == 0:
        return None
    return loc.inner_text(timeout=ACTION_TIMEOUT_MS)


def _probe(page: Any, p: Probe) -> str | None:
    """None when the probe passed, else why not."""
    if page.locator(p.target).count() == 0:
        return f"找不到控件 {p.target}"
    before = _text(page, p.expect_change)
    if before is None:
        return f"找不到结果区 {p.expect_change}"
    target = page.locator(p.target).first
    if p.action == "click":
        target.click(timeout=ACTION_TIMEOUT_MS)
    elif p.action == "input":
        target.fill(p.value or "", timeout=ACTION_TIMEOUT_MS)
        target.dispatch_event("change")
    else:
        target.select_option(p.value, timeout=ACTION_TIMEOUT_MS)
    page.wait_for_timeout(SETTLE_MS)
    try:
        page.wait_for_function(
            "([sel, before]) => { const e = document.querySelector(sel);"
            " return e !== null && e.innerText !== before; }",
            arg=[p.expect_change, before], timeout=CHANGE_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 - a timeout is the failure being measured
        return f"{p.action} {p.target} 之后 {p.expect_change} 的文字没有变化"
    return None


def check_with_browser(browser: Any, draft: InteractiveDraft) -> RenderCheck:
    """The check on an already launched Playwright browser."""
    console: list[str] = []
    errors: list[str] = []
    requests: list[str] = []
    overflow = 0
    failed: dict[int, str] = {}
    blanks: list[str] = []
    for width in WIDTHS:
        run = _Run()
        context = browser.new_context(viewport={"width": width, "height": draft.height},
                                      service_workers="block")
        try:
            page = _load(context, run, draft.html)
            overflow = max(overflow, int(page.evaluate(_OVERFLOW_JS)))
            if width == BLANK_WIDTH and (blank := _blank_note(page, None)):
                blanks.append(blank)
            page.close()
            for i, p in enumerate(draft.probes):
                page = _load(context, run, draft.html)     # each probe on a fresh load
                try:
                    why = _probe(page, p)
                except Exception as exc:  # noqa: BLE001 - a broken control fails the probe
                    why = f"{p.action} {p.target} 出错：{exc}"
                if why is not None and i not in failed:
                    failed[i] = f"{width}px: {why}"
                page.close()
        except Exception as exc:  # noqa: BLE001 - the page never loaded
            errors.append(f"{width}px 打不开：{exc}")
        finally:
            context.close()
        console += run.console
        errors += run.errors
        requests += run.requests
    passed = len(draft.probes) - len(failed)
    over = overflow if overflow > OVERFLOW_TOLERANCE_PX else 0
    ok = not console and not errors and not requests and not over and not failed and not blanks
    notes = [*failed.values(), *blanks]
    if over:
        notes.append(f"横向溢出 {over}px")
    if requests:
        notes.append("发起了网络请求：" + ", ".join(dict.fromkeys(requests))[:200])
    return RenderCheck(interactive_id=draft.interactive_id, ok=ok,
                       console_errors=_clip(console), page_errors=_clip(errors),
                       network_requests=len(requests), overflow_px=over,
                       probes_passed=passed, probes_total=len(draft.probes),
                       detail="；".join(notes)[:1000])


# ---- EvoUI blocks ----

def block_probes(block: EvoUIBlock) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(from, to) state settings to try: the block's own probes (from = its initial state), else two
    values per control (a route that binds a state: like a choice, its first and last option). Each
    one runs from the block's initial state."""
    if block.probes:
        return [({}, dict(p.set)) for p in block.probes]
    out = []
    for c in controls(block.body):
        b = c.get("bind")
        if c.get("kind") in NUMBER_CONTROLS:
            out.append(({b: c["min"]}, {b: c["max"]}))
        elif c.get("kind") in (*OPTION_CONTROLS, "route") and len(c.get("options") or []) >= 2:
            out.append(({b: c["options"][0]["value"]}, {b: c["options"][-1]["value"]}))
        elif c.get("kind") in BOOL_CONTROLS:
            out.append(({b: 1}, {b: 0}) if block.state.get(b) in (0, 1) and not isinstance(block.state.get(b), bool)
                       else ({b: True}, {b: False}))
    return out


# The block's visible text without leaving ghosts: content an `if` / transition replaced fades out for
# 120 ms marked `data-leaving` (evoui.js motion); it is not the block's text any more.
_VISIBLE_TEXT_JS = """const visibleText = (root) => {
  const gone = [...root.querySelectorAll('[data-leaving]')], was = gone.map(e => e.style.display);
  gone.forEach(e => { e.style.display = 'none'; });
  const t = root.innerText;
  gone.forEach((e, i) => { e.style.display = was[i]; });
  return t;
};
const shown = (root) => visibleText(root) + [...root.querySelectorAll('svg')].filter(s => !s.closest('[data-leaving]')).map(s => s.outerHTML).join('');"""

# A deep-dive folds its part away until the reader opens it; the check opens them all, so overflow, clipped text, a
# blank area or a dead control inside one counts (closed, the part has no box at all).
_OPEN_DEEP_DIVES_JS = """() => { const ds = document.querySelectorAll('#evoui-mount details.evoui-dd');
  for (const d of ds) d.open = true; return ds.length; }"""

_BLOCK_STATE_JS = """() => {
  """ + _VISIBLE_TEXT_JS + """
  const root = document.querySelector('#evoui-mount .evoui');
  return {rendered: !!root && !!window.__evoui,
          fallback: !!document.querySelector('#evoui-mount .evoui-error'),
          text: root ? visibleText(root) : ''};
}"""

# What a reading quotes: the visible text without the chart's and plot's tick labels.
_READING_TEXT_JS = """() => {
  if (document.querySelector('#evoui-mount .evoui-error')) return '渲染出错，只显示 fallback 文字';
  const root = document.querySelector('#evoui-mount .evoui');
  if (!root) return '';
  const gone = [...root.querySelectorAll('[data-leaving],svg')], was = gone.map(e => e.style.display);
  gone.forEach(e => { e.style.display = 'none'; });
  const t = root.innerText;
  gone.forEach((e, i) => { e.style.display = was[i]; });
  return t;
}"""

_SET_STATES_JS = """([initial, to]) => {
  for (const [k, v] of Object.entries({...initial, ...to})) window.__evoui.set(k, v);
}"""

# Clipped text inside the block: an element that hides its horizontal overflow (overflow-x hidden / clip)
# while its content is wider than its box (scrollWidth > clientWidth + 1) and some text node inside it
# really sticks out past its right edge. Deliberate truncation (text-overflow: ellipsis), screen-reader-only
# text (.evoui-sr), leaving ghosts, elements
# with no box and the sandboxed app frame are skipped. Returns up to 5 short descriptions.
_CLIPPED_JS = """() => {
  const root = document.querySelector('#evoui-mount .evoui'), out = [];
  if (!root) return out;
  for (const el of root.querySelectorAll('*')) {
    if (out.length >= 5) break;
    if (el.closest('.evoui-sr,[data-leaving],iframe') || el instanceof SVGElement) continue;
    const cs = getComputedStyle(el);
    if (cs.overflowX !== 'hidden' && cs.overflowX !== 'clip') continue;
    if (cs.textOverflow === 'ellipsis') continue;                     // deliberate truncation (shows …)
    if (el.clientWidth < 2 || el.scrollWidth <= el.clientWidth + 1) continue;
    const box = el.getBoundingClientRect(), walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let cut = null;
    for (let t = walker.nextNode(); t; t = walker.nextNode()) {
      if (!t.textContent.trim()) continue;
      const r = document.createRange(); r.selectNodeContents(t);
      const rb = r.getBoundingClientRect();
      if (rb.width > 0 && (rb.right > box.right + 1 || rb.left < box.left - 1)) { cut = t.textContent.trim(); break; }
    }
    if (cut !== null) out.push((el.className && typeof el.className === 'string' ? '.' + el.className.split(' ')[0] : el.tagName.toLowerCase()) + '：' + cut.slice(0, 30));
  }
  return out;
}"""

_BLOCK_PROBE_JS = """([initial, from, to]) => {
  """ + _VISIBLE_TEXT_JS + """
  const ev = window.__evoui, root = document.querySelector('#evoui-mount .evoui');
  for (const [k, v] of Object.entries(initial)) ev.set(k, v);       // every probe starts from scratch
  for (const [k, v] of Object.entries(from)) ev.set(k, v);
  const before = shown(root);
  for (const [k, v] of Object.entries(to)) ev.set(k, v);
  const err = !!document.querySelector('#evoui-mount .evoui-error');
  return {changed: shown(root) !== before, fallback: err};
}"""


# ---- drawings: what a reader can touch on them, and what the eye would see first ----

MARK_TOUCHES = 6          # marks clicked or dragged in a drawing
_MARKS_JS = """() => [...document.querySelectorAll('#evoui-mount .evoui-hit, #evoui-mount .evoui-drag')].length"""
_MARK_BOX_JS = """(i) => { const e = [...document.querySelectorAll('#evoui-mount .evoui-hit, #evoui-mount .evoui-drag')][i]; if (!e) return null;
  e.scrollIntoView({block: 'center'}); const r = e.getBoundingClientRect();
  return {x: r.x + r.width / 2, y: r.y + r.height / 2, drag: e.classList.contains('evoui-drag'), w: r.width, h: r.height}; }"""
_MARK_SNAP_JS = """() => { """ + _VISIBLE_TEXT_JS + """ return shown(document.querySelector('#evoui-mount .evoui')); }"""
# Text sticking out of its drawing, and two labels on top of each other: what a look at the picture would catch first.
_SVG_PROBLEMS_JS = """() => {
  const out = [];
  for (const s of document.querySelectorAll('#evoui-mount svg.evoui-svg')) {
    const b = s.getBoundingClientRect();
    const seen = (e) => { for (let x = e; x && x !== s; x = x.parentElement) if (+getComputedStyle(x).opacity < 0.05) return false; return true; };
    const ts = [...s.querySelectorAll('text')].filter((t) => t.textContent.trim() && seen(t)).slice(0, 200).map((t) => [t.textContent.trim().slice(0, 16), t.getBoundingClientRect(), !!t.closest('.axis')]);
    for (const [t, r, axis] of ts) if (!axis && (r.left < b.left - 3 || r.right > b.right + 3 || r.top < b.top - 3 || r.bottom > b.bottom + 3)) out.push(`文字「${t}」超出了画布（viewBox）`);
    for (let i = 0; i < ts.length; i++) for (let j = i + 1; j < ts.length; j++) {
      if (ts[i][2] && ts[j][2]) continue;
      if (ts[i][0] === ts[j][0]) continue;       // the same words twice in one place read as one
      const a = ts[i][1], c = ts[j][1], w = Math.min(a.right, c.right) - Math.max(a.left, c.left), h = Math.min(a.bottom, c.bottom) - Math.max(a.top, c.top);
      if (w > 0 && h > 0 && w * h > 0.3 * Math.min(a.width * a.height, c.width * c.height)) out.push(`文字「${ts[i][0]}」和「${ts[j][0]}」叠在一起`);
    }
    // Text half on a filled shape and half off it: a label over a bar, not a label inside its box. A round shape's box has empty
    // corners, so it counts a little smaller.
    const shapes = [...s.querySelectorAll('rect,circle,ellipse')].filter((e) => {
      const cs = getComputedStyle(e), fill = e.getAttribute('fill') || '';        // on a soft fill the words stay readable
      return fill && fill !== 'none' && fill !== 'transparent' && !fill.includes('color-mix') && +cs.opacity > 0.5 && +cs.fillOpacity > 0.5 && seen(e) && !e.closest('clipPath');
    }).slice(0, 400).map((e) => { const r = e.getBoundingClientRect(), k = e.tagName.toLowerCase() === 'rect' ? 0 : 0.15;
      return {left: r.left + r.width * k, right: r.right - r.width * k, top: r.top + r.height * k, bottom: r.bottom - r.height * k}; });
    for (const [t, r, axis] of ts) {
      if (axis || !(r.width > 0 && r.height > 0)) continue;
      for (const c of shapes) {
        const w = Math.min(r.right, c.right) - Math.max(r.left, c.left), h = Math.min(r.bottom, c.bottom) - Math.max(r.top, c.top);
        if (w < r.height || h < r.height * 0.5) continue;              // at least a character, at least half its height
        if (w * h > 0.85 * r.width * r.height) continue;               // the text sits inside the shape
        out.push(`文字「${t}」压在图形上（一部分在图形里，一部分在外面）：挪开，或者放进图形里`);
        break;
      }
    }
  }
  return [...new Set(out)].slice(0, 5);
}"""


def _touch_marks(page: Any) -> list[str]:
    """Clicks and drags a few marks for real: at least one has to change the picture."""
    n = int(page.evaluate(_MARKS_JS))
    if not n:
        return []
    picks = sorted({0, n - 1, *(i * n // MARK_TOUCHES for i in range(MARK_TOUCHES))})[:MARK_TOUCHES]
    for i in picks:
        box = page.evaluate(_MARK_BOX_JS, i)
        if box is None or not box["w"] or not box["h"]:
            continue
        before = page.evaluate(_MARK_SNAP_JS)
        page.mouse.move(box["x"], box["y"])
        if box["drag"]:
            page.mouse.down()
            page.mouse.move(box["x"] + 50, box["y"] - 40, steps=6)
            page.wait_for_timeout(60)
            page.mouse.up()
        else:
            page.mouse.down()
            page.mouse.up()
        page.wait_for_timeout(SETTLE_MS)
        if page.evaluate(_MARK_SNAP_JS) != before:
            return []
    return [f"图上有 {n} 个能点或拖的地方，试了 {len(picks)} 个，画面都没有变化：onClick / onDrag 要改变画面用到的 state"]


# ---- app nodes: inside the frame ----

APP_PROBES = 6            # things clicked inside an app's frame
# Which elements got a touch handler: there is no way to ask the DOM afterwards.
_HANDLER_HOOK_JS = """(() => {
  const add = EventTarget.prototype.addEventListener, touch = new Set(['click', 'pointerdown', 'pointerup', 'mousedown', 'mouseup', 'touchstart']);
  const seen = new WeakSet();
  Object.defineProperty(window, '__evoTouched', {value: seen});
  // passive: the bridge listening for when to fit the frame, not the app
  EventTarget.prototype.addEventListener = function (t, f, o) { if (touch.has(t) && this && !(o && o.passive)) seen.add(this); return add.call(this, t, f, o); };
})();"""
# Marks up to APP_PROBES things the reader can touch: controls first, then what has a handler or a pointer cursor; a
# handler on a whole drawing (or the document: delegation) makes its shapes touchable.
_APP_MARK_JS = """(limit) => {
  const seen = window.__evoTouched || new WeakSet();
  const on = (e) => seen.has(e) || e.onclick || e.onpointerdown || e.onmousedown || e.hasAttribute('onclick') || e.hasAttribute('onpointerdown') || e.hasAttribute('onmousedown');
  const big = (e) => { const r = e.getBoundingClientRect(); return r.width >= 4 && r.height >= 4; };
  const shapes = 'circle,ellipse,rect,path,polygon,polyline,line,text,image';
  const out = new Set(document.querySelectorAll('button,[role=button],input,select,canvas'));
  const delegated = seen.has(document) || seen.has(window) || (document.body && on(document.body));
  const all = [...document.querySelectorAll('body *')].filter((e) => !out.has(e) && !['SCRIPT', 'STYLE'].includes(e.tagName));
  for (const e of all) if (on(e) && !(e.matches('svg,g') && e.querySelector(shapes))) out.add(e);
  for (const e of all) if (getComputedStyle(e).cursor === 'pointer' && !(e.matches('svg,g') && e.querySelector(shapes))) out.add(e);
  if (delegated) for (const s of document.querySelectorAll('svg')) for (const e of s.querySelectorAll(shapes)) out.add(e);
  for (const s of all) if (s.matches('svg,g') && on(s)) for (const e of s.querySelectorAll(shapes)) out.add(e);
  const list = [...out].filter(big).slice(0, 200), step = Math.max(1, Math.floor(list.length / limit));
  document.querySelectorAll('[data-evo-probe]').forEach((e) => e.removeAttribute('data-evo-probe'));
  // half the tries for what is listed first (the controls), the rest spread over the shapes
  const picked = [...new Set([...list.slice(0, Math.ceil(limit / 2)), ...list.filter((_, i) => i % step === 0)])].slice(0, limit);
  picked.forEach((e) => e.setAttribute('data-evo-probe', ''));
  return picked.length;
}"""
_APP_MARK_NONE_JS = "() => document.querySelectorAll('[data-evo-probe]').forEach((e) => e.removeAttribute('data-evo-probe'))"
_APP_READY_JS = """() => {
  const apps = [...document.querySelectorAll('#evoui-mount .evoui-app')].filter(a => !a.closest('[data-leaving]'));
  return apps.length > 0 && apps.every(a => a.dataset.ready === 'true');
}"""
_APP_TEXT_JS = "() => document.body ? document.body.innerText : ''"
_APP_NAME_JS = ("e => e.innerText || e.textContent || e.getAttribute('aria-label') || e.getAttribute('name')"
                " || e.id || e.tagName.toLowerCase()")
# Content the frame cuts off: taller than the frame, cut by a box that hides overflow, an SVG label outside its drawing.
_APP_CLIPPED_JS = """() => {
  const out = [], de = document.documentElement, b = document.body;
  if (!b) return out;
  const h = Math.ceil(Math.max(de.scrollHeight, b.scrollHeight)), fh = window.innerHeight;
  if (h > fh + 2) out.push(`内容 ${h}px 高，框只有 ${fh}px（下面的部分看不到）`);
  const clip = (s) => s === 'hidden' || s === 'clip';
  for (const el of b.querySelectorAll('*')) {
    if (out.length >= 4) break;
    if (el instanceof SVGElement || el.clientHeight < 20) continue;
    const cs = getComputedStyle(el);
    if (!clip(cs.overflowY) || el.scrollHeight <= el.clientHeight + 2) continue;
    const box = el.getBoundingClientRect(), w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let t = w.nextNode(); t; t = w.nextNode()) {
      if (!t.textContent.trim()) continue;
      const r = document.createRange(); r.selectNodeContents(t);
      const rb = r.getBoundingClientRect();
      if (rb.height > 0 && rb.bottom > box.bottom + 2) { out.push('框里的文字被裁掉：' + t.textContent.trim().slice(0, 24)); break; }
    }
  }
  for (const s of b.querySelectorAll('svg')) {
    if (out.length >= 4 || s.ownerSVGElement || getComputedStyle(s).overflow === 'visible') continue;
    const box = s.getBoundingClientRect();
    for (const t of s.querySelectorAll('text')) {
      const r = t.getBoundingClientRect();
      if (r.width > 0 && (r.left < box.left - 2 || r.right > box.right + 2 || r.top < box.top - 2 || r.bottom > box.bottom + 2)) {
        out.push('图上的文字跑出了画布：' + t.textContent.trim().slice(0, 24)); break;
      }
    }
  }
  return out;
}"""
# the frame's visible text, its DOM (an SVG's shapes and attributes, a picture's source) and a hash of each canvas's
# pixels (a canvas toDataURL refuses counts as "x")
_APP_SNAP_JS = """() => {
  const hash = (s) => { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return h; };
  const cs = [...document.querySelectorAll('canvas')].map(c => { try { return hash(c.toDataURL()); } catch (e) { return 'x'; } });
  const b = document.body;
  return JSON.stringify([b ? b.innerText : '', b ? hash(b.innerHTML) : 0, cs]);
}"""


READING_FIRST_CHARS = 700    # the block's text before any trial operation
READING_CHARS = 200          # what one trial operation changed
READINGS_CHARS = 2400        # all of them


def _lines(text: str) -> list[str]:
    return [x for x in (ln.strip() for ln in text.splitlines()) if x]


def _varying(first: str, shots: list[str]) -> list[str]:
    """Each shot's lines without the ones on the screen at every setting (titles, option labels): a
    reading that stayed the same still shows next to the ones that moved, and a swap shows as a swap."""
    def places(text: str) -> list[tuple[int, str]]:      # counted from both ends: a branch that opens moves what follows
        ls = _lines(text)
        return [(i, x) for i, x in enumerate(ls)] + [(i - len(ls), x) for i, x in enumerate(ls)]

    still = set(places(first)).intersection(*(set(places(x)) for x in shots))

    def moving(text: str) -> str:
        ls = _lines(text)
        return " | ".join(x for i, x in enumerate(ls) if (i, x) not in still and (i - len(ls), x) not in still)
    return [moving(t) or "（文字没变）" for t in shots]


def _setting(states: dict[str, Any]) -> str:
    return "，".join(f"{k}={v}" for k, v in states.items())


def _readings(page: Any, block: EvoUIBlock, probes: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[str]:
    """What the screen says at first and after each trial state (each from the initial state). The
    controls also go to their extremes together: a mistake between two controls shows only then."""
    trials: list[dict[str, Any]] = []
    for frm, to in probes:
        trials += [t for t in (frm, to) if t and t not in trials]
    numbers = [c for c in controls(block.body) if c.get("kind") in NUMBER_CONTROLS]
    if len({c["bind"] for c in numbers}) >= 2:
        trials += [{c["bind"]: c[end] for c in numbers} for end in ("min", "max")]
    page.evaluate(_SET_STATES_JS, [block.state, {}])
    first = page.evaluate(_READING_TEXT_JS)
    out = [" | ".join(_lines(first))[:READING_FIRST_CHARS]]
    shots = []
    for t in trials:
        page.evaluate(_SET_STATES_JS, [block.state, t])
        shots.append(page.evaluate(_READING_TEXT_JS))
    out += [f"{_setting(t)}：{x[:READING_CHARS]}" for t, x in zip(trials, _varying(first, shots), strict=True)]
    return _budget(out)


def _budget(readings: list[str]) -> list[str]:
    out: list[str] = []
    left = READINGS_CHARS
    for r in readings:
        if len(r) > left:
            break
        out.append(r)
        left -= len(r)
    return out


def _refusal(problem: str) -> str:
    """The guard's reason and how to write it instead: the reader shows only the fallback for this block."""
    name = problem.split(":")[0]
    if problem.endswith(("passed to map", "passed to iterate", "used as an index", ": called", ": computed member")):
        how = (f"{name} 的值不能确定只有数字（比如带文字、带对象），这样的名字只能写 {name}.字段 或 {name}[0]，"
               "不能传给 map / iterate，也不能用变量当下标。"
               "改法：选项值用数字、对象键不加引号，或者把文字放到这个名字外面")
    elif "too much work" in problem or "too many loops" in problem:
        how = ("循环太多：range(变量) 和 map(数组) 各按 2000 次算，嵌套相乘，一个表达式最多 20 万次、最多 2 层。"
               "改法：写 range(字面数字)，比如 range(15) 配 k < n ? … : 0")
    elif "text inside iterate" in problem:
        how = "iterate 的表达式里不能有字符串：对象键不加引号，文字放到 iterate 外面的表达式"
    elif "computed member" in problem:
        how = "a[下标] 的下标只能有数字、名字和算术，不能有字符串，读到的结果也不能调用"
    else:
        how = "换一种写法"
    return f"阅读器的安全检查拒绝了这个块（{problem}），读者只会看到 fallback 文字。{how}"


def guard_problem(browser: Any, block: EvoUIBlock) -> str | None:
    """What guard.js says about the block as the plugin receives it (None: nothing), on an empty page."""
    context = browser.new_context(service_workers="block")
    try:
        page = context.new_page()
        page.add_script_tag(content=GUARD_PATH.read_text(encoding="utf-8"))
        return page.evaluate("(block) => window.EvoUIGuard.adapterProblem(block)", json.loads(block_json(block)))
    finally:
        context.close()


def has_app(block: EvoUIBlock) -> bool:
    return any(n.get("kind") == "app" for n in node_keys(block.model_dump(mode="json")).values())


def _click(page: Any, el: Any) -> None:
    """One probe action on an element inside the frame: a select moves to its next option, a range is
    clicked at 80 % of its track, anything else is clicked."""
    kind = el.evaluate("e => e.tagName.toLowerCase() + ':' + (e.type || '')")
    if kind.startswith("select"):
        n = int(el.evaluate("e => e.options.length"))
        if n > 1:
            el.select_option(index=(int(el.evaluate("e => e.selectedIndex")) + 1) % n, timeout=ACTION_TIMEOUT_MS)
        return
    if kind == "input:range":
        el.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
        box = el.bounding_box()
        if box:
            page.mouse.click(box["x"] + box["width"] * 0.8, box["y"] + box["height"] / 2)
        return
    el.click(timeout=ACTION_TIMEOUT_MS)


def _check_app(page: Any, width: int, touch: Sequence[AppProbe] = ()) -> tuple[list[str], int, list[str]]:
    """(failures, horizontal overflow in px inside the frame, readings) of the block's app at this width."""
    try:
        page.wait_for_function(_APP_READY_JS, timeout=APP_READY_MS)
    except Exception:  # noqa: BLE001 - a timeout is the failure being measured
        if page.locator("#evoui-mount .evoui-app").count() == 0:
            return [f"{width}px: 块里有小程序（app），但它没有显示出来（在没显示的分支里？）"], 0, []
        return [f"{width}px: 小程序没有在 {APP_READY_MS // 1000} 秒内调用 evowiki.ready()"], 0, []
    out: list[str] = []
    readings: list[str] = []
    handle = page.locator("#evoui-mount .evoui-app iframe").first.element_handle()
    frame = handle.content_frame() if handle else None
    if frame is None:
        return [f"{width}px: 找不到小程序的 frame"], 0, []
    over = int(frame.evaluate(_OVERFLOW_JS))
    if over > OVERFLOW_TOLERANCE_PX:
        out.append(f"{width}px: 小程序里横向溢出 {over}px")
    page.wait_for_timeout(SETTLE_MS)            # the bridge's first fit of the frame to its content
    out += [f"{width}px: {c}" for c in frame.evaluate(_APP_CLIPPED_JS)]
    moved = False
    first = frame.evaluate(_APP_TEXT_JS)
    for p in touch:                             # the author's own operations: they show that touching it does something
        before = frame.evaluate(_APP_SNAP_JS)
        why = _operate(page, frame, p)
        page.wait_for_timeout(SETTLE_MS)
        moved = moved or (why is None and frame.evaluate(_APP_SNAP_JS) != before)
    if moved:
        out += [f"{width}px: 动手之后：{c}" for c in frame.evaluate(_APP_CLIPPED_JS) if f"{width}px: {c}" not in out]
        readings.append("小程序：" + " | ".join(_lines(first))[:READING_FIRST_CHARS])
        readings.append("动手之后：" + " | ".join(_lines(frame.evaluate(_APP_TEXT_JS)))[:READING_FIRST_CHARS])
        rejected = int(page.evaluate("() => (window.__evoui && window.__evoui.rejected) ? window.__evoui.rejected() : 0"))
        if rejected:
            out.append(f"{width}px: 小程序发来 {rejected} 条不合规的消息（已丢弃，见 console）")
        return out, over, readings
    def touchable() -> list[Any]:
        """Found again before each try: a program that redraws replaces its elements."""
        frame.evaluate(_APP_MARK_JS, APP_PROBES)
        found = [el for el in frame.query_selector_all("[data-evo-probe]") if el.is_visible()]
        frame.evaluate(_APP_MARK_NONE_JS)
        return found

    els = touchable()
    if not els:
        out.append(f"{width}px: 小程序里没有能动手的东西（按钮、输入框、画布，或者带点击处理的元素和图形）")
    else:
        changed = False
        names: list[str] = []
        shots: list[str] = []
        for i in range(len(els)):
            now = touchable()
            if i >= len(now):
                break
            el = now[i]
            before = frame.evaluate(_APP_SNAP_JS)
            name = el.evaluate(_APP_NAME_JS)[:30]
            try:
                _click(page, el)
            except Exception as exc:  # noqa: BLE001 - a disabled / covered element is skipped
                log.debug("render check: could not click %s in the app: %s", el, exc)
                continue
            page.wait_for_timeout(SETTLE_MS)
            changed = changed or frame.evaluate(_APP_SNAP_JS) != before
            names.append(name)
            shots.append(frame.evaluate(_APP_TEXT_JS))
        for el in touchable()[:3] if not changed else ():   # an app you drag does nothing when it is only clicked
            before = frame.evaluate(_APP_SNAP_JS)
            box = el.bounding_box()
            if box is None:
                continue
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + 60, y, steps=4)
            page.mouse.up()
            page.wait_for_timeout(SETTLE_MS)
            changed = frame.evaluate(_APP_SNAP_JS) != before
            if changed:
                break
        if not changed:
            out.append(f"{width}px: 点了也拖了小程序里的 {len(els)} 个地方，画面都没有变化")
        out += [f"{width}px: 动手之后：{c}" for c in frame.evaluate(_APP_CLIPPED_JS) if f"{width}px: {c}" not in out]
        readings.append("小程序：" + " | ".join(_lines(first))[:READING_FIRST_CHARS])
        readings += [f"动「{n}」：{x[:READING_CHARS]}" for n, x in zip(names, _varying(first, shots), strict=True)]
    rejected = int(page.evaluate("() => (window.__evoui && window.__evoui.rejected) ? window.__evoui.rejected() : 0"))
    if rejected:
        out.append(f"{width}px: 小程序发来 {rejected} 条不合规的消息（已丢弃，见 console）")
    return out, over, readings


def check_block_with_browser(browser: Any, block_id: str, block: EvoUIBlock, images: Mapping[str, str] | None = None,
                             touch: Sequence[AppProbe] = ()) -> RenderCheck:
    probes = block_probes(block)
    if (problem := guard_problem(browser, block)) is not None:
        return RenderCheck(interactive_id=block_id, ok=False, probes_total=len(probes),
                           detail=_refusal(problem)[:1000])
    html = harness_html(block, images)
    readings: list[str] = []
    app = has_app(block)
    app_notes: list[str] = []
    cdn_ok: list[str] = []
    cdn_failed: list[str] = []
    console: list[str] = []
    errors: list[str] = []
    requests: list[str] = []
    notes: list[str] = []
    clipped: list[str] = []
    blanks: list[str] = []
    overflow = 0
    drawing: list[str] = []             # what is wrong with the picture itself
    failed: dict[int, str] = {}
    for width in BLOCK_WIDTHS:
        run = _Run()
        context = browser.new_context(viewport={"width": width, "height": 800},
                                      service_workers="block")
        if app:
            context.add_init_script(_HANDLER_HOOK_JS)
        try:
            page = _load(context, run, html, cdn=app)
            if page.evaluate(_OPEN_DEEP_DIVES_JS):
                page.wait_for_timeout(SETTLE_MS)        # the opened parts' enter motion
            st = page.evaluate(_BLOCK_STATE_JS)
            if not st["rendered"]:
                errors.append(f"{width}px: 块没有渲染出来")
            elif st["fallback"]:
                errors.append(f"{width}px: 渲染出错，显示了 fallback 文字")
            elif re.search(r"\bNaN\b", st["text"]):
                errors.append(f"{width}px: 算出了 NaN：有一个值不是数字（比如列表越界、拿文字做运算）")
            overflow = max(overflow, int(page.evaluate(_OVERFLOW_JS)))
            if st["rendered"]:
                clipped += [f"{width}px {c}" for c in page.evaluate(_CLIPPED_JS)]
                if width == BLANK_WIDTH and (blank := _blank_note(page, "#evoui-mount .evoui")):
                    blanks.append(blank)
            if app and st["rendered"]:
                bad, inner, seen = _check_app(page, width, touch)
                app_notes += bad
                overflow = max(overflow, inner)
                if width == BLOCK_WIDTHS[-1]:
                    readings += seen
            if st["rendered"] and width == BLANK_WIDTH and not st["fallback"]:
                drawing += page.evaluate(_SVG_PROBLEMS_JS)
                drawing += _touch_marks(page)
            for i, (frm, to) in enumerate(probes):
                if not st["rendered"]:
                    break
                r = page.evaluate(_BLOCK_PROBE_JS, [block.state, frm, to])
                if r["fallback"]:
                    failed.setdefault(i, f"{width}px: 把 {to} 设上之后渲染出错（显示了 fallback）")
                elif not r["changed"]:
                    failed.setdefault(i, f"{width}px: 把 {to} 设上之后，块里的文字和图都没有变化")
                elif width == BLANK_WIDTH:             # another branch or an extreme value shows other labels
                    drawing += page.evaluate(_SVG_PROBLEMS_JS)
            if st["rendered"] and probes and width == BLOCK_WIDTHS[-1]:
                readings += _readings(page, block, probes)
            page.close()
        except Exception as exc:  # noqa: BLE001 - the page never loaded
            errors.append(f"{width}px 打不开：{exc}")
        finally:
            context.close()
        console += run.console
        errors += run.errors
        requests += run.requests
        cdn_ok += run.cdn
        cdn_failed += run.cdn_failed
    over = overflow if overflow > OVERFLOW_TOLERANCE_PX else 0
    notes += app_notes + list(failed.values()) + blanks + list(dict.fromkeys(drawing))[:5]
    if clipped:
        notes.append("文字被裁掉（容器 overflow hidden 放不下）：" + "；".join(dict.fromkeys(clipped))[:300])
    if over:
        notes.append(f"横向溢出 {over}px")
    if requests:
        notes.append("发起了网络请求：" + ", ".join(dict.fromkeys(requests))[:200])
    if console:
        notes.append("控制台报错：" + "；".join(dict.fromkeys(console))[:200])
    if errors:
        notes.append("出错：" + "；".join(dict.fromkeys(errors))[:200])
    if cdn_failed:          # allowed, but say so: the app may look broken without its library
        notes.append("允许的 CDN 请求没有成功（不算失败，可能是离线）：" + ", ".join(dict.fromkeys(cdn_failed))[:300])
    elif cdn_ok:
        notes.append("从允许的 CDN 加载了：" + ", ".join(dict.fromkeys(cdn_ok))[:200])
    ok = (not console and not errors and not requests and not over and not failed and not app_notes
          and not clipped and not blanks and not drawing)
    return RenderCheck(interactive_id=block_id, ok=ok, console_errors=_clip(console),
                       page_errors=_clip(errors), network_requests=len(requests), overflow_px=over,
                       probes_passed=len(probes) - len(failed), probes_total=len(probes),
                       detail="；".join(notes)[:1000], readings=tuple(_budget(readings)))


def _operate(page: Any, frame: Any, p: AppProbe) -> str | None:
    """One author's probe inside the frame: None when the element was there and could be operated."""
    el = frame.query_selector(p.target)
    if el is None:
        return f"找不到 {p.target}"
    try:
        if p.action == "input":
            el.evaluate("(e,v)=>{e.value=v;e.dispatchEvent(new Event('input',{bubbles:true}));"
                        "e.dispatchEvent(new Event('change',{bubbles:true}))}", p.value or "")
        elif p.action == "drag":
            el.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
            box = el.bounding_box()
            if box is None:
                return f"{p.target} 没有显示出来"
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            dx, dy = p.by or (60, 0)
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + dx, y + dy, steps=8)
            page.mouse.up()
        else:
            el.click(timeout=ACTION_TIMEOUT_MS, force=True)
    except Exception as exc:  # noqa: BLE001 - what could not be done is what the author is told
        return f"{p.action} {p.target} 没做成：{str(exc).splitlines()[0][:120]}"
    return None


def _frame_of(page: Any) -> Any:
    page.wait_for_function(_APP_READY_JS, timeout=APP_READY_MS)
    handle = page.locator("#evoui-mount .evoui-app iframe").first.element_handle()
    return handle.content_frame() if handle else None


LOOK_SETTLE_MS = 900             # a transition or a short animation after an operation
LOOK_WIDTH = 720
LOOK_NARROW = 375
LOOK_QUALITY = 78
LOOK_STATES = 2                  # controls tried in a block's look


def look_block_with_browser(browser: Any, block: EvoUIBlock, probes: Sequence[AppProbe],
                            images: Mapping[str, str] | None = None) -> BlockLook:
    html = harness_html(block, images)
    shots: list[tuple[str, str]] = []
    notes: list[str] = []

    def shot(page: Any, name: str) -> None:
        page.wait_for_timeout(LOOK_SETTLE_MS)
        raw = page.screenshot(type="jpeg", quality=LOOK_QUALITY, full_page=True)
        shots.append((name, base64.b64encode(raw).decode("ascii")))

    app = has_app(block)

    def opened(width: int, scheme: str) -> tuple[Any, Any, Any]:
        context = browser.new_context(viewport={"width": width, "height": 900}, service_workers="block",
                                      color_scheme=scheme)
        page = _load(context, _Run(), html, cdn=True)
        return context, page, _frame_of(page) if app else None

    context, page, frame = opened(LOOK_WIDTH, "light")
    try:
        shot(page, "刚打开")
        for i, p in enumerate(probes, 1):
            before = frame.evaluate(_APP_SNAP_JS)
            why = _operate(page, frame, p)
            page.wait_for_timeout(SETTLE_MS)
            if why is None and frame.evaluate(_APP_SNAP_JS) == before:
                why = "做了，画面没有变化"
            notes.append(f"操作 {i}（{p.action} {p.target}）：{why or '做了，画面变了'}")
            shot(page, f"操作 {i} 之后：{p.see}"[:80])
        if not app:
            page.evaluate(_OPEN_DEEP_DIVES_JS)
            controls_tried = block_probes(block)[:LOOK_STATES]
            for _, to in controls_tried:
                page.evaluate(_SET_STATES_JS, [block.state, to])
                shot(page, f"把 {_setting(to)} 之后")
            if not controls_tried and page.evaluate(_MARKS_JS):
                box = page.evaluate(_MARK_BOX_JS, 0)
                if box:
                    page.mouse.click(box["x"], box["y"])
                    shot(page, "点了图上第一个能点的地方之后")
    finally:
        context.close()
    for name, width, scheme in (("最窄的窗格，刚打开", NARROW_BLOCK, "light"), ("深色模式，刚打开", LOOK_WIDTH, "dark")):
        context, page, _ = opened(width, scheme)
        try:
            shot(page, name)
        finally:
            context.close()
    return BlockLook(shots=tuple(shots), notes=tuple(notes))


def look_interactive_with_browser(browser: Any, draft: InteractiveDraft) -> BlockLook:
    """A library component opened as the reader gets it, after its own probes, and narrow."""
    shots: list[tuple[str, str]] = []
    notes: list[str] = []

    def shot(page: Any, name: str) -> None:
        page.wait_for_timeout(LOOK_SETTLE_MS)
        raw = page.screenshot(type="jpeg", quality=LOOK_QUALITY, full_page=True)
        shots.append((name, base64.b64encode(raw).decode("ascii")))

    for name, width in (("刚打开", LOOK_WIDTH), ("窄屏 375px，刚打开", LOOK_NARROW)):
        context = browser.new_context(viewport={"width": width, "height": 900}, service_workers="block")
        try:
            page = _load(context, _Run(), draft.html)
            shot(page, name)
            if width == LOOK_WIDTH:
                for i, p in enumerate(draft.probes, 1):
                    why = _probe(page, p)
                    notes.append(f"操作 {i}（{p.action} {p.target}）：{why or '画面变了'}")
                    shot(page, f"操作 {i} 之后")
        finally:
            context.close()
    return BlockLook(shots=tuple(shots), notes=tuple(notes))


def _browser_pid(browser: Any) -> list[int]:
    """The browser's process id, for killing it at the deadline (Playwright's API does not expose it; CDP does)."""
    try:
        info = browser.new_browser_cdp_session().send("SystemInfo.getProcessInfo")
    except Exception as exc:  # noqa: BLE001 - a browser without CDP: the deadline still answers, the thread stays stuck
        log.warning("render check could not read the browser's pid: %s", exc)
        return []
    return [p["id"] for p in info["processInfo"] if p["type"] == "browser"]


class PlaywrightChecker:
    """`RenderChecker` over headless Chromium. One browser per `check`, on a fresh thread of its own:
    sync Playwright objects must stay on the thread that made them, and refuse a thread that already
    runs an asyncio loop (a server thread, another Playwright)."""

    def __init__(self, launch: Callable[[Any], Any] | None = None) -> None:
        self._launch = launch or (lambda pw: pw.chromium.launch(headless=True))

    def check(self, draft: InteractiveDraft) -> RenderCheck:
        return self._run(lambda b: check_with_browser(b, draft), draft.interactive_id,
                         len(draft.probes))

    def check_block(self, block_id: str, block: EvoUIBlock, images: Mapping[str, str] | None = None,
                    touch: Sequence[AppProbe] = ()) -> RenderCheck:
        """`BlockChecker`: the EvoUI block rendered by evoui.js (see the module doc)."""
        return self._run(lambda b: check_block_with_browser(b, block_id, block, images, touch), block_id,
                         len(block_probes(block)))

    def look_block(self, block_id: str, block: EvoUIBlock, probes: Sequence[AppProbe],
                   images: Mapping[str, str] | None = None) -> BlockLook:
        """`BlockChecker`: real screenshots of the block; empty (with the reason) when it cannot be opened."""
        got: list[BlockLook] = []

        def fn(b: Any) -> RenderCheck:
            got.append(look_block_with_browser(b, block, probes, images))
            return RenderCheck(interactive_id=block_id, ok=True)
        c = self._run(fn, block_id, len(probes))
        return got[0] if got else BlockLook(notes=(c.detail or "打不开，没有截图",))

    def look_interactive(self, draft: InteractiveDraft) -> BlockLook:
        got: list[BlockLook] = []

        def fn(b: Any) -> RenderCheck:
            got.append(look_interactive_with_browser(b, draft))
            return RenderCheck(interactive_id=draft.interactive_id, ok=True)
        c = self._run(fn, draft.interactive_id, len(draft.probes))
        return got[0] if got else BlockLook(notes=(c.detail or "打不开，没有截图",))

    def _run(self, fn: Callable[[Any], RenderCheck], rid: str, probes: int) -> RenderCheck:
        got: list[RenderCheck] = []
        browser_pid: list[int] = []

        def work() -> None:
            got.append(self._in_thread(fn, rid, probes, browser_pid))

        worker = threading.Thread(target=work, name="render-check", daemon=True)
        worker.start()
        worker.join(CHECK_DEADLINE_S)
        if not worker.is_alive():
            return got[0]
        log.warning("render check of %s passed its deadline; killing its browser", rid)
        for pid in browser_pid:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        worker.join(10)         # the dead browser ends the blocked call; a thread still stuck is a daemon, and is logged
        if worker.is_alive():
            log.error("render check thread of %s is still stuck after its browser was killed", rid)
        return RenderCheck(interactive_id=rid, ok=False, probes_total=probes,
                           detail=f"渲染检查超时（{CHECK_DEADLINE_S} 秒）：这个块算得太久，把页面卡住了，减少数据量或循环次数")

    def _in_thread(self, fn: Callable[[Any], RenderCheck], rid: str, probes: int,
                   browser_pid: list[int] | None = None) -> RenderCheck:
        def unavailable(why: str) -> RenderCheck:
            return RenderCheck(interactive_id=rid, ok=False, probes_total=probes, detail=why[:1000])
        try:
            from playwright.sync_api import sync_playwright  # optional extra `deep`
        except ImportError:
            return unavailable("渲染检查不可用：没有安装 playwright（uv sync --extra deep）")
        try:
            with sync_playwright() as pw:
                browser = self._launch(pw)
                if browser_pid is not None:
                    browser_pid.extend(_browser_pid(browser))
                try:
                    return fn(browser)
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001 - no browser (e.g. chromium not installed)
            log.warning("render check could not start a browser: %s", exc)
            return unavailable(f"渲染检查不可用：{exc}（uv run playwright install chromium）")
