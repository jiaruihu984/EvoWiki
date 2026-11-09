"""Method registry of the layer-2 entries (A2 FitModel): implicit v1, append-only versions,
rollback appends a copy; the cycle's fit model comes from the registry."""

from datetime import UTC, datetime

import pytest

from evowiki.teach.contracts import DEFAULT_FIT_PATHS, FitModel, MethodVersion
from evowiki.teach.cycle import RsiCycle
from evowiki.teach.layer2 import method_registry as mr
from evowiki.teach.layer2.store import MemoryLayer2Store
from evowiki.teach.rsi_store import MemoryRsiStore
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1

T = datetime(2026, 10, 1, 9, tzinfo=UTC)
U = "u1"


def test_defaults():
    assert mr.DEFAULT_FIT_MODEL == FitModel(version=1, paths=DEFAULT_FIT_PATHS,
                                            defaults_id=mr.FIT_DEFAULTS_ID)


def test_implicit_v1_without_writing():
    s = MemoryLayer2Store()
    assert mr.current_model(s, U) == mr.DEFAULT_FIT_MODEL
    assert s.method_versions(U, "A2") == []


def test_a2_commit_and_rollback_on_an_empty_chain():
    s = MemoryLayer2Store()
    v = mr.rollback(s, U, "A2", 1, now=T)                         # rollback to the implicit v1
    assert v.version == 2 and v.fit_model == mr.DEFAULT_FIT_MODEL.model_copy(update={"version": 2})
    fm = FitModel(version=1, paths=("slip",), prior_strength=2.0)
    v3 = mr.commit(s, U, "A2", fit_model=fm, change_id="mc2", now=T)
    assert v3.version == 3 and mr.current_model(s, U) == fm.model_copy(update={"version": 3})
    with pytest.raises(ValueError, match="no version 9"):
        mr.rollback(s, U, "A2", 9, now=T)


def test_commit_shape_and_numbering_fail_closed():
    s = MemoryLayer2Store()
    with pytest.raises(ValueError, match="not the next"):
        s.add_method_version(U, MethodVersion(
            kind="A2", version=2, at=T,
            fit_model=mr.DEFAULT_FIT_MODEL.model_copy(update={"version": 2})))
    with pytest.raises(ValueError):                       # B2 retired (2026-10-29)
        MethodVersion.model_validate({"kind": "B2", "version": 1, "at": T,
                                      "fit_model": mr.DEFAULT_FIT_MODEL.model_dump()})


def test_cycle_default_fit_model_comes_from_the_registry():
    layer2 = MemoryLayer2Store()
    cyc = RsiCycle(MemoryRsiStore(), ASPECT_CONFIG_V1, new_id=lambda p: f"{p}1", layer2=layer2)
    assert cyc.fit_model(U) == mr.DEFAULT_FIT_MODEL
    fm = FitModel(version=1, paths=("slip", "guess_scale"))
    mr.commit(layer2, U, "A2", fit_model=fm, change_id="mc1", now=T)
    assert cyc.fit_model(U) == fm.model_copy(update={"version": 2})
    pinned = RsiCycle(MemoryRsiStore(), ASPECT_CONFIG_V1, new_id=lambda p: p, layer2=layer2,
                      fit_model=lambda uid: fm)
    assert pinned.fit_model(U) == fm


def test_stored_legacy_v1_is_told_apart_from_the_implicit_default():
    """A v1 stored before `defaults_id` existed (6 paths) loads unchanged with defaults_id None;
    the implicit default (7 paths) carries the id; a rollback to the legacy v1 keeps its content."""
    legacy_paths = tuple(p for p in DEFAULT_FIT_PATHS if p != "start_known")
    legacy = FitModel.model_validate({"version": 1, "paths": list(legacy_paths)})
    assert legacy.defaults_id is None and mr.DEFAULT_FIT_MODEL.defaults_id == mr.FIT_DEFAULTS_ID
    s = MemoryLayer2Store()
    s.add_method_version(U, MethodVersion(kind="A2", version=1, at=T, fit_model=legacy))
    assert mr.current_model(s, U) == legacy                        # stored row keeps what it stored
    mr.commit(s, U, "A2", fit_model=mr.DEFAULT_FIT_MODEL, change_id="c", now=T)
    v = mr.rollback(s, U, "A2", 1, now=T)
    assert v.fit_model.paths == legacy_paths and v.fit_model.defaults_id is None
    assert mr.current_model(MemoryLayer2Store(), U).defaults_id == mr.FIT_DEFAULTS_ID
