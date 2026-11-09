"""Offline cases: loading, the topic split, and the in-memory world the harness replay runs on."""

import json

import pytest

from evowiki.harness.diff import PackDiff
from evowiki.harness.offline.evalset import OfflineCase, load, profiles, split, stand_in, world
from evowiki.harness.pack import EMPTY
from evowiki.harness.replay import ReplayDeps, replay
from evowiki.wikiapp.refine_spec import WikiSpecSource
from tests.deep.fakes import DeepScript
from tests.harness.offline.fixtures import USER, case, one_write, service


def test_split_is_by_topic_and_stable():
    cases = [case(i, f"topic-{i // 2}") for i in range(40)]
    dev, test = split(cases)
    assert dev and test and len(dev) + len(test) == 40
    assert not {c.topic for c in dev} & {c.topic for c in test}
    assert split(cases) == (dev, test)
    assert split(cases, seed="other") != (dev, test)


def test_load_rejects_a_case_without_learner_words(tmp_path):
    p = tmp_path / "cases.jsonl"
    row = {"id": "x", "topic": "t", "source": "C", "heading": "h", "learner": "l",
           "original": "旧"}
    p.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="no 'said'"):
        load(p)
    p.write_text(json.dumps({**row, "source": "D"}, ensure_ascii=False), encoding="utf-8")
    assert load(p)[0].exemplar


def test_a_case_replays_through_the_harness_replay():
    cases = [case(1, "a"), case(2, "b", source="D")]
    repo, hcases = world(cases, user_id=USER)
    assert hcases["c2"].anchor and not hcases["c1"].anchor
    # the learner is in the profile, not in the instruction; each case has its own stand-in user
    assert "这段看不懂" in hcases["c1"].instruction and "零基础" not in hcases["c1"].instruction
    assert hcases["c1"].user_id == stand_in(USER, cases[0]) != hcases["c2"].user_id
    ds = one_write(DeepScript(), "新讲解：先说它是什么。")
    hc = hcases["c1"]
    spec = WikiSpecSource(repo).spec(repo.load(USER))
    out = replay(hc, PackDiff(), base_pack=EMPTY,
                 base_spec=spec, deps=ReplayDeps(deep=service(repo, ds, cases), repo=repo))
    assert "新讲解：先说它是什么。" in out.text
    assert out.case_id == "c1" and out.writer_provider == "anthropic"
    assert not ds.pending()
    # the gateway appended the stand-in's profile to every writer call, as online
    assert ds.requests and all("## 我是谁" in r.system and "零基础，学过高中数学" in r.system
                               for r in ds.requests)


def test_profiles_give_each_stand_in_its_own_learner():
    cases = [case(1, "a"), OfflineCase(id="c2", topic="b", source="C", heading="h",
                                       learner="研究生，做过机器学习", said="太浅", original="旧")]
    ctx = profiles(cases, user_id=USER)
    assert "零基础" in ctx(stand_in(USER, cases[0]), "deep.write")
    assert "研究生" in ctx(stand_in(USER, cases[1]), "deep.write")
    assert ctx(USER, "deep.write") == "" and ctx(stand_in(USER, cases[0]), "rsi.h.judge") == ""
