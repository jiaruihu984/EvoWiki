"""Improver H8 examples go through harness.skills (render checked) when a round applies them
(harness phase 3b Task 4). Uses the phase-2 round fakes; the checker is a fake."""

import json
from dataclasses import replace

from evowiki.deep.evoui import EXAMPLES_PATH
from evowiki.harness import round as round_mod
from evowiki.harness.diff import KeyOp, PackDiff
from tests.deep.fakes import FakeChecker
from tests.harness.test_round import Env, FakeImprover

EXAMPLE = json.loads(EXAMPLES_PATH.read_text(encoding="utf-8"))[0]
H8 = PackDiff(ops=(KeyOp("H8.deep.evoui_examples", [EXAMPLE]),))


def test_an_example_is_render_checked_and_stored(pool):
    chk = FakeChecker()
    env = Env(pool, improver=FakeImprover(H8))
    env.deps = replace(env.deps, render=chk)
    r = env.run()
    assert r.status == "applied", r.reason
    assert chk.checked == ["example-1"]
    assert env.pack().values["H8.deep.evoui_examples"][0]["name"] == EXAMPLE["name"]


def test_a_failed_render_check_fails_the_round_and_leaves_the_pack(pool):
    chk = FakeChecker(fail_block=lambda bid, b: "画面溢出")
    env = Env(pool, improver=FakeImprover(H8))
    env.deps = replace(env.deps, render=chk)
    r = env.run()
    assert r.status == "failed" and "渲染检查没通过" in r.reason
    assert env.pack().version == 0 and env.pack().values == {} and env.changes() == []


def test_no_render_checker_refuses_an_example(pool):
    env = Env(pool, improver=FakeImprover(H8))
    r = env.run()
    assert r.status == "failed" and "没有渲染检查，不能加交互块示例" in r.reason
    assert r.reason.endswith(round_mod.NO_RENDER) or round_mod.NO_RENDER in r.reason
    assert env.pack().values == {}
