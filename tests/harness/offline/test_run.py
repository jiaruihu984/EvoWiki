"""One offline round on fakes: a quality-marker writer and judge for the decisions, and one round on
the harness's real replay + blind judge with scripted providers (no real model)."""

import hashlib
import re

from evowiki.harness import replay as replay_module
from evowiki.harness.diff import KeyOp, PackDiff
from evowiki.harness.judge import Comparison
from evowiki.harness.offline.evalset import split, world
from evowiki.harness.offline.run import harness_judge, harness_write, markdown, run_offline
from evowiki.harness.pack import EMPTY
from evowiki.harness.replay import ReplayDeps, ReplayResult
from evowiki.wikiapp.refine_spec import WikiSpecSource
from tests.deep.fakes import DeepScript
from tests.harness.offline.fixtures import USER, case, one_write, service
from tests.harness.test_judge import ACC, judge_gw

GOOD = PackDiff(ops=(KeyOp("H1.write", "先用一句话说它是什么。"),))
NULL = PackDiff(ops=(KeyOp("H1.write", "写得自然一些。"),))


def _noise(*parts) -> float:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(h[:4], "big") / 2**32 - 0.5           # −0.5 … 0.5


def fake(effects: dict[PackDiff, float], *, fail: str | None = None):
    """A writer whose text carries its quality, and a judge that names the higher one."""
    def write(c, diff, i):
        if c.id == fail:
            raise LookupError("快照里没有这一段")
        q = effects.get(diff, 0.0) + _noise(c.id, i, id(diff) if diff not in effects else 0)
        return ReplayResult(c.id, f"质量 {q:.3f}", "anthropic", f"run-{c.id}-{i}", 0.01)

    def judge(c, a, b):
        qa, qb = (float(re.search(r"[-\d.]+$", x.text).group()) for x in (a, b))
        w = "a" if qa - qb > 0.15 else "b" if qb - qa > 0.15 else "tie"
        return Comparison("cmp", c.id, a.run_id, b.run_id, w, "", "", False, ("", ""))
    return write, judge


CASES = [case(i, f"topic-{i}") for i in range(60)] + [case(90 + i, f"ex-{i}", source="D")
                                                     for i in range(4)]


def test_a_good_candidate_is_chosen_and_a_null_one_is_not():
    dev, held = split(CASES)
    write, judge = fake({GOOD: 0.6, NULL: 0.0, PackDiff(): 0.0})
    rep = run_offline(dev, held, {"good": GOOD, "null": NULL}, write=write, judge=judge)
    assert rep.aa is not None and rep.aa[0]
    by = {o.name: o for o in rep.candidates}
    assert by["good"].dev.passed and by["good"].held_out.passed
    assert not by["null"].dev.passed and by["null"].held_out is None
    assert rep.chosen == "good"
    text = markdown(rep)
    assert "采用候选 good" in text and "A/A" in text


def test_a_one_sided_pipeline_voids_the_round_before_any_candidate_is_written():
    dev, held = split(CASES)
    calls = []

    def write(c, diff, i):
        calls.append(diff)
        return ReplayResult(c.id, f"质量 {i}", "anthropic", f"r{i}", 0.0)

    def judge(c, a, b):      # always prefers the later sample: a biased pipeline
        return Comparison("cmp", c.id, a.run_id, b.run_id, "a", "", "", False, ("", ""))
    rep = run_offline(dev, held, {"good": GOOD}, write=write, judge=judge)
    assert rep.aa == (False, 1.0) and rep.chosen is None and rep.candidates == ()
    assert GOOD not in calls
    assert "作废" in markdown(rep)


def test_a_failed_write_drops_its_case_and_says_why():
    dev, held = split(CASES)
    write, judge = fake({GOOD: 0.6, PackDiff(): 0.0}, fail=dev[0].id)
    rep = run_offline(dev, held, {"good": GOOD}, write=write, judge=judge)
    assert "快照里没有这一段" in rep.unusable[dev[0].id]
    assert rep.candidates[0].dev.cases == sum(not c.exemplar for c in dev) - 1


def test_a_round_on_the_harness_replay_and_blind_judge(monkeypatch):
    # one script for every replay, popped turn by turn: at once, replays would take each other's turns
    monkeypatch.setattr(replay_module, "REPLAY_WORKERS", 1)
    dev, held = [case(1, "t1"), case(2, "t2")], [case(3, "t3")]
    repo, hcases = world(dev + held, user_id=USER)
    ds = DeepScript()
    for body in ("旧写法，分数一。", "旧写法，分数一。", "新写法，分数二。", "新写法，分数二。",
                 "旧写法，分数一。", "新写法，分数二。"):
        one_write(ds, body)
    deps = ReplayDeps(deep=service(repo, ds, dev + held), repo=repo)
    write = harness_write(hcases, base_pack=EMPTY,
                          base_spec=WikiSpecSource(repo).spec(repo.load(USER)), deps=deps)

    def decide(first, second):
        win = "甲" if "分数二" in first else "乙" if "分数二" in second else "same"
        return {"better": win, "quote": "新写法，分数二。" if win != "same" else "",
                "reason": "先说了是什么"}
    judge = harness_judge(hcases, gw=judge_gw(decide), account=ACC)
    rep = run_offline(dev, held, {"good": GOOD}, write=write, judge=judge, k=1, aa=False)
    assert rep.chosen == "good", markdown(rep)
    assert not ds.pending() and not rep.unusable
