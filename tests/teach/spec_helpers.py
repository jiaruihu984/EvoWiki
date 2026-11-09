"""Shared builders for teaching-spec tests (localization, spec ops): a teaching spec whose item s2 is
the culprit of 「不像人话 / 术语没先讲」, signals and section logs."""

from datetime import UTC, datetime, timedelta
from itertools import count

from evowiki.teach.contracts import SectionLog, Signal, SpecItem, TeachingSpec

T0 = datetime(2026, 10, 1, 9, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


ITEMS = (
    SpecItem(item_id="s1", text="先给结论，再讲直觉。", origin="default", authority="rsi"),
    # an RSI-adjustable item (rule provenance contract: an imported rule is the user's, RSI never
    # changes it; a default one is RSI's to tune)
    SpecItem(item_id="s2", text="用简练的书面语，术语直接使用，不必解释。", origin="default", authority="rsi"),
    SpecItem(item_id="s3", text="每段都给一个具体的数值例子。", origin="default", authority="rsi"),
    SpecItem(item_id="s4", text="篇幅控制在 300 字以内。", origin="default", authority="rsi"),
)


def spec(*items: SpecItem, version: int = 1, exemplars=()) -> TeachingSpec:
    return TeachingSpec(version=version, items=items or ITEMS, exemplars=exemplars)


_sig = count(1)


def sig(page: str, section: str | None, *, kind="page_feedback", chips=(), text="",
        minute=60, version=1, outcome=None) -> Signal:
    return Signal(signal_id=f"g{next(_sig)}", kind=kind, at=at(minute), page_id=page,
                  section_id=section, chips=chips, text=text, outcome=outcome,
                  spec_version=version)


def log(page: str, section: str, *, items=("s1", "s2", "s3", "s4"), version=1, minute=0,
        role="explain", bands=None) -> SectionLog:
    cells = tuple(bands) if bands else (f"{page}.c:term",)
    return SectionLog(page_id=page, section_id=section, at=at(minute), producer="writer",
                      cells=cells, bands=bands or {cells[0]: "gap"}, role=role,
                      spec_items=items, spec_version=version, infer_version=1)


def unclear_feedback(page="p1", sections=("s-a", "s-b")) -> list[Signal]:
    """Five 「不像人话 / 术语没先讲」 complaints on two sections."""
    a, b = sections
    return [sig(page, a, chips=("unclear",), text="看不懂，像教科书"),
            sig(page, a, chips=("term_before_explained",), text="方差是什么没说"),
            sig(page, b, chips=("unclear", "term_before_explained")),
            sig(page, b, chips=("term_before_explained",), text="标准差突然就出现了"),
            sig(page, a, chips=("unclear",))]
