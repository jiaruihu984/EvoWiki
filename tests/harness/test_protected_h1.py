"""P0 (RSI permission audit 2026-10-28) on master's anchor marks (`llm.prompt_steps.Step.anchor`,
harness/anchors.H1_CLOSED_STEPS, diff.closed_h1): the harness improver (track H) never rewrites
the feedback reader nor the teaching import (RSI-B's own prompts retired with it on 2026-10-29). diff.refuse refuses them, read_pack does not list them, and the re-check at apply
(round._check) refuses them. The user still sets them. Fake models only."""

import pytest

from evowiki.harness import round as round_mod
from evowiki.harness.anchors import H1_CLOSED_STEPS
from evowiki.harness.diff import KeyOp, PackDiff, closed_h1, refuse
from evowiki.harness.pack import EMPTY
from evowiki.service.harness_defaults import defaults
from evowiki.store import harness as pack_store
from tests.harness.test_improver_3b import KEY_ROW, _pack
from tests.harness.test_round import NOW, Env, FakeImprover, U

CLOSED = sorted(f"H1.{k}" for k in H1_CLOSED_STEPS)


def test_the_closed_set_holds_feedback():
    assert set(CLOSED) == {"H1.feedback", "H1.language_style"}
    assert all(closed_h1(n) for n in CLOSED) and not closed_h1("H1.write")


@pytest.mark.parametrize("name", CLOSED)
def test_refuse_rejects_every_closed_step(name):
    for value in ("换一种比较方法", None):            # a new text, or back to the default
        why = refuse(PackDiff((KeyOp(name, value),)), EMPTY, {}, defaults=defaults())
        assert why is not None and name in why
    assert refuse(PackDiff((KeyOp("H1.write", "先举例"), KeyOp(name, "x"))), EMPTY, {},
                  defaults=defaults()) is not None
    assert refuse(PackDiff((KeyOp("H1.write", "先举例"),)), EMPTY, {}, defaults=defaults()) is None


def test_read_pack_does_not_list_them(pool):
    names = set(KEY_ROW.findall(_pack(pool)))
    assert "H1.write" in names
    assert not names & set(CLOSED)
    assert not set(KEY_ROW.findall(_pack(pool, dim="H1"))) & set(CLOSED)


@pytest.mark.parametrize("name", ["H1.feedback"])
def test_the_apply_recheck_refuses_them(pool, name):
    env = Env(pool)
    diff = PackDiff((KeyOp(name, "新的写法"),))
    with pool.connection() as conn, pytest.raises(round_mod._Skip):
        round_mod._check(conn, U, diff, {name: None}, env.deps)


def test_a_round_proposing_one_never_writes_it(pool):
    env = Env(pool, improver=FakeImprover(PackDiff((KeyOp("H1.feedback", "宽松一点"),))))
    r = env.run()
    assert r.status != "applied"
    assert "H1.feedback" not in env.pack().values


def test_the_user_still_sets_them(pool):
    env = Env(pool)
    with pool.connection() as conn:
        pack_store.set_value(conn, U, "H1.feedback", "我自己的写法", source="user", why="我改的",
                             now=NOW)
    assert env.pack().values["H1.feedback"] == "我自己的写法"
