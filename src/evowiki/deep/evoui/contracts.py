"""EvoUI block contract (evoui-blocks). Pure data — no IO, no LLM.

An EvoUI block is a JSON object that describes one interactive with a fixed set of node kinds and a
small, safe expression language (`deep/evoui/spec.md` is the format a model reads). It is never HTML and
never contains script: every text field is plain text (renderers set it as text, never as markup), every
computation is an expression in the grammar the validators parse (`deep.evoui.validate` in Python,
`EvoUI.validate` in `deep/evoui/evoui.js`; the two are kept in lock-step by a shared test corpus), and
only `evoui.js` turns a block into DOM — the one renderer the web app, the Obsidian plugin and the
backend render check all use.

The structure tables below (node kinds and their fields, plot shapes, tones, size caps) are what both
validators enforce. Changing one is a cross-lane change: do it in its own commit and update `evoui.js`."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EVOUI_TYPE = "evoui"
EVOUI_FENCE = "evoui"          # a note carries a block as a ```evoui fence whose content is the block JSON

Tone = Literal["default", "secondary", "accent", "success", "warning", "danger"]
TONES: tuple[str, ...] = ("default", "secondary", "accent", "success", "warning", "danger")
BOX_TONES: tuple[str, ...] = ("plain", "surface")

# kind -> (required fields, optional fields)
_BOX_STYLE: tuple[str, ...] = ("background", "width", "height", "flex", "radius", "padding", "align", "color", "clip", "click")
NODE_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "box": (("children",), ("gap", "border", "tone", "justify", *_BOX_STYLE)),
    "row": (("children",), ("gap", "justify", "wrap", *_BOX_STYLE)),
    "grid": (("children",), ("columns", "gap")),
    "grid-item": (("children",), ("span", "gap", "border", "tone", *_BOX_STYLE)),
    "text": (("text",), ("size", "weight", "tone", "color", "align", "rich")),
    "title": (("text",), ("size", "tone", "color")),
    "caption": (("text",), ("align",)),
    "badge": (("text",), ("tone",)),
    "math": (("tex",), ("display",)),
    "table": (("columns", "rows"), ("caption",)),
    "icon": (("name",), ("tone",)),
    "divider": ((), ()),
    "slider": (("bind", "min", "max", "step"), ("label",)),
    "input": (("bind", "min", "max", "step"), ("label",)),
    "choice": (("bind", "options"), ("style",)),
    "select": (("bind", "options"), ("label",)),
    "toggle": (("bind",), ("label",)),
    "checkbox": (("bind",), ("label",)),
    "button": (("label", "set"), ("tone",)),
    "if": (("cond", "then"), ("else",)),
    "plot": (("xmin", "xmax", "ymin", "ymax", "items"), ("grid", "height", "legend")),
    "chart": (("chart", "data", "x", "series"), ("annotations", "caption", "curve", "donut", "gradient", "hover",
                                                 "stack", "visible_points", "x_label", "y_label", "y_min")),
    "quiz": (("questions",), ("cases", "id", "layout", "title")),
    "flashcards": (("cards",), ("id", "shuffle", "title")),
    "reveal": (("children",), ("after", "id", "label", "mode")),
    "animate": (("children",), ("effect",)),
    "transition": (("children", "key"), ()),
    "celebrate": (("when",), ()),
    "timer": (("mode",), ("autostart", "bind", "id", "label", "seconds")),
    "player": (("bind", "min", "max", "step"), ("id", "interval_ms", "label", "loop")),
    "pager": (("children",), ("gate", "id")),
    "fill-blank": (("sentence", "options", "correct", "why"), ("cell", "id")),
    "sentence": (("sentence", "words", "answer", "why"), ("cell", "id")),
    "route": (("prompt", "options"), ("bind", "id", "preference")),
    "app": (("html",), ("height", "id", "images", "items")),
    "svg": (("viewBox", "children"), ("images", "tween")),
    "each": (("in", "children"), ("as", "index")),
    "timeline": (("events",), ()),
    "deep-dive": (("children",), ("label",)),
}
# shape -> (required fields, optional fields)
SHAPE_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "arrow": (("x1", "y1", "x2", "y2"), ("tone", "label", "dashed")),
    "point": (("x", "y"), ("tone", "label")),
    "polyline": (("points",), ("tone", "dashed")),
    "segment": (("x1", "y1", "x2", "y2"), ("tone", "dashed")),
    "label": (("x", "y", "text"), ("tone",)),
    "rect": (("x", "y", "w", "h"), ("tone", "fill", "dashed", "label")),
    "circle": (("cx", "cy", "r"), ("tone", "fill", "dashed", "label")),
    "text": (("x", "y", "text"), ("tone", "anchor", "size")),
}
# Stateful learning nodes: each may carry `id` (NODE_ID_RE, unique in the block) and gets a node key —
# its `id`, else `<kind>#<n>` (n = index among the block's nodes of that kind in document order:
# children, then `then`, then `else`). Progress and result events are filed under it
# (`deep.evoui.node_keys`; evoui.js `STATEFUL` / `keyMap`, same list in the same order). The full D1
# list is here from the start so the backend grader can key nodes later tasks add; a kind takes `id`
# only once its NODE_FIELDS entry lists it (a kind not in NODE_FIELDS is refused before this rule).
STATEFUL_KINDS: tuple[str, ...] = ("quiz", "flashcards", "reveal", "pager", "fill-blank", "sentence",
                                   "timer", "route", "player", "app")
NODE_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
# `bind` names a state. A `player` is a control too: it steps a number state from min to max (the render
# check's automatic probe sets it min -> max like a slider's). A `route` may bind a state too (optional):
# a choice sets it to the option's value (the automatic probe sets it first -> last option like a choice's).
CONTROL_KINDS = ("slider", "input", "choice", "select", "toggle", "checkbox", "player", "route")
NUMBER_CONTROLS = ("slider", "input", "player")    # bind a number state, clamp to [min, max]
BOOL_CONTROLS = ("toggle", "checkbox")             # bind a true/false state
OPTION_CONTROLS = ("choice", "select")             # options [{label, value}]
TEXT_SIZES: tuple[str, ...] = ("xs", "sm", "md", "lg")
TITLE_SIZES: tuple[str, ...] = ("md", "lg", "xl", "2xl")
CHART_KINDS: tuple[str, ...] = ("line", "area", "bar", "scatter", "pie")
# chart options and the chart kinds that take them (a field on another kind is refused): `stack` (bool) bar /
# area; `curve` line / area; `gradient` (bool) area; `hover` (a number state: the hovered point's index, -1
# when none), `visible_points` (int >= MIN_WINDOW: points shown at once, the rest scrolls) and `annotations`
# (<= MAX_ANNOTATIONS: {x | y: expression, label: template, tone?}) line / area / bar; `donut` (bool) pie.
CHART_CURVES: tuple[str, ...] = ("linear", "smooth", "step")      # default "linear"
CARTESIAN_CHARTS: tuple[str, ...] = ("line", "area", "bar")
CHART_OPTION_KINDS: dict[str, tuple[str, ...]] = {
    "stack": ("bar", "area"), "curve": ("line", "area"), "gradient": ("area",), "hover": CARTESIAN_CHARTS,
    "visible_points": CARTESIAN_CHARTS, "annotations": CARTESIAN_CHARTS, "donut": ("pie",)}
ANNOTATION_FIELDS: tuple[str, ...] = ("x", "y", "label", "tone")    # exactly one of x / y; label required
ICONS: tuple[str, ...] = ("arrow-down", "arrow-up", "arrow-left", "arrow-right", "check", "cross",
                          "dot", "plus", "minus", "info", "warning")
ANCHORS: tuple[str, ...] = ("start", "middle", "end")
CELL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.[a-z_]{1,32}$")   # a quiz question's K cell (concept.aspect)
# quiz: `layout` "list" (default: every question at once + one 「检查答案」) or "paged" (one question per
# page, judged on click, results page); a question's fields and its `type` (single default / multi with
# `correct` a list / tf with exactly 2 options)
QUIZ_LAYOUTS: tuple[str, ...] = ("list", "paged")
QUESTION_FIELDS: tuple[tuple[str, ...], tuple[str, ...]] = (
    ("q", "options", "correct", "why"), ("case", "cell", "type", "feedback", "hint"))
QUESTION_TYPES: tuple[str, ...] = ("single", "multi", "tf")
# flashcards: `cards` [{front, back, cell?, hint?}] (front / back / hint plain text; `cell` a K cell like
# a quiz question's); `shuffle` (bool, default true: show the shuffle button); `title` plain text
CARD_FIELDS: tuple[tuple[str, ...], tuple[str, ...]] = (("front", "back"), ("cell", "hint"))
# Practice nodes: the kinds whose "done" a `reveal` (mode "after", `after` = the node's id) waits for and a
# gated `pager` page needs before it lets the reader on. One table both validators read (evoui.js `PRACTICE`).
PRACTICE_KINDS: tuple[str, ...] = ("quiz", "flashcards", "fill-blank", "sentence", "pager")
# fill-blank / sentence: the blank in `sentence` is three underscores; fill-blank has exactly one, a
# sentence builder 1–MAX_BLANKS. fill-blank `options` [{label, why?}]; route `options` [{label, value,
# next, desc?}] (value: text ≤ MAX_ROUTE_VALUE_CHARS, a number or true/false, distinct; `next` is the
# instruction a host may send to the model as the next turn); route `preference` a key like NODE_ID_RE.
BLANK = "___"
FILL_OPTION_FIELDS: tuple[tuple[str, ...], tuple[str, ...]] = (("label",), ("why",))
ROUTE_OPTION_FIELDS: tuple[tuple[str, ...], tuple[str, ...]] = (("label", "value", "next"), ("desc",))
# timeline: `events` [{title, when?, text?, tone?}] in order (all plain text; `when` a date / stage label, an event
# without one is numbered), at least MIN_EVENTS. deep-dive: `children` (at least 1) folded under a summary line
# `label` (plain text, default 「深入一点」) the reader opens on demand; not stateful (nothing recorded).
# The design follows DeepTutor's book blocks (HKUDS/DeepTutor deeptutor/book/blocks timeline.py / deep_dive.py,
# Apache-2.0): ideas only, no code copied.
TIMELINE_EVENT_FIELDS: tuple[tuple[str, ...], tuple[str, ...]] = (("title",), ("text", "tone", "when"))
MIN_EVENTS = 2
DEEP_DIVE_LABEL = "深入一点"
REVEAL_MODES: tuple[str, ...] = ("click", "steps", "after")   # default "click"
ANIMATE_EFFECTS: tuple[str, ...] = ("rise", "fade")           # default "rise"
TIMER_MODES: tuple[str, ...] = ("countdown", "stopwatch")
BUILTIN_NAMES: tuple[str, ...] = (
    "sin", "cos", "tan", "atan2", "sqrt", "abs", "min", "max", "round", "floor", "ceil", "exp", "log",
    "pow", "PI", "fmt", "deg2rad", "range", "map", "sum", "len", "iterate",
    "asin", "acos", "atan", "hypot", "sign", "log2", "log10", "clamp", "lerp", "remap", "rand", "filter", "fold", "slice",
    "concat", "reverse", "sort", "put", "swap", "repeat", "indexOf", "has", "join", "str", "arc")
# A drawing's elements (`svg` node): real SVG names, attributes are numbers or templates; arrow / node / plot are ours.
SVG_ELS: dict[str, tuple[str, ...]] = {
    "g": (), "path": ("d",), "rect": ("width", "height"), "circle": ("r",), "ellipse": ("rx", "ry"), "line": (),
    "polyline": ("points",), "polygon": ("points",), "text": ("text",), "image": ("src", "width", "height"),
    "arrow": ("x1", "y1", "x2", "y2"), "node": ("width", "height"), "plot": ("x", "y"), "defs": (), "clipPath": ("id",),
    "animate": ("attributeName", "dur"), "animateTransform": ("attributeName", "dur", "type"),
    "animateMotion": ("dur", "path")}
SVG_ATTRS = frozenset({
    "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "d", "points", "transform", "fill",
    "stroke", "stroke-width", "stroke-dasharray", "stroke-dashoffset", "stroke-linecap", "stroke-linejoin", "opacity",
    "fill-opacity", "stroke-opacity", "clip-path", "text-anchor", "dominant-baseline", "font-size", "font-weight",
    "font-style", "attributeName", "values", "from", "to", "by", "dur", "begin", "repeatCount", "keyTimes", "calcMode",
    "type", "path", "rotate", "text", "wrap", "tip", "bend", "both", "label", "src", "alt", "id", "color", "left", "top",
    "grid", "xlabel", "ylabel"})
SVG_BOOL = ("grid", "both")
SMIL_ATTRS = ("x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "d", "points", "transform",
              "fill", "stroke", "opacity", "fill-opacity", "stroke-opacity", "stroke-width", "stroke-dashoffset")
SVG_KIDS = ("g", "plot", "defs", "clipPath", "rect", "circle", "ellipse", "path", "line", "polygon", "polyline", "text",
            "image", "node", "arrow")
SVG_COLORS = ("transparent", "blue", "orange", "green", "red", "yellow", "purple", "teal", "gray", "blue-soft", "orange-soft", "green-soft",
              "red-soft", "yellow-soft", "purple-soft", "teal-soft", "gray-soft", "text", "muted", "line", "bg", "surface",
              "none", "currentColor", "info", "success", "warning", "danger", "discovery", "secondary", "default", "subtle",
              "primary", "surface-secondary", "surface-tertiary")
COLOR_ATTRS = ("fill", "stroke", "color", "background")
COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\([0-9.,%\s/]+\))$")
PATH_RE = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE\s,.+-]*$")
VIEWBOX_RE = re.compile(r"^-?[0-9.]+[ ,]+-?[0-9.]+[ ,]+[0-9.]+[ ,]+[0-9.]+$")


# size caps (both validators)
MAX_BLOCK_CHARS = 40_000      # the block as compact JSON
MAX_NODES = 400
MAX_DEPTH = 16
MAX_EXPR_CHARS = 500          # one expression or one template
MAX_TEXT_CHARS = 600          # one plain text (title, option label, fallback is separate)
MAX_STATE = 32
MAX_DERIVED = 48
MAX_PLOT_ITEMS = 60
MIN_CARDS = 2                 # flashcards: at least 2 cards (how many more is the writer's call: prompt guidance)
MAX_REVEAL_STEPS = 12         # reveal: 1–12 children (steps)
MAX_TIMER_SECONDS = 3600      # timer countdown: 1–3600 s
MIN_INTERVAL_MS = 100         # player interval_ms: 100–5000
MAX_INTERVAL_MS = 5000
MAX_BLANKS = 8                # sentence builder: 1–8 blanks, N–12 words (each ≤ 60 characters)
MAX_WORDS = 12
MAX_WORD_CHARS = 60
MIN_ROUTE_OPTIONS = 2         # route: at least 2 options
MAX_ROUTE_VALUE_CHARS = 64
MAX_OPTIONS = 8
MAX_FALLBACK_CHARS = 1500
MAX_COLUMNS = 4               # grid columns / grid-item span
MAX_ANNOTATIONS = 8           # chart annotations
MIN_WINDOW = 3                # chart window: at least 3 points shown
MAX_EXPR_ROWS = 200           # render guard: a table whose rows are an expression shows at most this many
MAX_CASES = 6                 # diagnostic quiz cases
MAX_TEX_CHARS = 600           # one math formula (plain LaTeX, no templates)

# ---- app: the sandboxed HTML mini-app (EvoUI v2 tier 4) ----
# `app` {html, height?, items?, id?}: one complete self-contained HTML mini-app, rendered by evoui.js in
# `<iframe sandbox="allow-scripts">` whose srcdoc starts with a CSP meta (APP_CSP), the host tokens and the
# bridge script (`window.evowiki`). The iframe sandbox + this CSP are the boundary; the static scan below
# (both validators, rule for rule) is defence in depth. At most one app per block; the block cap
# (MAX_BLOCK_CHARS) counts the block with every app's `html` replaced by "". evoui.js holds the same
# constants (`EvoUI.APP`); tests/deep/test_evoui_parity.py compares them.
APP_CDN_ALLOWLIST: tuple[str, ...] = ("https://cdn.jsdelivr.net/", "https://unpkg.com/",
                                      "https://cdnjs.cloudflare.com/")
_CDNS = " ".join(APP_CDN_ALLOWLIST)
APP_CSP = (f"default-src 'none'; script-src 'unsafe-inline' {_CDNS}; style-src 'unsafe-inline' {_CDNS}; "
           f"img-src data: blob:; font-src data: {_CDNS}; media-src data: blob:; connect-src 'none'; "
           "form-action 'none'; base-uri 'none'")
APP_SANDBOX = "allow-scripts"         # never allow-same-origin / top-navigation / popups / forms / modals
MAX_APP_HTML = 60_000                 # html: 1–60000 characters
MIN_APP_HEIGHT = 200                  # height: a whole number 200–900 (px), default 420
MAX_APP_HEIGHT = 900
DEFAULT_APP_HEIGHT = 420
MIN_APP_RESIZE = 200                  # evowiki.resize(h) is clamped to 200–1200
MAX_APP_RESIZE = 1200
MAX_APP_ITEMS = 40                    # items: 0–40 [{id, label, cell?}]
MAX_APP_LABEL = 120                   # an item's label: plain text ≤ 120
MAX_APP_MESSAGE = 2000                # one bridge message as JSON (an item's response included)
MAX_APP_SAVE = 16000                  # evowiki.save(progress): the progress as JSON
MAX_APP_EVENTS = 500                  # accepted bridge messages per render
APP_READY_MS = 8000                   # no evowiki.ready() within 8 s: the block's fallback shows under the frame
MAX_APP_LATENCY_MS = 3_600_000        # any item's latencyMs is at most 1 h (BlockEventIn bound; evoui.js clamps in emit)
MAX_APP_ITEM_REPORTS = 3              # accepted item reports per item id per render (repeats beyond are dropped)
APP_ITEM_FIELDS: tuple[tuple[str, ...], tuple[str, ...]] = (("id", "label"), ("cell",))
# images: real pictures the app draws with `evowiki.image(id)` (a blob: URL; the bytes come through the host, never
# from the network). The block carries each one's attribution, shown under the frame; `asset` is set when it is stored.
MAX_APP_IMAGES = 4
APP_IMAGE_FIELDS: tuple[tuple[str, ...], tuple[str, ...]] = (("id", "title", "license", "source", "source_url"),
                                                             ("alt", "asset", "author", "license_url"))
# The static scan. Regex sources are shared verbatim with evoui.js (flags: "i" = ignore case; Python
# compiles them with re.ASCII so both engines see the same characters). `_WS` is JavaScript's own
# whitespace, so `document .cookie` with any blank in between still matches.
_WS = r"[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]"
_NWS = r"\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_Q = "\"'`"
# (pattern, flags, problem) — any match refuses the html
APP_FORBIDDEN: tuple[tuple[str, str, str], ...] = (
    (r"<(?:iframe|object|embed|base)(?![A-Za-z0-9_-])", "i", "no <iframe>, <object>, <embed> or <base>"),
    (r"<meta(?![A-Za-z0-9_-])[^>]*http-equiv", "i", "no <meta http-equiv>"),
    (rf"(?<![A-Za-z0-9_$])document{_WS}*\.{_WS}*cookie(?![A-Za-z0-9_$])", "", "no document.cookie"),
    (r"(?<![A-Za-z0-9_$])(?:localStorage|sessionStorage|indexedDB)(?![A-Za-z0-9_$])", "",
     "no localStorage / sessionStorage / indexedDB (keep progress with evowiki.save)"),
    ((rf"(?<![A-Za-z0-9_$.])(?:window|self|globalThis){_WS}*(?:\.{_WS}*|\[{_WS}*[{_Q}])"
      r"(?:parent|top|opener|frameElement)(?![A-Za-z0-9_$])"), "",
     "no window.parent / window.top (talk to the page through window.evowiki)"),
    (rf"(?<![A-Za-z0-9_$.#-])(?:parent|top|opener){_WS}*[.\[]", "",
     "no parent. / top. access (talk to the page through window.evowiki; do not name a variable parent or top)"),
    ((rf"(?<![A-Za-z0-9_$])location{_WS}*(?:=(?![=>])|\.{_WS}*(?:(?:href|search|pathname|host|hostname|port|protocol)"
      rf"{_WS}*=(?![=>])|(?:assign|replace|reload)(?![A-Za-z0-9_$])))"), "",
     "no navigation (location = / location.href = / assign / replace / reload): the app stays on its page"),
    (r"(?<![A-Za-z0-9_$])(?:webkit|moz)?RTC(?:PeerConnection|DataChannel)(?![A-Za-z0-9_$])", "", "no WebRTC"),
    (r"<link(?![A-Za-z0-9_-])[^>]*(?:dns-prefetch|preconnect|prefetch|prerender)", "i",
     "no <link> dns-prefetch / preconnect / prefetch / prerender"),
)
# (pattern whose group 1 is a URL, flags) — a URL that names a scheme or starts with // must be data: /
# blob: / javascript: / about: or start with an allowlisted CDN (APP_CDN_ALLOWLIST, case ignored)
APP_URL_PATTERNS: tuple[tuple[str, str], ...] = (
    (rf"(?<![A-Za-z0-9_$-])(?:src|href|srcset|poster){_WS}*={_WS}*\"([^\"]*)\"", "i"),
    (rf"(?<![A-Za-z0-9_$-])(?:src|href|srcset|poster){_WS}*={_WS}*'([^']*)'", "i"),
    (rf"(?<![A-Za-z0-9_$-])(?:src|href|srcset|poster){_WS}*={_WS}*`([^`]*)`", "i"),
    (rf"(?<![A-Za-z0-9_$-])(?:src|href|srcset|poster){_WS}*={_WS}*([^{_Q}>{_NWS}]+)", "i"),
    (rf"(?<![A-Za-z0-9_$-])url{_WS}*\({_WS}*[{_Q}]?([^{_Q})]*)", ""),
    (rf"(?<![A-Za-z0-9_$.])import{_WS}*\(?{_WS}*[{_Q}]([^{_Q}]*)", ""),
    ((rf"(?<![A-Za-z0-9_$.])(?:import|export)(?![A-Za-z0-9_$])[^;{_Q}]*?(?<![A-Za-z0-9_$])from{_WS}*[{_Q}]"
      rf"([^{_Q}]*)"), ""),
)
APP_URL_EXTERNAL = r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|[/\\]{2})"     # names a scheme, or //host
APP_URL_LOCAL = r"^(?:data|blob|javascript|about):"               # flags "i"
APP_URL_TRIM = rf"^{_WS}+|{_WS}+$"
_ID =re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvoUIProbe(_M):
    """One scripted try for the render check: set these states, then the block's visible text must
    differ from before (a block whose probes are omitted gets two values per control automatically)."""

    set: dict[str, Any] = Field(min_length=1)


class EvoUIBlock(_M):
    """The block as a model writes it. Shape only — `deep.evoui.validate` checks the node tree, the
    expressions and the caps (this model holds `body` as a plain dict on purpose: its grammar is the
    validators', not pydantic's)."""

    type: Literal["evoui"]
    title: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=300)        # the understanding obstacle it addresses
    state: dict[str, Any]
    derived: dict[str, str]
    body: dict[str, Any]
    fallback: str = Field(min_length=1, max_length=MAX_FALLBACK_CHARS)   # plain text when not rendered
    probes: tuple[EvoUIProbe, ...] = Field(default=(), max_length=6)


class BlockDraft(_M):
    """A block a writer placed on a section (`{{block:<block_id>}}` in that section's body)."""

    block_id: str
    section_id: str
    why: str = Field(min_length=1, max_length=300)              # which understanding obstacle it solves
    block: EvoUIBlock

    @model_validator(mode="after")
    def _id(self) -> "BlockDraft":
        if not _ID.match(self.block_id):
            raise ValueError("block_id must be kebab-case [a-z0-9-], 2–64 chars")
        return self


def block_placeholder(block_id: str) -> str:
    return "{{block:" + block_id + "}}"
