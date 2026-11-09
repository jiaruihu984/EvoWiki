"""Task 8 (trial fix T2): every wiki-flow prompt carries the user's language requirement."""

import pytest

from evowiki.assistant.compact import compact
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import run_turn
from evowiki.goals.blocks import blocks_prompt
from evowiki.goals.guardrails import DraftOutline, page_bands, to_outline
from evowiki.goals.intake import IntakeScope
from evowiki.goals.intake import _outline_prompt as intake_prompt
from evowiki.goals.language import language_rule
from evowiki.goals.path import path_prompt
from evowiki.llm.chat import ChatTurn
from tests.assistant.fakes import Data
from tests.assistant.test_compact import ACC, GOOD, _gw, state
from tests.assistant.test_loop import INFO, setup
from tests.goals.fakes_k import ZSCORE_OUTLINE, zscore_k, zscore_spec
from tests.goals.test_path_fakes import k_for, transformer

LANGS = ("zh", "en")


def _check(prompt: str, lang: str) -> None:
    assert language_rule(lang) in prompt
    other = "en" if lang == "zh" else "zh"
    assert language_rule(other) not in prompt
    assert "用中文" not in prompt and "全部用中文" not in prompt
    assert "中文 Markdown" not in prompt


class _Stop(Exception):
    pass


def _capture():
    seen: list[str] = []

    def llm(role, purpose, prompt, schema):
        seen.append(prompt)
        raise _Stop

    return llm, seen


def _outline(lang):
    spec, k = zscore_spec(), zscore_k()
    spec = spec.model_copy(update={"language": lang})
    return spec, k, to_outline(DraftOutline.model_validate(ZSCORE_OUTLINE), spec,
                               page_bands(spec, k))


@pytest.mark.parametrize("lang", LANGS)
def test_intake_prompt(lang):
    scope = IntakeScope(goal="学 z 分数 · z 分数", region_id="c1", region_name="z 分数",
                        parent_goal="学 z 分数", siblings=(("z 分数", ""),))
    _check(intake_prompt("学 z 分数", [], scope, lang), lang)
    _check(blocks_prompt("学 z 分数", ("我会算均值",), [], lang), lang)


@pytest.mark.parametrize("lang", LANGS)
def test_path_prompt(lang):
    g = transformer()
    fresh = {c.id: (f"{c.id}.term",) for c in g.concepts}
    p = path_prompt(g, fresh, language=lang)
    _check(p, lang)
    assert ("学完这页你能”开头" in p or "以「学完这页你能」开头" in p) == (lang == "zh")
    assert k_for(g) is not None


@pytest.mark.parametrize("lang", LANGS)
def test_assistant_system_prompt(tmp_path, lang):
    class D(Data):
        def language(self, user_id):
            return lang

    deps, prov, _ = setup(tmp_path, [ChatTurn(text="答")])
    deps.data = D()
    list(run_turn(deps, INFO, MessageIn(text="问")))
    _check(prov.chat_calls[0].system, lang)


@pytest.mark.parametrize("lang", LANGS)
def test_compaction_system_prompt(tmp_path, lang):
    prov, gw = _gw(tmp_path, GOOD)
    compact(state(10, size=500), gw, ACC, cut=6, language=lang)
    _check(prov.calls[0].system, lang)
