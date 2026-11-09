"""`build_page_readers` / `DurableRuntime._close_readers`: one LayoutWorker serves as the local
reader and the layout parser, and is closed once. No worker process is started."""

from types import SimpleNamespace

from evowiki.service.deps import DurableRuntime, build_page_readers
from evowiki.sources.layout_client import LayoutWorker
from evowiki.sources.vision import NoVision


def _build(env):
    return build_page_readers(env, pool=None, meter=None, prices=None, providers={},
                              clock=lambda: None)


def test_no_layout_python_means_no_local_reader_and_no_layout():
    local, claude, layout = _build({})
    assert isinstance(local, NoVision) and isinstance(claude, NoVision) and layout is None


def test_one_layout_worker_is_local_and_layout(tmp_path):
    python = tmp_path / "python.exe"
    python.touch()
    local, claude, layout = _build({"EVOWIKI_LAYOUT_PYTHON": str(python),
                                    "EVOWIKI_LAYOUT_BACKEND": "docling"})
    assert isinstance(local, LayoutWorker) and layout is local
    assert local.backend == "docling" and local.available()
    assert isinstance(claude, NoVision)


def test_the_shared_worker_is_closed_once():
    closed: list[str] = []
    worker = SimpleNamespace(name="local", close=lambda: closed.append("worker"))
    claude = SimpleNamespace(name="claude", close=lambda: closed.append("claude"))
    rt = DurableRuntime("postgresql://unused", "test")
    rt.sources = SimpleNamespace(local=worker, claude=claude, layout=worker)
    rt._close_readers()
    assert closed == ["worker", "claude"]
