"""Teaching-standard injection in the outline part (fake gateways only) and the
prerequisite-fold guardrail."""


from evowiki.goals.guardrails import (
    page_bands,
)
from evowiki.teach.standard import SECTION_KEYS, marker
from tests.goals.fakes_k import zscore_k, zscore_spec


def present(text: str) -> set[str]:
    return {k for k in SECTION_KEYS if marker(k) in text}


def test_the_outline_part_injects_its_sections_only():
    from evowiki.deep.agent import outline_part
    # deep@v32: page boundaries are the path's; the outline part carries no standard section
    assert present(outline_part(zscore_spec(), zscore_k())) == set()


def test_the_prompt_spells_out_where_every_cell_may_go():
    from evowiki.deep.agent import outline_part
    from evowiki.goals.outline_context import assumable_text
    from tests.goals.fakes_k import zscore_k, zscore_spec
    spec, k = zscore_spec(), zscore_k()
    # deep@v32: what each band means is said once (outline reference); only the data is listed
    line = assumable_text(spec, page_bands(spec, k))
    assert line.startswith("- 可以放进「默认你会」（assumed）的格子：") and "mean.term" in line
    assert "z_score" not in line and line in outline_part(spec, k)
