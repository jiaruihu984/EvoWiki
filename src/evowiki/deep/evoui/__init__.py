"""EvoUI: declarative interactive blocks (contract in `contracts.py`, format in `spec.md`).

This directory is the single source of the format and its renderer:
- `evoui.js`  — the renderer and its validator (browser; web app, Obsidian plugin, render check);
- `evoui.css` — its stylesheet;
- `guard.js`  — the safety guard on top of the validator (browser; Obsidian plugin, render check);
- `spec.md`   — the format as a model reads it (prompts load it from here);
- `examples.json` — good blocks for prompts and tests;
- `validate.py` — the same validator in Python (kept equal by a parity test).

`harness_html` wraps one block in a standalone page (the render check opens it; it also serves as a
viewer). `block_fence` is a block inline (a ```evoui fence whose content is the block JSON: the
streaming preview); `asset_fence` is how a written page carries a stored block (```evoui + `asset: <id>`),
so the reader's answers and progress go back to that asset (`POST /wiki/assets/{id}/results`)."""

import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from evowiki.deep.evoui.contracts import CONTROL_KINDS, EVOUI_FENCE, STATEFUL_KINDS, EvoUIBlock
from evowiki.deep.evoui.validate import ExprError, parse, validate

HERE = Path(__file__).parent
JS_PATH = HERE / "evoui.js"
CSS_PATH = HERE / "evoui.css"
GUARD_PATH = HERE / "guard.js"
# the format's text (skills/write-page/references/evoui; see GROUPS below)
REFS_DIR = HERE.parent.parent / "skills" / "write-page" / "references" / "evoui"
EXAMPLES_PATH = HERE / "examples.json"
BLOCK_ELEMENT_ID = "evoui-block"

__all__ = ["CSS_PATH", "EXAMPLES_PATH", "GUARD_PATH", "JS_PATH", "ExprError", "asset_fence", "block_fence",
           "block_json", "controls", "examples", "harness_html", "node_keys", "parse", "spec_text",
           "validate"]


def spec_text() -> str:
    """The whole format: core, then every group (the served spec.md, the typed templates' guide,
    tests). A model reads it in parts (read_ref)."""
    return "\n\n".join((REFS_DIR / f"{n}.md").read_text(encoding="utf-8").strip()
                       for n in ("core", *GROUPS))


def templates() -> list[dict[str, Any]]:
    """The templates: [{name, use, block}] (examples.json), shown to the writer as patterns. Ordered
    most broadly useful first: `spec_for_prompt(max_examples=n)` keeps the first n (the tier-4 `app`
    template is last)."""
    return json.loads(EXAMPLES_PATH.read_text(encoding="utf-8"))


def examples() -> list[dict[str, Any]]:
    """The template blocks."""
    return [t["block"] for t in templates()]


def block_json(block: EvoUIBlock | dict[str, Any], *, indent: int | None = None) -> str:
    if isinstance(block, EvoUIBlock):
        data = block.model_dump(mode="json")
        if not data.get("probes"):
            data.pop("probes", None)
    else:
        data = block
    return json.dumps(data, ensure_ascii=False, indent=indent,
                      separators=None if indent else (",", ":"))


def stored_form(text: str) -> str | None:
    """The block in `text` (a note's indented fence or an asset file) as `block_json` stores it, so a fence and its
    stored asset compare equal; None when `text` is not a JSON object."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return block_json(data) if isinstance(data, dict) else None


def block_fence(block: EvoUIBlock | dict[str, Any]) -> str:
    """The note form: ```evoui + the block JSON (indented, readable) + ```."""
    return f"```{EVOUI_FENCE}\n{block_json(block, indent=1)}\n```"


def asset_fence(asset_id: str) -> str:
    """The note form of a stored block: ```evoui + `asset: <id>` + ``` (the plugin fetches and caches
    the block, and reports results / progress against that asset)."""
    return f"```{EVOUI_FENCE}\nasset: {asset_id}\n```"


def _script_json(text: str) -> str:
    return text.replace("<", "\\u003c")          # nothing inside can close the <script> element


def app_images(block: EvoUIBlock | dict[str, Any]) -> list[dict[str, Any]]:
    """The images every app node of the block declares (`app.images`), in document order."""
    d = block if isinstance(block, dict) else block.model_dump(mode="json")
    return [im for n in node_keys(d).values() if n.get("kind") == "app" for im in n.get("images") or ()]


_HARNESS_IMAGES = ("{image:(im)=>{const b=(window.__evouiImages||{})[im.id];if(!b)return null;"
                   "return{mime:'image/webp',bytes:Uint8Array.from(atob(b),c=>c.charCodeAt(0)).buffer};}}")


def harness_html(block: EvoUIBlock | dict[str, Any], images: Mapping[str, str] | None = None) -> str:
    """A standalone page that renders `block` with evoui.js / evoui.css (inline, offline). The handle
    `EvoUI.render` returns is `window.__evoui` ({state, set}). `images`: an app's `evowiki.image` answers,
    image id -> WebP in base64."""
    css = CSS_PATH.read_text(encoding="utf-8")
    js = JS_PATH.read_text(encoding="utf-8")
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<style>{css}\nhtml,body{{margin:0}}body{{padding:12px;background:#fff}}"
            "@media (prefers-color-scheme:dark){body{background:#1e1e21}}</style></head><body>"
            "<div id=\"evoui-mount\"></div>"
            f"<script type=\"application/json\" id=\"{BLOCK_ELEMENT_ID}\">"
            f"{_script_json(block_json(block))}</script>"
            f"<script>{js}</script>"
            f"<script>window.__evouiImages={json.dumps(dict(images or {}))};</script>"
            "<script>window.__evoui=EvoUI.render(JSON.parse(document.getElementById("
            f"'{BLOCK_ELEMENT_ID}').textContent),document.getElementById('evoui-mount'),{_HARNESS_IMAGES});</script>"
            "</body></html>")


def controls(body: Any) -> Iterator[dict[str, Any]]:
    """Every bound control (slider, input, choice, select, toggle, checkbox, player, and a route that
    binds a state) in the node tree (inside `if` branches too), in order. Buttons are not included:
    their values are expressions."""
    if not isinstance(body, dict):
        return
    if body.get("kind") in CONTROL_KINDS and "bind" in body:
        yield body
    for c in body.get("children") or ():
        yield from controls(c)
    for f in ("then", "else"):
        yield from controls(body.get(f))


def quizzes(body: Any) -> list[dict[str, Any]]:
    """The quiz nodes in document order (children, then `then`, then `else`) — the numbering
    evoui.js gives `onQuizResult` / `results()` (`quiz`)."""
    out: list[dict[str, Any]] = []

    def walk(n: Any) -> None:
        if not isinstance(n, dict):
            return
        if n.get("kind") == "quiz":
            out.append(n)
        for c in n.get("children") or ():
            walk(c)
        walk(n.get("then"))
        walk(n.get("else"))

    walk(body)
    return out


def node_keys(block: Any) -> dict[str, dict[str, Any]]:
    """Every stateful node (`STATEFUL_KINDS`) of the block by its node key, in document order
    (children, then `then`, then `else`; `if` branches included whether shown or not): the key is the
    node's `id`, else `<kind>#<n>` with n its index among the block's nodes of that kind. This is the
    numbering evoui.js files progress and result events under (`EvoUI.nodeKeys`). Assumes a valid
    block (ids unique); a repeated id keeps the first node."""
    out: dict[str, dict[str, Any]] = {}
    count: dict[str, int] = {}

    def walk(n: Any) -> None:
        if not isinstance(n, dict):
            return
        kind = n.get("kind")
        if kind in STATEFUL_KINDS:
            i = count[kind] = count.get(kind, -1) + 1
            nid = n.get("id")
            out.setdefault(nid if isinstance(nid, str) else f"{kind}#{i}", n)
        children = n.get("children")
        if isinstance(children, list):
            for c in children:
                walk(c)
        walk(n.get("then"))
        walk(n.get("else"))

    walk(block.get("body") if isinstance(block, dict) else None)
    return out


def quiz_cells(block: Any) -> set[str]:
    """Every K `cell` the block names: quiz questions', flashcards' cards', fill-blank / sentence
    builder nodes' and app items' (their answers become K evidence)."""
    if not isinstance(block, dict):
        return set()
    items = [q for z in quizzes(block.get("body")) for q in z.get("questions") or ()]
    items += [c for n in node_keys(block).values() if n.get("kind") == "flashcards"
              for c in (n.get("cards") if isinstance(n.get("cards"), list) else ())]
    items += [n for n in node_keys(block).values() if n.get("kind") in ("fill-blank", "sentence")]
    items += [it for n in node_keys(block).values() if n.get("kind") == "app"
              for it in (n.get("items") if isinstance(n.get("items"), list) else ())]
    return {q["cell"] for q in items if isinstance(q, dict) and isinstance(q.get("cell"), str)}


TIERS_RULE = """交互按四层选，从轻到重，先用前面的：
1. 预制组件（组件库里有对得上的就用它：可配置组件填一份 config；固定内容组件主题对得上才用）；
2. 简单的图（EvoUI 块里只用 chart 或 plot，加一两个滑块或选择，看一个量怎么随另一个变）；
3. 自己拼的交互（EvoUI 块：状态 → 控件 → 图 → 实时读数 → 一句随状态变的结论；也包括闪卡、分页测验、填空、拼句、
   翻页练习、计时、播放器逐步动画、选择路线这些学习节点）；
4. 沙盒小程序（EvoUI 块里的 `app` 节点，自己写 HTML 和脚本）：只用于前三层的节点做不到的效果——3D、连续的画布动画、
   物理模拟、像游戏一样的练习。它同样要在 question 里写清用处，块要有完整的 fallback 文字，读者的作答要通过
   evowiki.item / evowiki.summary 回报（对应 `items` 里声明的项）。能用节点表达的，就不要写 app。
不管哪一层，都要写明它的用处（块的 question 和 write_block 的 why 写这个）：
- 看懂：学习者不动手就看不见、想不清的变化或关系；
- 练熟：他要记住或会做的东西（一组符号、单词，一个要自己算出来的步骤），只读不练记不住、做不出——让他当场回想或
  动手做，马上知道对错和为什么（闪卡接测验、例题之后的同类练习）；
- 辨析：容易混的几种情况（诊断测验）。
三样都没有，就不做交互，用文字和例子讲清楚。"""


def spec_for_prompt(max_examples: int | None = None,
                    extra: Sequence[Mapping[str, Any]] = ()) -> str:
    """The EvoUI format as a model reads it: spec.md plus the templates (name, when to use it and
    the block as compact JSON); `max_examples` keeps only the first few (None: all). `extra`
    templates (the user's H8 examples, same fields) follow the built-in ones."""
    ts = templates() if max_examples is None else templates()[:max_examples]
    ex = "\n\n".join(f"### {t['name']}（{t['use']}）\n{block_json(t['block'])}"
                     for t in [*ts, *extra])
    return f"{spec_text()}\n\n## 模板（照着结构写，内容换成这一页的）\n\n{ex}"


# The format's prose lives in the page-writing skill (one source: skills/write-page/references/evoui,
# design 2026-10-28 A5): core (blocks, nodes, expressions, limits) and one file per group of
# nodes; each group's templates are the examples.json entries with that `group`.
GROUPS = ("chart", "quiz", "flashcard", "timeline", "reveal", "player", "practice", "app",
          "progress")


def group_templates(group: str, extra: Sequence[Mapping[str, Any]] = ()) -> str:
    """The templates of one group as a model reads them (built-in, then the pack's H8 `extra`,
    whose templates without a known group go with `core`); "" when it has none."""
    ts = [t for t in [*templates(), *extra]
          if (t.get("group") if t.get("group") in GROUPS else "core") == group]
    if not ts:
        return ""
    return "## 模板（结构照着写，内容、数字和题目换成这一页的）\n\n" + "\n\n".join(
        f"### {t['name']}（{t['use']}）\n{block_json(t['block'])}" for t in ts)
