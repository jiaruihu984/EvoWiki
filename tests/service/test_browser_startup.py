import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from evowiki.service import cli


def browser(monkeypatch, tmp_path, *, installed=False, launches=True):
    executable = tmp_path / "chromium"
    if installed:
        executable.touch()
    calls = []

    def launch(**kw):
        calls.append("launch")
        if not launches:
            raise RuntimeError("missing DLL")
        return SimpleNamespace(close=lambda: calls.append("close"))

    @contextmanager
    def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(executable_path=str(executable), launch=launch))

    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=playwright))
    return calls


@pytest.mark.parametrize("executable_exists", [False, True])
def test_official_installer_checks_whole_bundle_before_launch(monkeypatch, tmp_path, executable_exists):
    calls = browser(monkeypatch, tmp_path, installed=executable_exists)
    commands = []
    monkeypatch.setattr(cli.subprocess, "run", lambda args, **kw: commands.append(args) or
                        SimpleNamespace(returncode=0))
    cli._ensure_browser()
    assert commands == [[sys.executable, "-m", "playwright", "install", "chromium"]]
    assert calls == ["launch", "close"]


def test_failed_download_stops_before_browser_work(monkeypatch, tmp_path):
    calls = browser(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="安装失败"):
        cli._ensure_browser()
    assert calls == []


def test_broken_browser_stops_startup(monkeypatch, tmp_path):
    browser(monkeypatch, tmp_path, installed=True, launches=False)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError, match="Chromium 无法启动"):
        cli._ensure_browser()


def test_interrupted_bundle_with_chromium_present_can_finish_installing(monkeypatch, tmp_path):
    calls = browser(monkeypatch, tmp_path, installed=True)
    ready = False

    def install(*args, **kwargs):
        nonlocal ready
        ready = True
        return SimpleNamespace(returncode=0)

    @contextmanager
    def playwright():
        def launch(**kwargs):
            assert ready, "Chromium exists, but its headless shell has not finished installing"
            calls.append("launch")
            return SimpleNamespace(close=lambda: calls.append("close"))
        yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    monkeypatch.setattr(cli.subprocess, "run", install)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=playwright))
    cli._ensure_browser()
    assert calls == ["launch", "close"]
