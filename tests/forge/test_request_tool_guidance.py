"""request_tool / call_tool descriptions: say WHEN to forge (repeated per-item work, equivalence
checks), stay byte-stable (prompt cache), and never name internals (P3 T1)."""
import hashlib
import json
from datetime import UTC, datetime

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.forge import run
from evowiki.forge.run import ForgeRun, ForgeRuntime
from evowiki.retrieval.embed import HashingEmbedder
from tests.forge.fakes import InProcessSandbox

GOLDEN = {  # update deliberately when the texts change (prompt cache + H2 baseline move with it)
    "request_tool": "7587156553d03bc1",
    "call_tool": "9bce98abe3c3405d",
}
CTX = ToolContext(user_id="u1", session_id="s1")


class _Trace:
    def start(self, *a, **kw): ...
    def model(self, *a, **kw): ...
    def tool(self, *a, **kw): ...
    def finish(self, *a, **kw): ...


@pytest.fixture
def forge_run_fixture():
    rt = ForgeRuntime(pool=None, gateway=None, sandbox=InProcessSandbox(),  # type: ignore[arg-type]
                      embedder=HashingEmbedder(dim=256), trace=_Trace(),
                      clock=lambda: datetime(2026, 10, 23, tzinfo=UTC))
    return ForgeRun(agent="assistant", reachable={}, first_class=(), request_tool_on=True,
                    user_id="u1", run_id="r1", group="assistant", runtime=rt)  # type: ignore[arg-type]


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def test_request_tool_says_when_to_use():
    d = run.REQUEST_TOOL_DESCRIPTION
    for phrase in ("3 个以上", "逐个", "等价", "call_tool", "纯计算", "作为参数传入", "只说结果"):
        assert phrase in d, phrase
    assert len(d) <= 1000


def test_no_internal_words():
    for d in (run.REQUEST_TOOL_DESCRIPTION, run.CALL_TOOL_DESCRIPTION):
        for word in ("沙箱", "锻造", "forge", "DeepSeek", "模型", "WASI"):
            assert word not in d, word


def test_tool_specs_use_the_constants(forge_run_fixture):
    tools = {t.spec.name: t for t in forge_run_fixture.tools([], CTX)}
    assert tools["request_tool"].spec.description == run.REQUEST_TOOL_DESCRIPTION
    assert tools["call_tool"].spec.description == run.CALL_TOOL_DESCRIPTION


def test_tool_list_is_byte_stable_across_rounds(forge_run_fixture):
    def defs():
        ts = forge_run_fixture.tools([], CTX)
        return json.dumps([t.spec.model_dump(mode="json") for t in ts], ensure_ascii=False,
                          sort_keys=True)
    assert defs() == defs()


def test_golden():
    assert _sha(run.REQUEST_TOOL_DESCRIPTION) == GOLDEN["request_tool"]
    assert _sha(run.CALL_TOOL_DESCRIPTION) == GOLDEN["call_tool"]
