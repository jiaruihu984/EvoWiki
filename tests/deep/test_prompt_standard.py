"""Teaching-standard injection in the deep prompts (fake gateway only), and the self-check rules
derived from the standard's CHECKLIST."""

from evowiki.deep import agent
from evowiki.deep.contracts import (
    BUILTIN_REVIEW_RULES,
    RefinePlan,
    ReviewFinding,
)
from evowiki.deep.tools import BUILTIN_TEXT
from evowiki.teach.standard import CHECKLIST, SECTION_KEYS, marker

WRITE_KEYS = {"order", "terms_first", "spoken_first", "step_trace", "tables_after",
              "visual_question", "interactive_tiers", "templates", "two_views", "depth", "learner",
              "language",
              "personal_examples", "sources", "reading"}


def _present(text: str) -> set[str]:
    return {k for k in SECTION_KEYS if marker(k) in text}


def test_write_system_injects_exactly_its_sections_and_keeps_the_library_rule():
    # deep@v32: the standard reaches the writer through its references (teaching preloaded,
    # sources with materials), not the system prompt
    from evowiki.deep import skill
    assert _present(agent.WRITE_SYSTEM) == set()
    assert _present(skill.ref("teaching")) | _present(skill.ref("sources")) | _present(skill.ref("reading")) == (
        WRITE_KEYS - {"visual_question", "interactive_tiers", "templates"})
    # (was: 「只能从「可用的交互组件」清单里选」/「不能自己写」, sentences the prompt lost before EvoUI v2)
    assert "不得编造不存在的组件" in skill.ref("interaction")
    assert "HTML 和脚本只能写在 app 节点里" in agent.WRITE_SYSTEM


def test_write_system_guides_the_learning_nodes_and_the_app_tier():
    from evowiki.deep import skill
    w = agent.WRITE_SYSTEM + skill.ref("evoui/index") + skill.ref("interaction")
    # deep@v32: the tiers in the EvoUI index, the structures in interaction, read_ref for the rest
    for must in ("draw_visual", "chart 或 plot", "read_ref", "fallback"):
        assert must in w, must


def test_prompt_version_bumped():
    assert agent.PROMPT_VERSION == "deep@v42"


def test_builtin_rules_come_from_the_checklist():
    ids = [c.id for c in CHECKLIST]
    # deep@v31: the six dimensions only (the seven extra rules are covered by them)
    assert list(BUILTIN_REVIEW_RULES) == ids == ["accuracy", "coverage", "fits_learner",
                                                 "coherence", "useful", "structure"]
    assert set(BUILTIN_REVIEW_RULES) == set(BUILTIN_TEXT)


def test_legacy_keys_map_to_checklist_and_old_findings_still_parse():
    assert ReviewFinding(rule="term_before_use", ok=True).rule == "term_before_use"


def test_old_stored_plan_without_visual_questions_parses():
    p = RefinePlan(known="k", observable="o", one_at_a_time=("a",), contrast="c", spoken_answer="s")
    assert p.visual_questions == ()
