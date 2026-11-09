"""Loaders on hand-written 5-row fixtures: only the column layout matches each dataset; every
value is invented (no row of any real file)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evowiki.belief.aspects import DefineConcept, Observe
from evowiki.teach.contracts import InferParams
from evowiki.teach.fit.public import (
    ASSISTMENTS_STEP,
    EPOCH,
    LoadStats,
    load_anki_revlogs,
    load_assistments_2009,
    load_ednet_kt1,
    load_eedi_2020,
    load_junyi,
    to_aspect_log,
)
from evowiki.teach.predict import predict_outcomes
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1

DATA = Path(__file__).with_name("data")


def _shape(log):
    return {s: [(sk, y) for _, sk, y in rows] for s, rows in log.items()}


def _sorted(log):
    return all([r[0] for r in rows] == sorted(r[0] for r in rows) for rows in log.values())


def test_assistments_orders_by_order_id_and_skips_missing_skill():
    stats = LoadStats()
    log = load_assistments_2009(DATA / "assistments_2009.csv", stats)
    assert _shape(log) == {"7": [("10", 0), ("11_12", 1), ("10", 1)], "8": [("10", 0)]}
    assert [r[0] for r in log["7"]] == [EPOCH + i * ASSISTMENTS_STEP for i in range(3)]
    assert stats.rows == 5 and stats.skipped == {"skill": 1}


def test_junyi_2015_and_2019_layouts():
    stats = LoadStats()
    log = load_junyi(DATA / "junyi_2015.csv", stats)
    assert _shape(log) == {"1": [("addition_1", 0), ("addition_1", 1), ("subtraction_1", 1)],
                           "2": [("addition_1", 1)]}
    assert stats.skipped == {"outcome": 1}
    assert log["1"][0][0].tzinfo is not None and _sorted(log)
    log = load_junyi(DATA / "junyi_2019.csv")
    assert _shape(log) == {"u1": [("c2", 0), ("c1", 1), ("c1", 1)], "u2": [("c1", 0), ("c2", 1)]}
    assert log["u1"][0][0] == datetime(2018, 9, 28, 17, tzinfo=UTC)


def test_ednet_kt1_first_tag_and_answer_key():
    stats = LoadStats()
    log = load_ednet_kt1(DATA / "ednet", max_students=10, stats=stats)
    assert _shape(log) == {"u1": [("5", 1), ("8", 0), ("5", 0)], "u2": [("8", 1)]}
    assert stats.skipped == {"skill": 1}
    assert list(load_ednet_kt1(DATA / "ednet", max_students=1)) == ["u1"]


def test_eedi_joins_time_and_most_specific_subject():
    stats = LoadStats()
    log = load_eedi_2020(DATA / "eedi", stats)
    assert _shape(log) == {"10": [("4002", 0), ("4001", 1), ("4001", 1)], "11": [("4002", 1)]}
    assert stats.skipped == {"skill": 1}
    assert log["10"][0][0] == datetime(2021, 3, 5, 10, 10, tzinfo=UTC)


def test_anki_revlog_file_and_tiny_directory():
    stats = LoadStats()
    log = load_anki_revlogs(DATA / "anki_revlog.csv", max_users=5, stats=stats)
    assert list(log) == ["anki_revlog"]
    assert [(c, g) for _, c, g in log["anki_revlog"]] == [
        ("11", 4), ("22", 1), ("11", 3), ("22", 2)]
    assert stats.skipped == {"rating": 1}
    tiny = load_anki_revlogs(DATA / "anki_tiny", max_users=1)
    assert list(tiny) == ["1"]
    rows = tiny["1"]
    card0 = [ts for ts, c, _ in rows if c == "5"]
    assert card0[1] - card0[0] == timedelta(days=0, minutes=1)
    assert card0[2] - card0[1] == timedelta(days=4, minutes=1)
    assert sorted(g for _, _, g in rows) == [1, 2, 3, 3, 4] and _sorted(tiny)


def test_bad_layout_fails_closed(tmp_path):
    bad = tmp_path / "x.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_assistments_2009(bad)
    with pytest.raises(ValueError):
        load_junyi(bad)
    with pytest.raises(ValueError):
        load_anki_revlogs(bad, max_users=1)


def test_to_aspect_log_replays_through_predict_outcomes():
    log = load_assistments_2009(DATA / "assistments_2009.csv")
    events = to_aspect_log(log["7"], concept_prefix="as")
    defs = [e for e in events if isinstance(e, DefineConcept)]
    obs = [e for e in events if isinstance(e, Observe)]
    assert [d.concept_id for d in defs] == ["as0", "as1"]
    assert [o.observation.polarity for o in obs] == ["-", "+", "+"]
    assert {o.observation.kind for o in obs} == {"quiz"}
    outs = predict_outcomes(events, ASPECT_CONFIG_V1, InferParams(), {})
    assert [o.y for o in outs] == [0.0, 1.0, 1.0]
    assert all(0.0 < o.p < 1.0 for o in outs)


def test_to_aspect_log_anki_ratings_as_recalls():
    log = load_anki_revlogs(DATA / "anki_revlog.csv", max_users=1)
    events = to_aspect_log(log["anki_revlog"], concept_prefix="ak", kind="recall")
    obs = [e for e in events if isinstance(e, Observe)]
    assert [o.observation.polarity for o in obs] == ["+", "-", "+", "~"]
    outs = predict_outcomes(events, ASPECT_CONFIG_V1, InferParams(), {})
    assert [o.y for o in outs] == [1.0, 0.0, 1.0, 0.5]
    assert to_aspect_log([], concept_prefix="x") == []
    with pytest.raises(ValueError):
        to_aspect_log(log["anki_revlog"], concept_prefix="bad.prefix")
    with pytest.raises(ValueError):
        to_aspect_log([(EPOCH, "s", 2)], concept_prefix="q")
