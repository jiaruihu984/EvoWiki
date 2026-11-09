"""The visualization step behind draw_visual: a section of a tutorial -> a picture the reader gets the idea from.

Its own model (`roster["visual"]`, EVOWIKI_VISUAL_MODEL; the writer's without it). Block markup first (`deep.scene`: nothing
the model writes runs as code); a section that wants real photos, or whose markup keeps failing, gets a small program in its
own short conversation:

  1. the author reads the section and the writer's three answers (what is invisible, what to draw, what changes when
     touched); it may first ask for real pictures (`<search>`) and then see candidate thumbnails;
  2. the program is validated and render-checked; a failure goes back once;
  3. look once: the author sees real screenshots (opening, after each of its probes, narrow, dark) and writes one
     revision. A revision that fails the check is dropped: the version that passed stands.

The block is an `app` node. Fail closed: anything that leaves no passing version (too big, keeps failing, the model is
unavailable) raises `VisualFailed` and the writer explains in text. A spending limit or a cancel is not a failure of the
picture: those propagate and end the run."""

import contextvars
import hashlib
import json
import logging
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import cache
from threading import Event
from typing import Any, Protocol

from pydantic import TypeAdapter, ValidationError

from evowiki.agent.skills import load_skill
from evowiki.deep.components import ConfigInvalid, cell_problems, instantiate
from evowiki.deep.contracts import (
    AppProbe,
    BlockLook,
    ImageDraft,
    InteractiveDraft,
    LibraryComponent,
    RenderCheck,
    RenderChecker,
)
from evowiki.deep.contracts import (
    BlockChecker as _BlockChecker,
)
from evowiki.deep.evoui.contracts import MAX_APP_HTML, MAX_FALLBACK_CHARS, BlockDraft, EvoUIBlock
from evowiki.deep.evoui.validate import validate as validate_block
from evowiki.deep.images import PROVIDER_NAMES, ImageCandidate, ImageRejected, ImageSearch
from evowiki.deep.language import messages_in_language, output_rule
from evowiki.deep.library import catalog_text
from evowiki.goals.language import Language
from evowiki.llm.budget import BudgetExceededError
from evowiki.llm.chat import ChatMessage, ChatRequest, ImagePart, ImagesNotSupported
from evowiki.llm.deadline import DeadlineExceeded, RequestCancelled
from evowiki.llm.gateway import Gateway, ProviderOutputError, ProviderUnavailableError
from evowiki.llm.models import LLMCall
from evowiki.llm.usage import Account

log = logging.getLogger("evowiki.deep.visual")

PURPOSE = "deep.visual"
DECIDE_PURPOSE = "deep.visual.decide"
RECHECK_PURPOSE = "deep.visual.recheck"
PROMPT_VERSION = "visual@v4"
MAX_QUERIES = 3
CANDIDATES_PER_QUERY = 4
MAX_PROBES = 3
MAX_DRAWING = 4                          # pictures of one page drawn at once (each opens its own browser for its checks)

LOOK = ("上面是你写的小程序的真实渲染：依次是刚打开、做完 probes 每一步、窄屏、深色模式。\n{notes}\n"
        "只看图判断：把文字遮住，一个没学过的人能看出这个概念吗？动手前后的变化说明问题吗？哪里挤、哪里被裁、哪里看不清、"
        "哪里其实只是文字或读数？图上画的和教程一致吗？这个形式适合这个主题吗？字和线是不是太大、配色是不是太闹？\n"
        "<review>里写判断和要改的地方</review>，然后按原来的格式输出修改后的完整几段（不用再写 <search>，<images> 要保留）。")
LOOK_COMPONENT = ("上面是这个组件的真实渲染。{notes}\n只看图判断：它真的让人看懂这一段要讲的东西吗？够就只回 <keep/>；"
                  "不够就按原来的格式现写一个小程序，写出完整的几段。")
NO_CONFIG = "组件 {cid} 的 config 格式：\n{schema}\n{why}写出 <component>，或者不用它、现写小程序。"
BAD_COMPONENT = "组件用不了：{why}\n改好，或者不用它、现写小程序。"
REVISE = "这是这一段现在的小程序：\n<current>{html}</current>\n写页面的人要改的：{change}\n只改需要改的，已有的图片留着，按原来的格式输出完整的几段。"
RECHECK = ("这一段的正文改过了：\n{body}\n\n之前发现的不一致：{conflict}\n小程序：\n{html}\n\n现在还对不上吗？"
           "对上了只回 <ok/>；还对不上写 <conflict>哪里对不上</conflict>。")
REPAIR = "渲染检查没过：{why}\n改好后按原来的格式输出完整的几段。"
NO_VERDICT = "没有读到判断。只输出要求的这几段，第一段是 <decide>draw</decide> 或 <decide>skip</decide>。"
TRIED = "\n\n上一次画这一段没有成功：{why}\n这次避开这个问题，必要时换一种画法。"
TOO_BIG = "（小程序最多 {n} 字，这次太长了，砍掉不必要的部分。）"


class VisualFailed(Exception):
    """No version passed; the text is for the writer."""


@cache
def system_text() -> str:
    return load_skill("draw-visual").body


@cache
def decide_text() -> str:
    return load_skill("decide-visual").body


@dataclass(frozen=True)
class Decision:
    """The visual model's call on one section: whether it gets a picture, and the three answers."""

    draw: bool
    why: str = ""
    invisible: str = ""
    show: str = ""
    touch: str = ""
    photos: tuple[str, ...] = ()
    unanswered: bool = False                  # no readable answer: not drawn, and on record as such


class VisualChecker(RenderChecker, _BlockChecker, Protocol):
    def look_interactive(self, draft: InteractiveDraft) -> BlockLook: ...


@dataclass(frozen=True)
class Brief:
    """What the author is told. The three answers are the writer's."""

    block_id: str
    section_id: str
    heading: str
    body: str
    invisible: str
    draw: str
    touch: str
    photos: tuple[str, ...] = ()              # what to look for in the image libraries before drawing
    tried: str = ""                           # why the last try at this picture failed (the request must not repeat)


@dataclass(frozen=True)
class Drawn:
    block: BlockDraft | None
    images: tuple[ImageDraft, ...]
    check: RenderCheck
    design: str
    review: str
    notes: tuple[str, ...]
    component: str | None = None              # a library component instead of `block`
    interactive: InteractiveDraft | None = None
    conflict: str = ""                        # where the picture and the section's text disagree


_DRAWN = TypeAdapter(Drawn)


@dataclass
class _Reply:
    raw: str
    echo: Any
    design: str
    html: str
    probes: tuple[AppProbe, ...]
    fallback: str
    review: str
    search: tuple[str, ...]
    images: tuple[dict[str, str], ...]
    component: dict[str, Any] | None = None
    keep: bool = False
    conflict: str = ""


def _tag(text: str, tag: str) -> str:
    got = re.findall(rf"<{tag}>(.*?)</{tag}>", text, flags=re.DOTALL)
    return got[-1].strip() if got else ""


def tried_note(b: Brief) -> str:
    """The gateway answers a repeated request from its call log, so a try after a failure has to say what failed."""
    return TRIED.format(why=b.tried) if b.tried else ""


def _no_placeholders(body: str) -> str:
    """A section as read: a placeholder the writer left is its wish, and the judgment should not take it as a picture."""
    return re.sub(r"\{\{(?:block|interactive|image):[^}]*\}\}\n?", "", body)


def _program(text: str) -> str:
    """The program: from the first <html> to the last </html> (it may mention its own tags). The model often leaves
    the closing tag out: then it ends where the next section starts."""
    a = text.find("<html>")
    if a < 0:
        return ""
    b = text.rfind("</html>")
    if b < a:
        ends = [i for i in (text.find(t, a) for t in ("<probes>", "<fallback>")) if i >= 0]
        b = min(ends) if ends else -1
    return text[a + len("<html>"):b].strip() if b > a else ""


def _json_list(text: str) -> list[Any]:
    try:
        got = json.loads(text) if text else []
    except ValueError:
        return []
    return got if isinstance(got, list) else []


def _component(text: str) -> dict[str, Any] | None:
    try:
        got = json.loads(text) if text else None
    except ValueError:
        return None
    return got if isinstance(got, dict) and isinstance(got.get("id"), str) else None


def _parse(raw: str, echo: Any) -> _Reply:
    probes = []
    for p in _json_list(_tag(raw, "probes"))[:MAX_PROBES]:
        try:
            probes.append(AppProbe.model_validate({k: v for k, v in p.items() if k in AppProbe.model_fields}))
        except (ValidationError, AttributeError):
            continue
    return _Reply(raw=raw, echo=echo, design=_tag(raw, "design"), html=_program(raw), probes=tuple(probes),
                  fallback=_tag(raw, "fallback"), review=_tag(raw, "review"),
                  search=tuple(str(q)[:120] for q in _json_list(_tag(raw, "search")))[:MAX_QUERIES],
                  images=tuple(x for x in _json_list(_tag(raw, "images")) if isinstance(x, dict)),
                  component=_component(_tag(raw, "component")), keep="<keep" in raw, conflict=_tag(raw, "conflict"))


def _carried(new: _Reply, old: _Reply) -> _Reply:
    """A later version keeps what the one before declared: its pictures, and a disagreement it has not said is gone."""
    if not new.images:
        new.images = old.images
    if not new.conflict:
        new.conflict = old.conflict
    return new


def _image_id(block_id: str, name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", f"{block_id}-{name}".lower())[:64].strip("-")


def _node_entry(name: str, m: ImageDraft) -> dict[str, str]:
    """An app's image while it is a draft: `asset` names the ImageDraft (stored: `wikiapp.refine_apply`)."""
    entry = {"id": name, "title": m.title[:120], "license": m.license[:120], "source": m.source,
             "source_url": m.source_url[:600], "asset": m.image_id, "alt": m.alt[:120]}
    if m.author:
        entry["author"] = m.author[:120]
    if m.license_url:
        entry["license_url"] = m.license_url[:600]
    return entry


@dataclass
class VisualStep:
    """One page's visualization step: the gateway it runs on and what it needs from the page."""

    gw: Gateway
    account: Account | None
    checker: "VisualChecker"
    images: ImageSearch | None
    page_id: str
    title: str
    goal: str
    known: str
    language: Language = "zh"
    calls: list[LLMCall] = field(default_factory=list)
    cancel: Event | None = None
    components: Mapping[str, LibraryComponent] = field(default_factory=dict)
    page_cells: Collection[str] = ()

    def _ask(self, messages: list[ChatMessage]) -> _Reply:
        if self.cancel is not None and self.cancel.is_set():
            raise RequestCancelled("the run was cancelled")
        turn, call = self.gw.converse("generator", ChatRequest(
            purpose=PURPOSE, prompt_version=PROMPT_VERSION, subject=self.page_id, system=system_text() + "\n" + output_rule(self.language),
            messages=messages_in_language(messages, self.language)), account=self.account)
        self.calls.append(call)
        return _parse(turn.text, turn.echo)

    def _plain(self, purpose: str, system: str, messages: tuple[ChatMessage, ...]) -> Any:
        if self.cancel is not None and self.cancel.is_set():
            raise RequestCancelled("the run was cancelled")
        turn, call = self.gw.converse("generator", ChatRequest(
            purpose=purpose, prompt_version=PROMPT_VERSION, subject=self.page_id, system=system + "\n" + output_rule(self.language),
            messages=messages_in_language(messages, self.language)), account=self.account)
        self.calls.append(call)
        return turn

    def decide(self, heading: str, body: str, hints: tuple[str, ...], decided: tuple[str, ...],
               outline: tuple[str, ...] = ()) -> Decision:
        """One cheap call per section. A reply without a verdict goes back once with what was wrong (the second request
        differs, so it is really asked again); no verdict after that, or no answer at all: not drawn, marked `unanswered`."""
        text = (f"页面：{self.title}\n目标：{self.goal}\n学习者：{self.known}\n"
                + (f"本页各段：{' / '.join(outline)}\n" if outline else "")
                + f"\n这一段：{heading}\n{_no_placeholders(body)}"
                + ("\n\n本页已经画了：\n" + "\n".join(decided) if decided else "")
                + ("\n\n写页面的人的建议：\n" + "\n".join(hints) if hints else ""))
        msgs = (ChatMessage(role="user", text=text),)
        try:
            turn = self._plain(DECIDE_PURPOSE, decide_text(), msgs)
            if _tag(turn.text, "decide").lower() not in ("draw", "skip"):
                log.warning("visual %s: an unreadable decision: %r", self.page_id, turn.text[:120])
                turn = self._plain(DECIDE_PURPOSE, decide_text(), (
                    *msgs, ChatMessage(role="assistant", text=turn.text or "（空）", echo=turn.echo),
                    ChatMessage(role="user", text=NO_VERDICT)))
        except (ProviderOutputError, ProviderUnavailableError, DeadlineExceeded) as exc:
            log.warning("visual %s: no decision on %s: %s", self.page_id, heading, type(exc).__name__)
            return Decision(False, why=f"模型没有答复（{type(exc).__name__}）", unanswered=True)
        raw = turn.text
        verdict = _tag(raw, "decide").lower()
        if verdict not in ("draw", "skip"):
            log.warning("visual %s: no decision on %s: %r", self.page_id, heading, raw[:120])
            return Decision(False, why="模型的回答读不出判断", unanswered=True)
        return Decision(draw=verdict == "draw", why=_tag(raw, "why")[:200], invisible=_tag(raw, "invisible")[:400],
                        show=_tag(raw, "show")[:400], touch=_tag(raw, "touch")[:400],
                        photos=tuple(str(q)[:120] for q in _json_list(_tag(raw, "photos")) if str(q).strip())[:MAX_QUERIES])

    def conflict_left(self, body: str, conflict: str, html: str) -> str:
        """After the text was corrected: what still disagrees ("" = nothing). No answer: it stays open."""
        try:
            raw = self._plain(RECHECK_PURPOSE, system_text(), (ChatMessage(
                role="user", text=RECHECK.format(body=body, conflict=conflict, html=html)),)).text
        except (ProviderOutputError, ProviderUnavailableError, DeadlineExceeded):
            return conflict
        return _tag(raw, "conflict") or ("" if "<ok" in raw else conflict)

    def _scene(self) -> Any:
        from evowiki.deep.scene import SceneStep  # it imports this module
        return SceneStep(gw=self.gw, account=self.account, checker=self.checker, page_id=self.page_id, title=self.title,
                         goal=self.goal, known=self.known, language=self.language, calls=self.calls, cancel=self.cancel)

    def revise(self, b: Brief, current: BlockDraft, change: str, used: dict[str, tuple[ImageDraft, dict[str, str]]]) -> Drawn:
        """A change to a picture that exists: the author starts from it, its pictures stay. VisualFailed: nothing changes."""
        if current.block.body.get("kind") != "app":
            return self._scene().revise(b, current, change)
        try:
            msgs = [ChatMessage(role="user", text=self._prompt(b) + "\n\n"
                                + REVISE.format(html=current.block.body.get("html", ""), change=change))]
            r = self._ask(msgs)
            if not r.images:
                r.images = tuple({"id": name} for name in used)
            draft, check, why = self._version(b, r, used)
            if draft is None or check is None:
                r2 = self._ask([*msgs, ChatMessage(role="assistant", text=r.raw, echo=r.echo),
                                ChatMessage(role="user", text=REPAIR.format(why=why))])
                r = _carried(r2, r)
                draft, check, why = self._version(b, r, used)
            if draft is None or check is None:
                raise VisualFailed(why[:400])
            return self._look_once(b, msgs, r, draft, check, {}, used, ())
        except (BudgetExceededError, RequestCancelled):
            raise
        except (ProviderOutputError, ProviderUnavailableError, DeadlineExceeded) as exc:
            raise VisualFailed(f"模型没有答复（{type(exc).__name__}）") from exc

    def _prompt(self, b: Brief) -> str:
        return (f"页面：{self.title}\n目标：{self.goal}\n学习者：{self.known}\n\n"
                f"这一段：{b.heading}\n{b.body}\n\n"
                f"写页面的人的回答：\n看不见的：{b.invisible}\n画什么：{b.draw}\n动手：{b.touch}"
                + (f"\n\n预制组件：\n{catalog_text(list(self.components.values()))}" if self.components else "")
                + tried_note(b))

    def _placed(self, b: Brief, r: _Reply) -> tuple[InteractiveDraft | None, RenderCheck | None, str]:
        """The reply's component, instantiated and render-checked: (draft, check, why not)."""
        spec = r.component or {}
        c = self.components.get(str(spec.get("id")))
        if c is None:
            return None, None, f"组件库里没有 {spec.get('id')}"
        config = spec.get("config")
        try:
            html, probes, title, question = instantiate(c, config)
        except ConfigInvalid as e:
            return None, None, f"config 不合法（{e}）"
        if problems := cell_problems(config, self.page_cells):
            return None, None, "；".join(problems)
        try:
            d = InteractiveDraft(interactive_id=b.block_id, section_id=b.section_id, title=title,
                                 learning_question=question, html=html, height=c.height, probes=probes)
        except ValidationError as e:
            return None, None, str(e.errors(include_url=False)[0]["msg"])
        check = self.checker.check(d)
        if not check.ok:
            from evowiki.deep.tools import check_text
            return None, None, check_text(check)[:1000]
        return d, check, ""

    def _with_component(self, b: Brief, msgs: list[ChatMessage], r: _Reply) -> tuple[Drawn | None, _Reply]:
        """The author picked a component: it gets its config's format once, is checked and looked at; the author keeps
        it or writes a program after all. (None, the reply to go on from) when it writes one."""
        c = self.components.get(str((r.component or {}).get("id")))
        if c is not None and c.config_schema is not None and (r.component or {}).get("config") is None:
            msgs += [ChatMessage(role="assistant", text=r.raw, echo=r.echo),
                     ChatMessage(role="user", text=NO_CONFIG.format(
                         cid=c.component_id, schema=json.dumps(c.config_schema, ensure_ascii=False), why=""))]
            r = self._ask(msgs)
            if r.component is None:
                return None, r
        d, check, why = self._placed(b, r)
        if d is None or check is None:
            msgs += [ChatMessage(role="assistant", text=r.raw, echo=r.echo),
                     ChatMessage(role="user", text=BAD_COMPONENT.format(why=why))]
            r = self._ask(msgs)
            if r.component is None:
                return None, r
            d, check, why = self._placed(b, r)
            if d is None or check is None:
                raise VisualFailed(why[:400])
        seen = self.checker.look_interactive(d)
        if seen.shots:
            parts = tuple(ImagePart(media_type="image/jpeg", sha256=hashlib.sha256(data.encode()).hexdigest(),
                                    data_b64=data, name=name) for name, data in seen.shots)
            msgs += [ChatMessage(role="assistant", text=r.raw, echo=r.echo),
                     ChatMessage(role="user", parts=parts, text=LOOK_COMPONENT.format(notes="\n".join(seen.notes)))]
            try:
                again = self._ask(msgs)
            except ImagesNotSupported:
                again = None
            if again is not None and again.html and not again.keep:
                return None, again
        return Drawn(block=None, images=(), check=check, design=r.design, review="", notes=seen.notes,
                     component=c.component_id if c is not None else None, interactive=d), r

    def _candidates(self, queries: tuple[str, ...]) -> tuple[list[ImagePart], dict[str, ImageCandidate], str]:
        """Thumbnails to look at, the candidates they show by label, and the words that go with them."""
        parts: list[ImagePart] = []
        found: dict[str, ImageCandidate] = {}
        seen: set[str] = set()
        lines = []
        for q in queries:
            for c in self.images.search(q, CANDIDATES_PER_QUERY) if self.images is not None else ():
                if c.source_url in seen:
                    continue
                seen.add(c.source_url)
                thumb = self.images.thumbnail(c) if self.images is not None else None
                if thumb is None:
                    continue
                label = f"c{len(found) + 1}"
                found[label] = c
                parts.append(ImagePart(media_type="image/webp", sha256=hashlib.sha256(thumb.data).hexdigest(),
                                       data_b64=thumb.data_b64, name=label))
                lines.append(f"[{label}] {c.title} | {c.license} | {c.width}×{c.height}")
        return parts, found, "\n".join(lines)

    def _picked(self, b: Brief, r: _Reply, found: Mapping[str, ImageCandidate],
                got: dict[str, tuple[ImageDraft, dict[str, str]]]) -> str:
        """Downloads what the reply declares (once each); what could not be used is told back."""
        problems = []
        for x in r.images:
            use, name = str(x.get("use", "")), str(x.get("id", ""))
            if name in got:
                continue
            c = found.get(use)
            if c is None or self.images is None or not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
                problems.append(f"图片 {use} → {name} 不存在或 id 不合法")
                continue
            try:
                img = self.images.fetch(c)
            except ImageRejected as e:
                problems.append(f"图片 {use} 不能用：{e}")
                continue
            d = ImageDraft(image_id=_image_id(b.block_id, name), section_id=b.section_id,
                           alt=str(x.get("alt") or c.title)[:200], title=c.title, author=c.author, license=c.license,
                           license_url=c.license_url, source=PROVIDER_NAMES[c.provider], source_url=c.source_url,
                           width=img.width, height=img.height, data_b64=img.data_b64)
            got[name] = (d, _node_entry(name, d))
        return "；".join(problems)

    def _version(self, b: Brief, r: _Reply, used: Mapping[str, tuple[ImageDraft, dict[str, str]]]
                 ) -> tuple[BlockDraft | None, RenderCheck | None, str]:
        """The reply as a checked block: (block, check, why not)."""
        if not r.html:
            return None, None, "没有 <html>"
        node: dict[str, Any] = {"kind": "app", "id": "viz", "html": r.html}
        names = {x.get("id") for x in r.images}
        pics = [e for name, (_, e) in used.items() if name in names]
        if pics:
            node["images"] = pics
        raw = {"type": "evoui", "title": b.heading[:120] or self.title[:120], "question": (b.invisible or b.heading or self.title)[:300],
               "state": {}, "derived": {}, "body": node,
               "fallback": (r.fallback or b.body)[:MAX_FALLBACK_CHARS]}
        problems = validate_block(raw)
        if problems:
            big = TOO_BIG.format(n=MAX_APP_HTML) if len(r.html) > MAX_APP_HTML else ""
            return None, None, "；".join(problems[:6]) + big
        draft = BlockDraft(block_id=b.block_id, section_id=b.section_id, why=b.invisible[:300] or b.heading,
                           block=EvoUIBlock.model_validate(raw))
        pictures = {name: d.data_b64 for name, (d, _) in used.items() if name in names}
        check = self.checker.check_block(b.block_id, draft.block, pictures or None, r.probes)
        if not check.ok:
            from evowiki.deep.tools import check_text
            return None, None, check_text(check)[:1500]
        return draft, check, ""

    def draw(self, b: Brief) -> Drawn:
        if not b.photos:      # real photos: only a program can lay marks over them today
            try:
                return self._scene().draw(b)
            except VisualFailed as exc:
                log.warning("scene %s/%s: %s; drawing a program", self.page_id, b.block_id, exc)
        try:
            return self._draw(b)
        except (BudgetExceededError, RequestCancelled):
            raise
        except (ProviderOutputError, ProviderUnavailableError, DeadlineExceeded) as exc:
            log.warning("visual %s/%s: %s: %s", self.page_id, b.block_id, type(exc).__name__, exc)
            raise VisualFailed(f"模型没有答复（{type(exc).__name__}）") from exc

    def draw_many(self, briefs: Sequence[Brief], shown: Callable[[Brief, Drawn], None]) -> dict[str, Drawn | str]:
        """The page's pictures side by side, once its text stands: section id -> what was drawn, or why not. Fan-out inside ONE
        durable step: its threads run plain calls (model replies are logged, so a re-execution after a crash pays only for what
        was in flight) and the writer's next step sees a settled result, in any order they finished. `shown(brief, drawn)` runs
        on this thread as each one lands. A spending limit or a cancel ends the run, after what is running has returned."""
        def run() -> dict[str, Any]:
            out: dict[str, Any] = {}
            stop: BaseException | None = None
            with ThreadPoolExecutor(max_workers=min(MAX_DRAWING, len(briefs)), thread_name_prefix="draw") as pool:
                # copy_context: the cancel scope and the step's charge list go along
                running = {pool.submit(contextvars.copy_context().run, self.draw, b): b for b in briefs}
                for f in as_completed(running):
                    b = running[f]
                    try:
                        d = f.result()
                    except VisualFailed as exc:
                        out[b.section_id] = str(exc)
                    except Exception as exc:  # noqa: BLE001 - raised below, once the others have returned
                        stop = stop or exc
                    else:
                        out[b.section_id] = _DRAWN.dump_python(d, mode="json")
                        shown(b, d)
            if stop is not None:
                raise stop
            return {"drawn": out}
        got = self.gw.durable_step("deep.draws", self.account, run)["drawn"]
        return {sid: v if isinstance(v, str) else _DRAWN.validate_python(v) for sid, v in got.items()}

    def _draw(self, b: Brief) -> Drawn:
        found: dict[str, ImageCandidate] = {}
        used: dict[str, tuple[ImageDraft, dict[str, str]]] = {}
        first = self._prompt(b)
        shown: tuple[ImagePart, ...] = ()
        if b.photos:
            parts, found, words = self._candidates(b.photos[:MAX_QUERIES])
            if parts:
                shown = tuple(parts)
                first += f"\n\n图库里找到的候选（图在前面，按这个顺序）：\n{words}\n能用就用（<images>），看不清就画。"
        msgs = [ChatMessage(role="user", text=first, parts=shown)]
        r = self._ask(msgs)
        if r.search and not r.html:
            parts, more, words = self._candidates(r.search)
            found = {**found, **more}
            msgs += [ChatMessage(role="assistant", text=r.raw, echo=r.echo),
                     ChatMessage(role="user", parts=tuple(parts),
                                 text=(f"候选图片，方括号里是编号（按上面的顺序）：\n{words}\n选能用的，写出完整的几段。"
                                       if parts else "这些词没有找到能转载的图片。不用真实图片，画出来，写出完整的几段。"))]
            r = self._ask(msgs)
        if r.component is not None and not r.html:
            drawn, r = self._with_component(b, msgs, r)
            if drawn is not None:
                return drawn
        problem = self._picked(b, r, found, used)
        draft, check, why = self._version(b, r, used)
        if draft is None or problem:
            msgs += [ChatMessage(role="assistant", text=r.raw, echo=r.echo),
                     ChatMessage(role="user", text=REPAIR.format(why="；".join(x for x in (why, problem) if x)))]
            r = _carried(self._ask(msgs), r)
            problem = self._picked(b, r, found, used)
            draft, check, why = self._version(b, r, used)
        if draft is None or check is None:
            raise VisualFailed(why[:400])
        notes = (problem,) if problem else ()
        return self._look_once(b, msgs, r, draft, check, found, used, notes)

    def _look_once(self, b: Brief, msgs: list[ChatMessage], r: _Reply, draft: BlockDraft, check: RenderCheck,
                   found: Mapping[str, ImageCandidate], used: dict[str, tuple[ImageDraft, dict[str, str]]],
                   notes: tuple[str, ...]) -> Drawn:
        names = {x.get("id") for x in r.images}
        pictures = {name: d.data_b64 for name, (d, _) in used.items() if name in names}
        seen = self.checker.look_block(b.block_id, draft.block, r.probes, pictures or None)
        review = ""
        kept, best = draft, r
        if seen.shots:
            parts = tuple(ImagePart(media_type="image/jpeg", sha256=hashlib.sha256(data.encode()).hexdigest(),
                                    data_b64=data, name=name) for name, data in seen.shots)
            looked = [*msgs, ChatMessage(role="assistant", text=r.raw, echo=r.echo),
                      ChatMessage(role="user", parts=parts, text=LOOK.format(notes="\n".join(seen.notes)))]
            try:
                r2 = self._ask(looked)
            except ImagesNotSupported:
                r2 = None                     # a model without eyes: what passed stands
            except (ProviderOutputError, ProviderUnavailableError, DeadlineExceeded) as exc:
                log.warning("visual %s/%s: no revision: %s", self.page_id, b.block_id, type(exc).__name__)
                r2 = None                     # a version already passed: it stands
            if r2 is not None and r2.keep and not r2.html:
                r2 = None                     # the model looked and is content: nothing to repair
            if r2 is not None:
                r2 = _carried(r2, r)
                review = r2.review
                problem = self._picked(b, r2, found, used)
                d2, c2, why = self._version(b, r2, used)
                if d2 is None or problem:              # the revision goes back once, like the first version
                    # the whole conversation: a reply's thinking is bound to what came before it
                    try:
                        r3 = self._ask([*looked, ChatMessage(role="assistant", text=r2.raw, echo=r2.echo),
                                        ChatMessage(role="user", text=REPAIR.format(why="；".join(x for x in (why, problem) if x)))])
                    except (ProviderOutputError, ProviderUnavailableError, DeadlineExceeded) as exc:
                        log.warning("visual %s/%s: no repair: %s", self.page_id, b.block_id, type(exc).__name__)
                    else:
                        problem = self._picked(b, r3, found, used)
                        d2, c2, why = self._version(b, r3, used)
                        r2 = _carried(r3, r2)
                if d2 is not None and c2 is not None and not problem:
                    kept, check, best = d2, c2, r2
                else:                                  # what passed stands; the writer has nothing to do about it
                    log.warning("visual %s/%s: the revision is dropped: %s", self.page_id, b.block_id, why or problem)
        names = {x.get("id") for x in best.images}
        return Drawn(block=kept, images=tuple(d for name, (d, _) in used.items() if name in names), check=check,
                     design=best.design, review=review, notes=(*notes, *seen.notes), conflict=best.conflict)
