import json

from evowiki.forge.api import split_examples
from evowiki.forge.build import build
from evowiki.forge.coder import examples_json, first_message, start_workspace
from evowiki.forge.models import ToolExample, ToolRequest
from tests.forge.fakes import InProcessSandbox

EX = tuple(ToolExample(args={"n": i}, expect={"n": 2 * i}) for i in range(6))
SCHEMA = '{"type":"object","description":"d","properties":{"n":{"type":"integer"}}}'
DOUBLE = {"tool.py": "def run(args):\n    return {'n': 2 * args['n']}\n",
          "test_tool.py": "import tool\ndef test_a():\n    assert tool.run({'n': 1}) == {'n': 2}\n",
          "schema.json": SCHEMA}


def req(examples=EX, holdout=()):
    return ToolRequest(name="double", title="翻倍", purpose="翻倍", agent="deep",
                       examples=examples, holdout=holdout)


def ws(files):
    return type("W", (), {"files": files})()


def test_split_is_deterministic_and_order_free():
    a = split_examples(req())
    b = split_examples(req(examples=tuple(reversed(EX))))
    assert set(map(repr, a.holdout)) == set(map(repr, b.holdout)) and len(a.holdout) == 2
    assert len(a.visible) == 4


def test_single_example_holds_nothing_out():
    s = split_examples(req(examples=EX[:1]))
    assert s.holdout == () and s.visible == EX[:1]


def test_explicit_holdout_wins():
    s = split_examples(req(examples=EX[:2], holdout=EX[2:3]))
    assert s.visible == EX[:2] and s.holdout == EX[2:3]


def test_request_for_coder_has_only_visible():
    s = split_examples(req())
    assert s.request_for_coder.examples == s.visible and s.request_for_coder.holdout == ()
    assert examples_json(s.request_for_coder) == examples_json(req(examples=s.visible))


def test_coder_never_sees_holdout():
    s = split_examples(req())
    ws_ = start_workspace(s.request_for_coder)
    text = first_message(s.request_for_coder, ws_).text + ws_.files["examples.json"]
    shown = json.loads(ws_.files["examples.json"])
    for ex in s.holdout:
        assert ex.model_dump(mode="json", exclude={"host"}) not in shown
        assert json.dumps(ex.model_dump(mode="json", exclude={"host"}), ensure_ascii=False) not in text
    assert all(json.dumps(e.model_dump(mode="json", exclude={"host"}), ensure_ascii=False) in text
               for e in s.visible)
    assert len(shown) == len(s.visible)


def test_lookup_table_tool_fails_on_holdout():
    s = split_examples(req())
    table = {e.args["n"]: e.expect for e in s.visible}
    files = {"tool.py": f"T = {table!r}\ndef run(args):\n    return T[args['n']]\n",
             "test_tool.py": "import tool\ndef test_a():\n"
                             f"    assert tool.run({{'n': {s.visible[1].args['n']}}})\n",
             "schema.json": SCHEMA}
    rep = build(ws(files), req(), InProcessSandbox())
    assert not rep.ok and rep.holdout_failed == 2 and "留出" in rep.reason
    assert rep.examples_passed == 4 and rep.holdout_total == 2
    for ex in s.holdout:   # never echo the held-out values
        assert str(ex.expect["n"]) not in rep.reason.replace("2 个", "")


def test_general_tool_passes_holdout():
    rep = build(ws(DOUBLE), req(), InProcessSandbox())
    assert rep.ok and rep.examples_passed == 4 and rep.holdout_passed == 2
    assert rep.holdout_failed == 0 and rep.holdout_total == 2
    assert rep.reason == "测试 1 个、样例 4 个、留出样例 2 个全部通过"


def test_no_holdout_reason_says_so():
    rep = build(ws(DOUBLE), req(examples=EX[:1]), InProcessSandbox())
    assert rep.ok and rep.holdout_total == 0 and "没有留出样例" in rep.reason


def test_visible_failure_stops_before_holdout():
    bad = dict(DOUBLE, **{"tool.py": "def run(args):\n    return {'n': 0}\n",
                          "test_tool.py": "import tool\ndef test_a():\n    assert tool.run({'n': 1})\n"})
    rep = build(ws(bad), req(), InProcessSandbox())
    assert not rep.ok and rep.holdout_passed == 0 and rep.holdout_failed == 0
    assert rep.holdout_total == 2 and "留出" not in rep.reason


def test_propose_hides_holdout_from_coder_and_archives_it(pool):
    from datetime import UTC, datetime

    from evowiki.content.models import Lang
    from evowiki.forge.api import propose
    from evowiki.store.users import UserRow, create_user
    from tests.forge.test_propose import EVI, deps, row, script

    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2", lang=Lang.EN,
                                  created_at=datetime(2026, 10, 22, tzinfo=UTC)))
    user = "u1"

    conv = script()
    s = split_examples(req())
    res = propose("tool", req(), EVI, user_id=user, deps=deps(pool, conv))
    assert res.build.ok and res.build.holdout_passed == 2 and res.build.examples_passed == 4
    seen = "".join(repr(r) for r in conv.requests)
    line = lambda ex: json.dumps(ex.model_dump(mode="json", exclude={"host"}), ensure_ascii=False)
    assert all(line(ex) in seen for ex in s.visible)
    assert not any(line(ex) in seen for ex in s.holdout)
    r = row(pool, res.candidate_id)
    assert len(r.spec["examples"]) == 6 and "examples.json" in r.files
    assert len(json.loads(r.files["examples.json"])) == 4
