import pytest
from pydantic import ValidationError

from evowiki.deep.contracts import (
    AssetMeta,
    InteractiveDraft,
    Probe,
    RefineDraft,
    RefinedSection,
    RefinePlan,
    RenderCheck,
    embed_block,
)
from evowiki.goals.contracts import SectionRole

PLAN = RefinePlan(known="k", observable="o", one_at_a_time=("a",), contrast="c", spoken_answer="s")


def _sec(sid="s1", body="看 {{interactive:pair-builder}}"):
    return RefinedSection(section_id=sid, heading="h", role=SectionRole.EXPLAIN, depth="interactive",
                          cue="c", body=body)


def _ix(section_id="s1"):
    return InteractiveDraft(interactive_id="pair-builder", section_id=section_id, title="t",
                            learning_question="q", html="<p>x</p>", height=800,
                            probes=(Probe(target="#a", action="click", expect_change="#out"),))


def test_probe_value_rule():
    Probe(target="#i", action="input", value="3", expect_change="#o")
    with pytest.raises(ValidationError):
        Probe(target="#i", action="input", expect_change="#o")
    with pytest.raises(ValidationError):
        Probe(target="#b", action="click", value="x", expect_change="#o")


def test_interactive_bounds_and_id():
    with pytest.raises(ValidationError):
        InteractiveDraft.model_validate({**_ix().model_dump(), "height": 300})
    with pytest.raises(ValidationError):
        InteractiveDraft.model_validate({**_ix().model_dump(), "interactive_id": "Bad_Id"})


def test_draft_interactive_must_be_placed_and_owned():
    RefineDraft(page_id="p", plan=PLAN, sections=(_sec(),), interactives=(_ix(),))
    with pytest.raises(ValidationError):
        RefineDraft(page_id="p", plan=PLAN, sections=(_sec(body="no marker"),), interactives=(_ix(),))
    with pytest.raises(ValidationError):
        RefineDraft(page_id="p", plan=PLAN, sections=(_sec(),), interactives=(_ix("s9"),))


def test_embed_block_matches_user_wiki_format():
    meta = AssetMeta(asset_id="a1", page_id="p", section_id="s1", interactive_id="pair-builder",
                     title="配对\n实验", height=800, sha256="0" * 64,
                     check=RenderCheck(interactive_id="pair-builder", ok=True))
    assert embed_block(meta, "assets/pair-builder.html") == (
        "```embedded-html\ntype: embedded-html\nid: pair-builder\nsrc: assets/pair-builder.html\n"
        "title: 配对 实验\nheight: 800\n```")
