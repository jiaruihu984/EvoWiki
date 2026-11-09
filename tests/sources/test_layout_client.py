"""LayoutWorker against a fake worker (tests/sources/fake_layout_worker.py: no MinerU, no
Docling) + needs_eyes + judge_page."""

import json
import logging
import sys
import time
from pathlib import Path

import pytest

from evowiki.sources import layout_client, layout_worker
from evowiki.sources.layout_client import ENV_BACKEND, ENV_PYTHON, LayoutWorker, judge_page
from evowiki.sources.vision import NoVision, PageImageRead, needs_eyes

FAKE = str(Path(__file__).with_name("fake_layout_worker.py"))
LAYOUT = Path(__file__).with_name("data") / "layout"
TEXT = "线性回归用来建模自变量与因变量之间的线性关系，目标是让误差的平方和最小。"
OK = PageImageRead(TEXT, False)


@pytest.fixture(autouse=True)
def _fake_worker_reads_its_settings(monkeypatch):
    monkeypatch.setattr(layout_client, "_WORKER_ENV_PREFIXES", (*layout_client._WORKER_ENV_PREFIXES, "FAKE_LAYOUT_"))


def unread(reason: str) -> PageImageRead:
    return PageImageRead(None, False, reason)


def varied(n: int) -> str:
    """n chars of text with no short period."""
    return "".join(chr(0x4E00 + (i * 7919) % 20000) for i in range(n))


def page(markdown=TEXT, blocks=(), do="ok") -> bytes:
    return json.dumps({"markdown": markdown, "blocks": list(blocks), "do": do}).encode()


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "starts.log"
    monkeypatch.setenv("FAKE_LAYOUT_LOG", str(path))
    monkeypatch.delenv("FAKE_LAYOUT_START", raising=False)
    monkeypatch.delenv("FAKE_LAYOUT_PDF", raising=False)
    monkeypatch.delenv(ENV_BACKEND, raising=False)
    monkeypatch.setenv("FAKE_LAYOUT_RAW", str(LAYOUT / "mineru_resnet_p1-3.json"))
    return path


def starts(path: Path) -> int:
    return path.read_text(encoding="utf-8").splitlines().count("start") if path.exists() else 0


def worker_requests(path: Path) -> list[tuple[int, int]]:
    """The (first, last) page ranges the fake worker was asked for, in order."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [(int(a), int(b)) for _, a, b in (x.split() for x in lines if x.startswith("pdf "))]


@pytest.fixture
def ocr():
    made: list[LayoutWorker] = []

    def make(**kw):
        o = LayoutWorker(python=sys.executable, worker=FAKE, **kw)
        made.append(o)
        return o
    yield make
    for o in made:
        o.close()


# -- needs_eyes (Q4) --

def test_needs_eyes_chart_area_boundary():
    assert not needs_eyes(TEXT, [{"label": "chart", "area": 0.14}])
    assert needs_eyes(TEXT, [{"label": "chart", "area": 0.15}])
    assert needs_eyes(TEXT, [{"label": "image", "area": 0.5}])
    assert needs_eyes(TEXT, [{"label": "figure", "area": 0.2}])
    assert not needs_eyes(TEXT, [{"label": "text", "area": 0.9}])


def test_needs_eyes_text_length_boundary():
    assert needs_eyes("一" * 19, [])
    assert not needs_eyes("一" * 20, [])
    assert needs_eyes("  一 二 \n" + "三" * 16, [])               # whitespace does not count
    assert needs_eyes('<div><img src="imgs/a.jpg" /></div>' + "一" * 5, [])   # nor figure markup
    assert not needs_eyes("$x<y$ 且 $y>z$ 所以 $x<z$ 成立", [])             # formula text counts


def test_no_vision():
    nv = NoVision()
    assert nv.name == "none" and not nv.available()
    assert nv.read([b"a", b"b"]) == [unread("no_reader")] * 2


# -- judge_page (degenerate output) --

def test_judge_page():
    assert judge_page(TEXT, []) is None
    assert judge_page(varied(5000), [{"label": "text", "area": 0.1}]) is None
    assert judge_page(TEXT * 5 + "重复内容" * 49 + "</td></tr></table>", []) == "repeat"
    assert judge_page(TEXT + "重复内容" * 5, []) == "repeat"              # short prefix + 5 times
    assert judge_page(TEXT + "重复内容" * 4, []) is None
    assert judge_page(TEXT + "重复内容重复内容", []) is None              # a 2-repeat is normal text
    assert judge_page(TEXT + "哈哈", []) is None
    assert judge_page(TEXT + "重复内容" * 60, []) == "repeat"            # 4-gram loop
    assert judge_page(TEXT + "重复内容" * 60 + "\n\n", []) == "repeat"  # trailing space ignored
    assert judge_page("abcdefghijklmnopqrst" * 5, []) == "repeat"       # n=20, 5 times
    assert judge_page("abcdefghijklmnopqrst" * 4, []) is None           # 4 times: not yet
    assert judge_page("abcdefghijklmnopqrstu" * 12, []) is None         # n=21: out of range
    assert judge_page(varied(20001), [{"label": "text", "area": 0.9}]) == "too_long"
    assert judge_page(varied(20000), [{"label": "text", "area": 0.9}]) is None
    assert judge_page(varied(12001), [{"label": "text", "area": 0.29}]) == "too_long"
    assert judge_page(varied(12001), [{"label": "text", "area": 0.2},
                                      {"label": "image", "area": 0.1}]) is None   # 30% cover
    assert judge_page(varied(12000), [{"label": "text", "area": 0.1}]) is None


# -- LayoutWorker --

def test_reads_pages_with_figure_judge(ocr, log):
    o = ocr()
    assert o.name == "local" and o.available()
    out = o.read([page(), page("图 1", [{"label": "chart", "area": 0.42}])])
    assert out == [OK, PageImageRead("图 1", True)]
    assert o.read([page()]) == [OK]
    assert o.read([]) == []
    assert starts(log) == 1                                 # one long-lived worker


def test_not_configured(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV_PYTHON, raising=False)
    assert not LayoutWorker(worker=FAKE).available()
    assert LayoutWorker(worker=FAKE).read([page()]) == [unread("unavailable")]
    monkeypatch.setenv(ENV_PYTHON, str(tmp_path / "nope.exe"))
    assert not LayoutWorker(worker=FAKE).available()
    monkeypatch.setenv(ENV_PYTHON, sys.executable)
    assert LayoutWorker(worker=FAKE).available()


def test_flaky_page_restarts_worker_and_retries(ocr, log):
    text = "重试后读出来的一页文字，足够长不需要看图。"
    out = ocr().read([page(), page(text, do="flaky"), page()])
    assert out == [OK, PageImageRead(text, False), OK]
    assert starts(log) == 2


def test_page_that_always_fails_is_none_others_read(ocr, log):
    out = ocr().read([page(do="fail"), page()])
    assert out == [unread("error:OUT_OF_RANGE: piece id is out of range"), OK]
    assert starts(log) == 2


def test_unparseable_request_and_bad_reply_fail_fast(ocr, log, monkeypatch):
    monkeypatch.setattr(layout_client, "PAGE_TIMEOUT_S", 5.0)
    t0 = time.monotonic()
    out = ocr().read([page(do="id_none"), page(do="bad_reply"), page()])
    assert time.monotonic() - t0 < 5                        # no wait for a reply that never comes
    assert out == [unread("error:JSONDecodeError: bad request"), unread("bad_reply"), OK]
    assert starts(log) == 3                                 # each retried once in a fresh worker


def test_repeating_tail_is_none_after_retry(ocr, log):
    out = ocr().read([page(TEXT + "重复内容" * 60), page()])
    assert out == [unread("repeat"), OK]
    assert starts(log) == 2                                 # retried in a fresh worker


def test_too_long_page_is_none(ocr, log):
    out = ocr().read([page(varied(20001), [{"label": "text", "area": 0.9}]),
                      page(varied(12001), [{"label": "text", "area": 0.1}]), page()])
    assert out == [unread("too_long"), unread("too_long"), OK]


def test_start_failure_marks_unavailable_and_reads_none(ocr, log, monkeypatch):
    monkeypatch.setenv("FAKE_LAYOUT_START", "fail")
    t = [1000.0]
    o = ocr(clock=lambda: t[0])
    assert o.available()
    assert o.read([page(), page()]) == [unread("unavailable")] * 2
    assert not o.available()
    assert o.read([page()]) == [unread("unavailable")]
    assert starts(log) == 1                                 # no retry while marked failed
    t[0] += 599
    assert not o.available()
    t[0] += 2                                               # 10 minutes later: try again
    monkeypatch.setenv("FAKE_LAYOUT_START", "ok")
    assert o.available()
    assert o.read([page()]) == [OK]


def test_smoke_failure_marks_unavailable(ocr, log, monkeypatch):
    monkeypatch.setenv("FAKE_LAYOUT_START", "smoke_fail")
    o = ocr()
    assert o.read([page()]) == [unread("unavailable")]
    assert not o.available()


def test_start_hang_times_out(ocr, log, monkeypatch):
    monkeypatch.setenv("FAKE_LAYOUT_START", "hang")
    monkeypatch.setattr(layout_client, "READY_TIMEOUT_S", 1.0)
    o = ocr()
    t0 = time.monotonic()
    assert o.read([page()]) == [unread("unavailable")]
    assert time.monotonic() - t0 < 10
    assert not o.available()


def test_hung_page_is_killed_rest_none(ocr, log, monkeypatch):
    monkeypatch.setattr(layout_client, "PAGE_TIMEOUT_S", 1.0)
    o = ocr()
    t0 = time.monotonic()
    out = o.read([page(), page(do="hang"), page()])
    assert time.monotonic() - t0 < 10
    assert out == [OK, unread("timeout"), unread("timeout")]
    assert o._proc is None                                  # killed
    assert o.available()                                    # a hung page is not a start failure
    assert o.read([page()]) == [OK]
    assert starts(log) == 2


def test_crashed_worker_rest_none(ocr, log):
    o = ocr()
    assert o.read([page(do="crash"), page()]) == [unread("crashed")] * 2
    assert o._proc is None
    assert o.read([page()]) == [OK]


def test_idle_worker_exits(ocr, log, monkeypatch):
    monkeypatch.setattr(layout_client, "IDLE_EXIT_S", 0.3)
    o = ocr()
    assert o.read([page()]) == [OK]
    proc = o._proc
    assert proc is not None
    deadline = time.monotonic() + 10
    while o._proc is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert o._proc is None and proc.poll() is not None
    assert o.read([page()]) == [OK]                         # starts again on demand
    assert starts(log) == 2


# -- parse_pdf --

def test_backend_from_env(monkeypatch):
    monkeypatch.delenv(ENV_BACKEND, raising=False)
    assert LayoutWorker(python=None).backend == "mineru"
    monkeypatch.setenv(ENV_BACKEND, "docling")
    assert LayoutWorker(python=None).backend == "docling"
    assert LayoutWorker(python=None, backend="mineru").backend == "mineru"
    with pytest.raises(ValueError):
        LayoutWorker(python=None, backend="paddle")


def test_parse_pdf_batches_and_adapts(ocr, log):
    done: list[tuple[int, int]] = []
    o = ocr()
    doc = o.parse_pdf(b"%PDF-fake", 20, on_batch=lambda d, t: done.append((d, t)))
    assert doc is not None and doc.backend == "mineru" and doc.version == "4.0.12"
    assert {p.page for p in doc.pages} == {1, 2, 3, 17, 18, 19}   # page_idx is absolute
    assert min(b.page for b in doc.blocks) == 1 and max(b.page for b in doc.blocks) == 19
    assert worker_requests(log) == [(1, 16), (17, 20)]
    assert done == [(0, 20), (16, 20), (20, 20)]    # 0: the worker is up
    assert starts(log) == 1
    assert o.read([page()]) == [OK]                          # the same worker reads images
    assert starts(log) == 1


def test_parse_pdf_stops_between_batches_when_told(ocr, log, caplog):
    asked: list[int] = []

    def stop() -> bool:
        asked.append(1)
        return True
    o = ocr()
    with caplog.at_level(logging.WARNING, logger="evowiki.sources.layout_client"):
        assert o.parse_pdf(b"%PDF-fake", 20, should_stop=stop) is None
    assert worker_requests(log) == [(1, 16)]                 # the second batch never ran
    assert asked == [1]
    assert "stopped after 16 of 20 pages" in caplog.text
    assert o.read([page()]) == [OK]                          # the worker is still fine
    assert starts(log) == 1


def test_parse_pdf_goes_on_when_the_stop_check_fails(ocr, log):
    def stop() -> bool:
        raise RuntimeError("db down")
    assert ocr().parse_pdf(b"%PDF-fake", 20, should_stop=stop) is not None
    assert worker_requests(log) == [(1, 16), (17, 20)]


def test_parse_pdf_docling(ocr, log, monkeypatch):
    monkeypatch.setenv("FAKE_LAYOUT_RAW", str(LAYOUT / "docling_resnet_p1-3.json"))
    doc = ocr(backend="docling").parse_pdf(b"%PDF-fake", 3)
    assert doc is not None and doc.backend == "docling"
    assert [p.page for p in doc.pages] == [1, 2, 3]
    assert worker_requests(log) == [(1, 3)]


def test_parse_pdf_passes_the_file(ocr, log, monkeypatch, tmp_path):
    seen = {}
    o = ocr()
    real = o._ask

    def spy(req, timeout):
        if req.get("op") == "pdf":
            seen["data"] = Path(req["path"]).read_bytes()
            seen["path"] = req["path"]
            seen["timeout"] = timeout
        return real(req, timeout)
    monkeypatch.setattr(o, "_ask", spy)
    assert o.parse_pdf(b"%PDF-1.7 bytes", 3) is not None
    assert seen["data"] == b"%PDF-1.7 bytes"
    assert seen["timeout"] == layout_client.BATCH_TIMEOUT_BASE_S + \
        layout_client.BATCH_TIMEOUT_PER_PAGE_S * 3
    assert not Path(seen["path"]).exists()                  # the temp file is removed


def test_parse_pdf_retries_a_failed_batch_once_in_a_fresh_worker(ocr, log, monkeypatch):
    monkeypatch.setenv("FAKE_LAYOUT_PDF", "fail_once")
    doc = ocr().parse_pdf(b"%PDF-fake", 20)
    assert doc is not None
    assert {p.page for p in doc.pages} == {1, 2, 3, 17, 18, 19}
    assert worker_requests(log) == [(1, 16), (1, 16), (17, 20)]
    assert starts(log) == 2


def test_parse_pdf_returns_none_and_logs_when_a_batch_keeps_failing(ocr, log, monkeypatch,
                                                                     caplog):
    monkeypatch.setenv("FAKE_LAYOUT_PDF", "fail")
    caplog.set_level(logging.WARNING, logger="evowiki.sources.layout_client")
    done: list[tuple[int, int]] = []
    assert ocr().parse_pdf(b"%PDF-fake", 20, on_batch=lambda d, t: done.append((d, t))) is None
    assert worker_requests(log) == [(1, 16), (1, 16)]       # later batches are not asked
    assert starts(log) == 2
    assert done == [(0, 20)]
    assert "pages 1-16" in caplog.text and "CUDA out of memory" in caplog.text


def test_parse_pdf_hung_batch_times_out_and_is_retried(ocr, log, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_LAYOUT_PDF", "hang")
    monkeypatch.setattr(layout_client, "BATCH_TIMEOUT_BASE_S", 0.5)
    monkeypatch.setattr(layout_client, "BATCH_TIMEOUT_PER_PAGE_S", 0.1)
    caplog.set_level(logging.WARNING, logger="evowiki.sources.layout_client")
    o = ocr()
    t0 = time.monotonic()
    assert o.parse_pdf(b"%PDF-fake", 3) is None
    assert time.monotonic() - t0 < 15
    assert worker_requests(log) == [(1, 3), (1, 3)]
    assert o._proc is None and o.available()                # a hang is not a start failure
    assert "timeout" in caplog.text


def test_parse_pdf_crash_is_retried(ocr, log, monkeypatch):
    monkeypatch.setenv("FAKE_LAYOUT_PDF", "crash")
    assert ocr().parse_pdf(b"%PDF-fake", 3) is None
    assert worker_requests(log) == [(1, 3), (1, 3)] and starts(log) == 2


def test_parse_pdf_start_failure_is_none(ocr, log, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_LAYOUT_START", "fail")
    caplog.set_level(logging.WARNING, logger="evowiki.sources.layout_client")
    o = ocr()
    assert o.parse_pdf(b"%PDF-fake", 3) is None
    assert not o.available() and starts(log) == 1
    assert o.parse_pdf(b"%PDF-fake", 3) is None             # no new start while marked failed
    assert starts(log) == 1
    assert "no GPU" in caplog.text


def test_parse_pdf_unavailable_without_python(monkeypatch, caplog):
    monkeypatch.delenv(ENV_PYTHON, raising=False)
    caplog.set_level(logging.WARNING, logger="evowiki.sources.layout_client")
    assert LayoutWorker(python=None).parse_pdf(b"x", 3) is None
    assert "unavailable" in caplog.text


def test_parse_pdf_bad_raw_counts_as_a_failure(ocr, log, monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('[5, "not an item"]', encoding="utf-8")
    monkeypatch.setenv("FAKE_LAYOUT_RAW", str(bad))
    assert ocr().parse_pdf(b"%PDF-fake", 3) is None
    assert worker_requests(log) == [(1, 3), (1, 3)]


# -- layout_worker's pure helpers (importable without MinerU / Docling) --

def test_worker_scrubs_embedded_images():
    raw = {"pictures": [{"image": {"uri": "data:image/png;base64,AAAA"}, "label": "picture"}],
           "items": [{"type": "image", "img_path": "data:...", "bbox": [1, 2, 3, 4]}]}
    assert layout_worker._scrub(raw) == {
        "pictures": [{"image": "", "label": "picture"}],
        "items": [{"type": "image", "img_path": "", "bbox": [1, 2, 3, 4]}]}


def test_worker_mineru_markdown():
    t = layout_worker._mineru_text
    assert t({"type": "text", "text": "Intro", "text_level": 1}) == "# Intro"
    assert t({"type": "text", "text": "body"}) == "body"
    assert t({"type": "equation", "text": "$$x^2$$"}) == "$$x^2$$"
    assert t({"type": "equation", "text": "x^2"}) == "$$x^2$$"
    assert t({"type": "table", "table_body": "<table></table>", "table_caption": ["T1"]}) == \
        "T1\n<table></table>"
    assert t({"type": "chart", "chart_caption": ["Fig 2"]}) == "Fig 2"
    assert t({"type": "list", "list_items": ["a", "b"]}) == "a\nb"


def test_the_worker_gets_the_system_and_the_parser_variables_never_a_key_or_a_database_url():
    env = layout_client.worker_env({
        "PATH": "p", "SystemRoot": "s", "TEMP": "t", "HF_HOME": "h", "CUDA_VISIBLE_DEVICES": "0", "MINERU_MODEL_SOURCE": "m",
        "ANTHROPIC_API_KEY": "x", "DEEPSEEK_API_KEY": "x", "TAVILY_API_KEY": "x", "HF_TOKEN": "x",
        "EVOWIKI_DATABASE_URL": "x", "EVOWIKI_ADMIN_DATABASE_URL": "x"})
    assert set(env) == {"PATH", "SystemRoot", "TEMP", "HF_HOME", "CUDA_VISIBLE_DEVICES", "MINERU_MODEL_SOURCE"}
