"""pdfium is not thread-safe: every public parse_pdf entry point holds one process-wide lock."""

import io
import threading
import time

import pypdfium2 as pdfium

from evowiki.sources import parse_pdf as pp


def _pdf(n: int = 2) -> bytes:
    doc = pdfium.PdfDocument.new()
    for _ in range(n):
        doc.new_page(200, 200)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def test_concurrent_pdfium_calls_are_serialised(monkeypatch):
    data = _pdf()
    state = {"inside": 0, "max": 0}
    guard = threading.Lock()
    real_open = pp._open

    def spy_open(d):
        with guard:
            state["inside"] += 1
            state["max"] = max(state["max"], state["inside"])
        time.sleep(0.05)                      # widen the window an unlocked call would overlap in
        try:
            return real_open(d)
        finally:
            with guard:
                state["inside"] -= 1

    monkeypatch.setattr(pp, "_open", spy_open)
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(pp.parse_pdf(data, title_hint="t")))
               for _ in range(4)]
    threads += [threading.Thread(target=lambda: results.append(pp.render_pages(data, [1])))
                for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 6
    assert state["max"] == 1


def test_results_unchanged_and_lock_released_after_each_call():
    data = _pdf(3)
    doc = pp.parse_pdf(data, title_hint="论文")
    assert [p.page for p in doc.pages] == [1, 2, 3]
    pngs = pp.render_pages(data, [3, 1])
    assert len(pngs) == 2 and all(b.startswith(b"\x89PNG") for b in pngs)
    assert pp.pdf_title(b"not a pdf") is None
    assert pp._PDFIUM_LOCK.acquire(blocking=False)      # free again, errors included
    pp._PDFIUM_LOCK.release()
