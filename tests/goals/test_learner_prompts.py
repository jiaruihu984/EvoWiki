"""Task 4 of prompt calibration: the learner-facing prompts (intake, diagnostic generate/verify,
path) carry exactly the teaching-standard sections their step needs — fake LLM only."""

import pytest

from evowiki.goals.blocks import blocks_prompt
from evowiki.goals.intake import IntakeError, IntakeScope
from evowiki.goals.intake import _outline_prompt as intake_prompt
from evowiki.goals.path import path_prompt
from evowiki.teach.standard import SECTION_KEYS, marker
from evowiki.wikiapp.llm import JSON_PROMPT_VERSION
from tests.llm_cases.inventory import REMOVED_PURPOSES

from .test_path_fakes import transformer

SCOPE = IntakeScope(goal="我想学方差 · 方差", region_id="c1", region_name="方差", scope="定义",
                    parent_goal="我想学方差", siblings=(("方差", "定义"),))


class Stop(Exception):
    pass


class Capture:
    """A JsonLLM that records the prompt and stops (nothing is parsed)."""

    def __init__(self):
        self.prompts: list[str] = []

    def __call__(self, role, purpose, prompt, schema):
        self.prompts.append(prompt)
        raise Stop


def only(prompt: str, *keys: str) -> None:
    for k in SECTION_KEYS:
        assert (marker(k) in prompt) == (k in keys), k


def captured(fn) -> str:
    cap = Capture()
    with pytest.raises((Stop, IntakeError)):
        fn(cap)
    return cap.prompts[0]


def test_intake_carries_no_teaching_standard():
    """intake@v15 (user ruling 2026-10-14): the planning questions are a page-writing rule,
    unrelated to decomposition; neither S1 nor an entered block's outline carries standard text."""
    only(blocks_prompt("我想学方差", ("我会算平均数",), []))     # S1
    p = intake_prompt("我想学方差", [], SCOPE)                 # the entered block's outline
    only(p)
    assert "当桥" not in p and "不要套通用的清单" not in p


def test_path_gets_path_order_overview_first_and_a_short_order_excerpt():
    p = path_prompt(transformer(), {})
    only(p, "path_order", "overview_first", "order")     # path@v10: no 「scope」 (default separate)
    assert "真正互不相关的两件事才分两页" not in p
    assert "先学协方差，再学特征值与特征向量" in p
    assert "7. 可迁移的规则" not in p                                # only the opening of `order`
    assert "怎样分页最好" in p and "容量" not in p                  # principles, no capacity


def test_prompt_versions_were_bumped_and_verify_removed():
    v = JSON_PROMPT_VERSION
    assert (v["goals.intake"], v["goals.path"]) == ("intake@v38", "path@v10")
    assert "goals.ask" not in v                     # record_evidence in the turn (stage 3 T9)
    assert not set(REMOVED_PURPOSES) & set(v)
    assert "goals.diagnostic.verify" not in v and "goals.diagnostic.generate" not in v
