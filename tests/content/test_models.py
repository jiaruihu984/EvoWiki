from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from evowiki.content.models import Concept, Edge, PrereqKind, QuizItem, Relation
from evowiki.timeutil import days_between


def test_days_between_returns_float_days():
    a = datetime(2026, 1, 1, tzinfo=UTC)
    assert days_between(a, a + timedelta(hours=36)) == pytest.approx(1.5)


def test_days_between_rejects_naive():
    with pytest.raises(ValueError):
        days_between(datetime(2026, 1, 1), datetime(2026, 1, 2, tzinfo=UTC))  # noqa: DTZ001


def test_prerequisite_edge_requires_kind():
    with pytest.raises(ValidationError):
        Edge(src="a", dst="b", relation=Relation.PREREQUISITE_OF, kind=None, strength=0.9)


def test_non_prerequisite_edge_forbids_kind():
    with pytest.raises(ValidationError):
        Edge(src="a", dst="b", relation=Relation.RELATED_TO, kind=PrereqKind.FORMAL, strength=0.5)


def test_concept_difficulty_bounds():
    with pytest.raises(ValidationError):
        Concept(id="x", name_en="X", name_zh="X", domain="la", difficulty=6)


def test_quiz_item_needs_a_concept():
    with pytest.raises(ValidationError):
        QuizItem(id="q", concept_ids=[], difficulty=2, kind="recall")
