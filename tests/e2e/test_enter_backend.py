"""The Stage 2 walkthrough's scripted goals.enter (tests/e2e/enter_backend.py) answers the real
prompt: the node's parts, and `known` only from his fenced words when the estimate part is there."""

from evowiki.goals.intake import IntakeScope, _outline_prompt
from tests.e2e.enter_backend import enter_reply

GOAL = "我要学黑壳虾"
WORDS = ("我要学黑壳虾", "换水我自己会做，水质检测从没学过")


def _prompt(name: str, words=WORDS, ancestors=()):
    scope = IntakeScope(goal=f"{GOAL} · {name}", region_id="c2", region_name=name,
                        parent_goal=GOAL, siblings=((name, ""),), ancestors=ancestors)
    return _outline_prompt(scope.goal, (), scope, words=words)


def test_a_block_with_his_words_gets_its_parts_and_verbatim_known():
    out = enter_reply(_prompt("水质与环境"))
    assert [(p["name"], p["atomic"]) for p in out["parts"]] == [
        ("水温", True), ("换水", True), ("水质检测", False)]
    assert out["known"] == [
        {"part": "换水", "level": "intermediate", "quote": "换水我自己会做"},
        {"part": "水质检测", "level": "unlearned", "quote": "水质检测从没学过"}]


def test_a_sub_block_and_no_words():
    out = enter_reply(_prompt("水质检测", words=(), ancestors=(("c2", "水质与环境"),)))
    assert [p["name"] for p in out["parts"]] == ["氨氮", "pH"] and "known" not in out
