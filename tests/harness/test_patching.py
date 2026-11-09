"""The service's code-patch hooks (harness 3b, Task 10): built only with an L2 sandbox and the forge's
test database (else None: the improver is never offered `propose_patch`); the PatchDeps they build
carry an accounted converse on the given gateway and the round's pack."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from evowiki.forge.binding import accounted
from evowiki.forge.patch import testdb
from evowiki.forge.sandbox import SandboxUnavailable
from evowiki.harness import patching
from evowiki.harness.pack import HarnessPack
from evowiki.llm.usage import Account

ACC = Account(user_id="u1", group="rsi")


class _Runner:
    def __init__(self, *a, **kw):
        pass


@pytest.fixture
def jobs(monkeypatch):
    import evowiki.forge.sandbox.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "JobRunner", _Runner)
    return jobs_mod


def test_no_forge_test_database_means_no_hooks(monkeypatch, jobs, tmp_path):
    monkeypatch.delenv(testdb.ENV, raising=False)
    (tmp_path / ".git").write_text("gitdir: x", encoding="utf-8")
    assert patching.build_hooks(object(), object(), main_root=tmp_path) is None


def test_no_sandbox_means_no_hooks(monkeypatch, tmp_path):
    import evowiki.forge.sandbox.jobs as jobs_mod

    def unavailable(*a, **kw):
        raise SandboxUnavailable("pywin32 missing")
    monkeypatch.setattr(jobs_mod, "JobRunner", unavailable)
    monkeypatch.setenv(testdb.ENV, "postgresql://postgres@127.0.0.1:5999/x")
    assert patching.build_hooks(object(), object(), main_root=tmp_path) is None


def test_not_a_git_checkout_means_no_hooks(monkeypatch, jobs, tmp_path):
    monkeypatch.setenv(testdb.ENV, "postgresql://postgres@127.0.0.1:5999/x")
    assert patching.build_hooks(object(), object(), main_root=tmp_path) is None


def test_hooks_build_accounted_patch_deps_with_the_pack(monkeypatch, jobs, tmp_path):
    pytest.importorskip("jsonschema")            # forge.api's build step needs it
    monkeypatch.setenv(testdb.ENV, "postgresql://postgres@127.0.0.1:5999/x")
    (tmp_path / ".git").write_text("gitdir: x", encoding="utf-8")
    pool, trace = object(), object()
    hooks = patching.build_hooks(pool, trace, main_root=tmp_path)
    assert hooks is not None and isinstance(hooks.runner, _Runner)
    assert hooks.main_root == tmp_path and hooks.propose is None and hooks.encode is None
    gw = SimpleNamespace(name="recording")
    pack = HarnessPack(4, {"H4.forge.patch.max_tool_calls": 30})
    d = hooks.deps_for(gw, ACC, pack)
    assert d.pool is pool and d.trace is trace and d.main_root == tmp_path and d.pack is pack
    assert accounted(d.converse) and d.converse.gateway is gw and d.converse.account == ACC
    assert d.test_db_url() == "postgresql://postgres@127.0.0.1:5999/x"
    assert callable(hooks.child_for(gw, ACC))


def test_default_main_root_is_the_checkout():
    root = patching.default_main_root()
    assert (root / "src" / "evowiki" / "harness" / "patching.py").exists()
    assert isinstance(root, Path)
