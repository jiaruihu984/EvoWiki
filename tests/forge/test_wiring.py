"""Forged tools wired into the page writer, the assistant's tool factory and the service (plan P2
T11). Fakes only: scripted deep gateway, in-process sandbox, HashingEmbedder."""

import json

import pytest

from evowiki.agent.tools import ToolResult, ToolSpec
from evowiki.content.models import Lang
from evowiki.forge import switch
from evowiki.forge.run import UNAVAILABLE
from evowiki.forge.sandbox import SandboxUnavailable
from evowiki.harness.pack import HarnessPack
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.service.deps import ForgeToolFactory, build_forge, forge_factory
from evowiki.sources.config import DATA_BEGIN, DATA_END
from evowiki.store import forge as store
from evowiki.store.users import UserRow, create_user
from tests.deep.fakes import DeepScript, call, finish, section, self_check, turn
from tests.deep.test_writer import outline_input, writer
from tests.forge.test_run import (
    CTX,
    DOUBLE,
    READER,
    T0,
    ReadPage,
    Recording,
    live,
    request_on,
    runtime,
)
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import Ids
from tests.wikiapp.test_page import organized


@pytest.fixture
def users(pool):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2",
                                  lang=Lang.EN, created_at=T0))
    return "u1"


@pytest.fixture
def env():
    from evowiki.wikiapp.repo import MemoryRepo
    return MemoryRepo(["u1"]), Script(), Ids()


def two_rounds(*first: dict) -> list[dict]:
    """Round 0: `first`, the page's three sections, a finish whose self-check names a problem
    (so a second, self-check round runs); round 1: finish."""
    return [*(turn(c) for c in first),
            turn(section("s1", role="organizer", depth="quick", heading="从你会的出发")),
            turn(section("s2")),
            turn(section("s3", role="recap", depth="quick", heading="标准差提醒")),
            turn(finish(1, checks=self_check(ok=False))),
            turn(finish(2))]


def write_requests(ds: DeepScript):
    return [r for r in ds.requests if r.purpose == "deep.write"]


def run_page(env, pool, *first, forge=None) -> DeepScript:
    pid = organized(env)
    ds = DeepScript().plan().write(*two_rounds(*first))
    w = writer(ds)
    w.forge = forge
    w.write(outline_input(env, pid))
    assert not ds.pending()
    return ds


def tool_names(req) -> list[str]:
    return [t.name for t in req.tools]


# --- the page writer -----------------------------------------------------------------------------

def test_page_writer_runs_a_first_class_forged_tool_and_keeps_the_list_fixed(env, pool, users):
    live(pool, "fc_a", "double", DOUBLE, title="翻倍")
    sandbox = Recording()
    rt = runtime(pool, sandbox=sandbox)
    pins = []
    orig = rt.for_run
    object.__setattr__(rt, "for_run", lambda **kw: pins.append(kw) or orig(**kw))
    ds = run_page(env, pool, call("double", n=4), forge=rt)
    reqs = write_requests(ds)
    assert len({r.params["round"] for r in reqs}) == 2               # two write rounds
    assert sandbox.calls == [{"n": 4}]                              # ran in the (fake) sandbox
    # stage 4: reqs[0] is plan_page's turn, so the forged tool's result is the second tool message
    # of reqs[2] (the first is plan_page's)
    results = [m.text for m in reqs[2].messages if m.role == "tool"]
    assert json.loads(unfence(results[1])) == {"n": 8}
    assert "double" in tool_names(reqs[0]) and "call_tool" in tool_names(reqs[0])
    # prompt cache: the tool definitions are byte-identical in every call of every round
    assert len({json.dumps([t.model_dump() for t in r.tools], ensure_ascii=False)
                for r in reqs}) == 1
    assert len(pins) == 1 and pins[0]["agent"] == "deep" and pins[0]["group"] == "page"
    assert pins[0]["run_id"].startswith("refine:")                  # the refine's run id


def same_prompts(a: DeepScript, b: DeepScript) -> None:
    ra, rb = write_requests(a), write_requests(b)
    assert len(ra) == len(rb)
    for x, y in zip(ra, rb, strict=True):
        assert x.tools == y.tools and x.system == y.system
        assert [m.model_dump() for m in x.messages] == [m.model_dump() for m in y.messages]


def test_without_forge_the_tool_list_is_the_builtins(env, pool, users):
    ds = run_page(env, pool)
    assert {tuple(tool_names(r)) for r in write_requests(ds)} == {(
        "plan_page", "read_k", "read_page", "read_spec", "write_section", "place_component", "read_ref",
        "draw_visual", "write_block", "render_check", "finish")}


@pytest.mark.parametrize("how", ["env", "setting"])
def test_kill_switch_makes_the_wiring_a_no_op(env, pool, users, monkeypatch, how):
    """Opted-in tool and request_tool on, forge switched off: the writer's tool lists and prompts
    are byte-identical to a run without any forge wiring."""
    live(pool, "fc_a", "double", DOUBLE, title="翻倍")
    request_on(pool)
    if how == "env":
        monkeypatch.setenv(switch.ENV, "off")
    else:
        with pool.connection() as conn:
            store.set_setting(conn, switch.GLOBAL, switch.key("tool"), False, T0)
    off = run_page(env, pool, forge=runtime(pool))
    from evowiki.wikiapp.repo import MemoryRepo
    plain = run_page((MemoryRepo(["u1"]), Script(), Ids()), pool, forge=None)
    same_prompts(off, plain)


# --- the assistant's tool factory -------------------------------------------------------------

class Info:
    def __init__(self, sid="s1"):
        self.user_id, self.session_id, self.page_id, self.note_concept_id = "u1", sid, "p1", None


class Base:
    """A tools factory (as WikiTools): fresh tool objects per turn, `durable()` copy."""

    def __init__(self):
        self.durable_called = False

    def __call__(self, info, started):
        return (ReadPage(), Echo())

    def durable(self):
        self.durable_called = True
        return self


class Echo:
    spec = ToolSpec(name="echo", description="回声", effect="read",
                    parameters={"type": "object", "properties": {}})

    def run(self, args, ctx):
        return ToolResult(content="echo")


def specs(tools):
    return [t.spec for t in tools]


def test_assistant_factory_without_runtime_is_the_base():
    base = Base()
    assert forge_factory(base, None) is base


def test_assistant_factory_forge_off_returns_exactly_the_base_tools(pool, users, monkeypatch):
    live(pool, "fc_a", "double", DOUBLE, agent="assistant")
    request_on(pool, "assistant")
    monkeypatch.setenv(switch.ENV, "off")
    f = ForgeToolFactory(Base(), runtime(pool))
    assert [t.spec.name for t in f.with_pack(Info(), T0, HarnessPack(0, {}))] == ["read_page", "echo"]


def test_assistant_factory_nothing_opted_in_is_the_base(pool, users):
    f = ForgeToolFactory(Base(), runtime(pool))
    assert [t.spec.name for t in f.with_pack(Info(), T0, HarnessPack(0, {}))] == ["read_page", "echo"]


def test_assistant_factory_adds_meta_tools_fixed_across_turns(pool, users):
    request_on(pool, "assistant")
    live(pool, "fc_r", "reader", READER, agent="assistant", host_tools=("read_page",))
    f = forge_factory(Base(), runtime(pool))
    first = f.with_pack(Info(), T0, HarnessPack(0, {}))
    assert [t.spec.name for t in first] == ["read_page", "echo", "reader", "call_tool",
                                            "request_tool"]
    assert specs(f.with_pack(Info(), T0.replace(minute=5), HarnessPack(0, {}))) == specs(first)   # next turn: same bytes
    # the forged tool reads through the turn's own read_page with the session's context
    read_page = first[0]
    out = first[2].run({"p": "p9"}, CTX)
    assert out.error is None and json.loads(out.content) == {"t": "<p9>"}
    assert read_page.ctxs[0].user_id == "u1" and read_page.ctxs[0].session_id == "s1"


def test_assistant_factory_durable_copy_wraps_the_base_durable():
    base = Base()
    d = ForgeToolFactory(base, object()).durable()   # type: ignore[arg-type]
    assert isinstance(d, ForgeToolFactory) and base.durable_called


# --- the service runtime ----------------------------------------------------------------------

class NoWasi:
    policy = "read-only"

    def run_tool(self, files, args, host=None):
        raise SandboxUnavailable("no wasmtime")


def test_missing_wasi_runtime_does_not_break_the_pin(pool, users):
    live(pool, "fc_a", "double", DOUBLE)
    run = runtime(pool, sandbox=NoWasi()).for_run(user_id="u1", agent="deep", run_id="r1",
                                                   group="page", pack_values={})
    assert run is not None
    tool = run.tools([], CTX)[0]
    res = tool.run({"n": 1}, CTX)
    assert res.error is not None and "这个工具现在不能运行" in res.content


def test_service_forge_without_a_model_answers_unavailable(pool, users):
    request_on(pool)
    rt = build_forge(pool, None, HashingEmbedder(dim=256), lambda: T0)
    run = rt.for_run(user_id="u1", agent="deep", run_id="r1", group="page", pack_values={})
    assert run is not None
    req = run.tools([], CTX)[-1]
    assert req.spec.name == "request_tool"
    res = req.run({"name": "double", "title": "翻倍", "purpose": "把 n 翻倍",
                   "examples": [{"args": {"n": 2}, "expect": {"n": 4}}]}, CTX)
    assert res.error is not None and UNAVAILABLE in res.content



def unfence(text: str) -> str:
    """A read tool's result as the tool returned it (the loop fences it as material)."""
    if DATA_BEGIN not in text:
        return text
    return text.split(DATA_BEGIN + "\n", 1)[1].rsplit("\n" + DATA_END, 1)[0]
