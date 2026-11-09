"""Fakes for deep refine tests: a scripted two-provider Gateway (anthropic plans and writes, deepseek
is the second provider) answering each purpose from its own queue, and a render checker that never
opens a browser. The writer plans in its own loop: `DeepScript.plan()` queues a deep.write turn
that calls plan_page (deep@v28)."""

import json
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from evowiki.deep.contracts import (
    BUILTIN_REVIEW_RULES,
    BlockLook,
    InteractiveDraft,
    LibraryComponent,
    Probe,
    RefinePlan,
    RenderCheck,
)
from evowiki.deep.library import MemoryLibrary
from evowiki.deep.llm import DeepLLM
from evowiki.deep.tools import Workspace
from evowiki.llm.chat import ChatRequest, ChatTurn
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.prices import load_prices
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage import NullMeter
from evowiki.teach.default_spec import DEFAULT_ITEMS
from tests.runtime.llm_helpers import MemoryCallLog
from tests.wikiapp.test_goal import NOW

DEEP_CONFIG = GatewayConfig(generator=ModelRef(provider="anthropic", model="claude-sonnet-5-5"),
                            verifier=ModelRef(provider="deepseek", model="deepseek-v4-pro"))
# the virtual v1 spec's items in force on a concept page (R1 `teachspec.applicable`)
RULES = (*BUILTIN_REVIEW_RULES,
         *(i.item_id for i in DEFAULT_ITEMS if i.scope in ("any", "concept", "exercise")))

PLAN = {"observable": "放错一格就配不回原链", "one_at_a_time": ["印章和凹模", "四种积木", "配对规则"],
        "contrast": "配对正确 vs 错一格", "spoken_answer": "先想一个印章……",
        "practice": "不用当场练：这一页讲配对的原理，没有要记的符号"}

HTML = ("<!doctype html><meta charset=utf-8><button id=b>加一</button><p id=out>0</p>"
        "<script>let n=0;b.onclick=()=>{out.textContent=++n}</script>")


# a raw EvoUI block (what write_block takes): memory cards, then a paged quiz; no K cells
PRACTICE_BLOCK = {
    "type": "evoui", "title": "あ行和か行", "question": "练熟：看到假名能马上读出来吗？",
    "state": {}, "derived": {},
    "body": {"kind": "box", "children": [
        {"kind": "caption", "text": "先在心里读出来，再翻面核对。"},
        {"kind": "flashcards", "id": "kana", "cards": [
            {"front": "あ", "back": "a"}, {"front": "か", "back": "ka"}, {"front": "き", "back": "ki"}]},
        {"kind": "reveal", "mode": "after", "after": "kana", "children": [
            {"kind": "quiz", "id": "q", "layout": "paged", "questions": [
                {"q": "き 怎么读？", "options": ["ka", "ki"], "correct": 1, "why": "き = ki"}]}]}]},
    "fallback": "あ a、か ka、き ki：先看假名读出来，再核对。"}


def planned(inp: Any, ws: Workspace | None = None, plan: dict = PLAN) -> Workspace:
    """`ws` (default: a fresh one on `inp.page`) with a plan accepted, as plan_page leaves it."""
    ws = ws if ws is not None else Workspace(inp.page)
    ws.plan = RefinePlan(known=inp.known, **{k: tuple(v) if isinstance(v, list) else v
                                             for k, v in plan.items()})
    return ws


def call(name: str, i: int = 1, **args: Any) -> dict:
    return {"id": f"{name}-{i}", "name": name, "args": args}


def plan_page(plan: dict = PLAN, outline: dict | None = None, i: int = 1) -> dict:
    """The writer's plan_page call (the plan; with `outline` for a page without one)."""
    return call("plan_page", i, plan=plan, **({"outline": outline} if outline is not None else {}))


def turn(*calls: dict, text: str = "") -> dict:
    return {"text": text, "tool_calls": list(calls), "input_tokens": 1000, "output_tokens": 500}


def section(sid: str = "s2", i: int = 1, **kw: Any) -> dict:
    args = {"section_id": sid, "heading": "配对规则怎么来", "role": "explain", "depth": "example",
            "cue": "A 对 T，G 对 C", "answer": "一句答案", "body": f"{sid} 精修正文：先从印章讲起。"}
    return call("write_section", i, **{**args, **kw})


def component(cid: str = "pair-demo", html: str = HTML) -> LibraryComponent:
    """A registered library component (its own render check passed)."""
    return LibraryComponent(
        component_id=cid, name="拼一条链", description="点按钮看计数加一", html=html, height=480,
        learning_question="对面放什么？", params={"加一": "点一下计数加一"},
        probes=(Probe(target="#b", action="click", expect_change="#out"),),
        check=RenderCheck(interactive_id=cid, ok=True, probes_passed=1, probes_total=1))


def library(*cids: str) -> MemoryLibrary:
    return MemoryLibrary([component(c) for c in (cids or ("pair-demo",))])


def library_empty() -> MemoryLibrary:
    return MemoryLibrary()


def interactive(iid: str = "pair-demo", sid: str = "s2", i: int = 1, **kw) -> dict:
    """The writer placing library component `iid` on section `sid` (`config=`, `placement_id=`)."""
    return call("place_component", i, component_id=iid, section_id=sid,
                why="让他亲手点一下，看见计数怎么变", **kw)


def self_check(ok: bool = True, rules: tuple[str, ...] = RULES) -> list[dict]:
    return [{"rule": r, "ok": ok or r != "coherence", "section_id": "s2",
             "note": "" if ok else "「碱基」先用后讲，已改"} for r in rules]


def finish(i: int = 1, checks: list[dict] | None = None) -> dict:
    return call("finish", i, self_check=self_check() if checks is None else checks)


def simple_round(i: int = 1, *, with_interactive: bool = False) -> list[dict]:
    """One write round: read the spec, write s2 (+ an interactive), finish."""
    body = "s2 精修正文。\n\n{{interactive:pair-demo}}\n" if with_interactive else None
    first = [call("read_spec", i, part="items"),
             section("s2", i, **({"body": body} if body else {}))]
    if with_interactive:
        first.append(interactive(i=i))
    return [turn(*first), turn(finish(i))]


class DeepScript:
    """Queued outputs per purpose; an unscripted call fails the test. Every write turn reports
    1000 input / 500 output tokens (priced: Sonnet 5.5 → $0.007 per turn)."""

    def __init__(self) -> None:
        self.queues: dict[str, deque] = defaultdict(deque)
        self.served: list[tuple[str, str]] = []           # (provider, purpose)
        self.requests: list[ChatRequest | LLMRequest] = []
        self.log = MemoryCallLog()
        self.before_write: Callable[[], None] | None = None

    def push(self, purpose: str, *outputs: Any) -> "DeepScript":
        self.queues[purpose].extend(outputs)
        return self

    def plan(self, plan: dict = PLAN, outline: dict | None = None) -> "DeepScript":
        """The writer's first turn: plan_page alone."""
        return self.push("deep.write", turn(plan_page(plan, outline)))

    def write(self, *turns: dict) -> "DeepScript":
        return self.push("deep.write", *turns)

    def _next(self, provider: str, purpose: str, subject: str) -> Any:
        q = self.queues.get(purpose)
        if not q:
            raise AssertionError(f"unscripted {purpose}/{subject} via {provider}")
        self.served.append((provider, purpose))
        out = q.popleft()
        if isinstance(out, Exception):
            raise out
        return out

    def provider(self, name: str) -> ScriptedChatProvider:
        def chat(model: str, r: ChatRequest) -> ChatTurn:
            if self.before_write is not None:
                self.before_write()
            self.requests.append(r)
            return ChatTurn.model_validate(self._next(name, r.purpose, r.subject))

        def complete(model: str, r: LLMRequest) -> str:
            self.requests.append(r)
            out = self._next(name, r.purpose, r.subject)
            return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        return ScriptedChatProvider(name, chat, complete)

    def providers(self) -> dict[str, ScriptedChatProvider]:
        return {"anthropic": self.provider("anthropic"), "deepseek": self.provider("deepseek")}

    def llm(self, meter: Any = None) -> DeepLLM:
        return DeepLLM(config=DEEP_CONFIG, providers=self.providers(), log=self.log,
                       prices=load_prices(), meter=meter or NullMeter(), clock=lambda: NOW)

    def pending(self) -> dict[str, int]:
        return {k: len(q) for k, q in self.queues.items() if q}

    def count(self, purpose: str) -> int:
        return sum(p == purpose for _, p in self.served)


class FakeChecker:
    """Passes everything unless `fail` says otherwise for the html; counts checks."""

    def __init__(self, fail: Callable[[InteractiveDraft], str | None] = lambda d: None,
                 fail_block: Callable[[str, Any], str | None] = lambda bid, b: None):
        self.fail, self.fail_block = fail, fail_block
        self.checked: list[str] = []

    def look_interactive(self, draft: Any) -> BlockLook:
        return BlockLook(shots=(("刚打开", "aGk="),), notes=())

    def look_block(self, block_id: str, block: Any, probes: Any, images: Any = None) -> BlockLook:
        return BlockLook(shots=(("刚打开", "aGk="),), notes=())

    def check_block(self, block_id: str, block: Any, images: Any = None, touch: Any = ()) -> RenderCheck:
        self.checked.append(block_id)
        why = self.fail_block(block_id, block)
        return RenderCheck(interactive_id=block_id, ok=why is None, probes_passed=0 if why else 1,
                           probes_total=1, page_errors=(why,) if why else (), detail=why or "")

    def check(self, d: InteractiveDraft) -> RenderCheck:
        self.checked.append(d.interactive_id)
        why = self.fail(d)
        return RenderCheck(interactive_id=d.interactive_id, ok=why is None,
                           probes_passed=0 if why else len(d.probes), probes_total=len(d.probes),
                           page_errors=(why,) if why else (), detail=why or "")
