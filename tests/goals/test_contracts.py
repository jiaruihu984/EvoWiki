import pytest
from pydantic import ValidationError

from evowiki.goals.contracts import (
    Aspect,
    BandParams,
    CheckItem,
    Observation,
    OutlineSection,
    PageOutline,
    SectionRole,
    cell_id,
    split_cell,
)


def test_cell_ids_round_trip():
    assert cell_id("z_score", Aspect.INTERPRET) == "z_score.interpret"
    assert split_cell("z_score.interpret") == ("z_score", Aspect.INTERPRET)
    with pytest.raises(ValueError):
        split_cell("z_score.vibes")
    with pytest.raises(ValueError):
        split_cell("noaspect")


def test_band_params_ordered_and_clamped():
    BandParams()
    with pytest.raises(ValidationError):
        BandParams(likely_min_m=0.65, known_min_m=0.62, shaky_min_m=0.3)
    with pytest.raises(ValidationError):
        BandParams(known_min_m=0.99)


def test_self_report_must_quote():
    with pytest.raises(ValidationError):
        Observation(cell="mean.procedure", polarity="+", strength=0.8, kind="self", evidence_ref="e1")
    Observation(cell="mean.procedure", polarity="+", strength=0.8, kind="self", evidence_ref="e1", quote="均值会算")


def test_check_item_key_in_range():
    with pytest.raises(ValidationError):
        CheckItem(cell="mean.procedure", stem="?", options=("a", "b", "c"), answer=3)


def test_check_section_carries_item():
    item = CheckItem(cell="mean.procedure", stem="?", options=("a", "b", "c"), answer=0)
    OutlineSection(id="s1", heading="h", role=SectionRole.CHECK, check=item)
    with pytest.raises(ValidationError):
        OutlineSection(id="s1", heading="h", role=SectionRole.CHECK)
    with pytest.raises(ValidationError):
        OutlineSection(id="s1", heading="h", role=SectionRole.LINE, check=item)


def test_outline_unique_sections_and_targets():
    s = OutlineSection(id="s1", heading="h", role=SectionRole.LINE)
    with pytest.raises(ValidationError):
        PageOutline(page_id="p", goal="g", concepts=("a",), targets=("a",), sections=(s, s))
    with pytest.raises(ValidationError):
        PageOutline(page_id="p", goal="g", concepts=("a",), targets=("b",), sections=(s,))
