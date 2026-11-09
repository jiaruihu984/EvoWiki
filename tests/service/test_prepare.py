from types import SimpleNamespace

import pytest

from evowiki.deep import library_seed, store
from evowiki.service.cli import _initialize_library


def seed(cid, html="h", schema=None):
    return SimpleNamespace(component_id=cid, html=html, config_schema=schema)


def test_prepare_registers_only_missing_components(monkeypatch):
    old, new = seed("old"), seed("new")
    lib = SimpleNamespace(list=lambda: [old])
    monkeypatch.setattr(store, "PgLibrary", lambda _: lib)
    monkeypatch.setattr(library_seed, "load_seed", lambda: [old, new])
    monkeypatch.setattr(library_seed, "seed_html", lambda c: c.html)
    calls = []

    def register(target, checker, missing):
        calls.append((target, missing))
        lib.list = lambda: [old, new]
        return [SimpleNamespace(registered=True)]

    monkeypatch.setattr(library_seed, "register_seed", register)
    _initialize_library(object())
    _initialize_library(object())
    assert calls == [(lib, [new])]


@pytest.mark.parametrize("changed", [seed("c", html="new code"), seed("c", schema={"type": "object"})])
def test_prepare_registers_a_seed_whose_code_or_schema_changed_again(monkeypatch, changed):
    lib = SimpleNamespace(list=lambda: [seed("c")])
    monkeypatch.setattr(store, "PgLibrary", lambda _: lib)
    monkeypatch.setattr(library_seed, "load_seed", lambda: [changed])
    monkeypatch.setattr(library_seed, "seed_html", lambda c: c.html)
    calls = []
    monkeypatch.setattr(library_seed, "register_seed", lambda target, checker, missing: (
        calls.append(missing), [SimpleNamespace(registered=True)])[1])
    _initialize_library(object())
    assert calls == [[changed]]


def test_prepare_does_not_hide_component_validation_failures(monkeypatch):
    monkeypatch.setattr(store, "PgLibrary", lambda _: SimpleNamespace(list=list))
    monkeypatch.setattr(library_seed, "load_seed", lambda: [seed("broken")])
    monkeypatch.setattr(library_seed, "register_seed", lambda *a: [
        SimpleNamespace(registered=False, component_id="broken", error="invalid render")])
    with pytest.raises(RuntimeError, match="broken: invalid render"):
        _initialize_library(object())
