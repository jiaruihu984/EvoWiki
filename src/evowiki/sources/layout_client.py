"""The local layout worker (design §5 step 3, Q3/Q4): MinerU (default) or Docling in a child
process, free and offline. It reads page images (`read`, the importer's `local` VisionReader) and
parses whole PDFs into a `SourceDocument` (`parse_pdf`).

The parser lives in its own venv (Python 3.12, torch + `mineru[full]` or `docling`; see
`scripts/setup_layout_env.py`), so it runs as a long-lived child process
(`sources/layout_worker.py`, JSON lines on stdin/stdout): the models load once and then serve every
request. `EVOWIKI_LAYOUT_PYTHON` names that venv's python (unset = no local reader);
`EVOWIKI_LAYOUT_BACKEND` picks "mineru" (default) or "docling". The models take most of the card's
VRAM, so one lock serializes every request and the worker is stopped after IDLE_EXIT_S without
work. Each read / parse also holds the process's GPU (`sources.gpu.exclusive`, shared with a GPU
embedder): it waits while an embedding batch runs, and a GPU embedder stops the idle worker before
its batch (`release_gpu`; it starts again lazily).

Fail closed toward the import, which has a fallback: nothing here raises out of `read` or
`parse_pdf`. Every unread page carries a `reason`; a PDF that cannot be parsed is None, its reason
logged. A page fails when the worker answers `ok:false` or when `judge_page` rejects its text (a
degenerate decode loop: repeating tail, runaway length); it is retried once in a fresh worker,
then left None. A worker that crashes or does not answer a page within PAGE_TIMEOUT_S is killed and
the remaining pages are None. A PDF batch (BATCH_PAGES pages) that fails, times out or crashes the
worker is retried once in a fresh worker; a second failure makes the whole PDF None. A worker that
fails to start (its own start-up smoke read included) makes the reader unavailable for
RETRY_START_S. Every failure is logged."""

import base64
import json
import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from evowiki.sources import gpu
from evowiki.sources.document import SourceDocument
from evowiki.sources.layout_adapt import concat, from_docling, from_mineru
from evowiki.sources.vision import PageImageRead, has_figure_block, needs_eyes

log = logging.getLogger("evowiki.sources.layout_client")

ENV_PYTHON = "EVOWIKI_LAYOUT_PYTHON"     # e.g. C:/Users/<user>/evowiki-layout/Scripts/python.exe
ENV_BACKEND = "EVOWIKI_LAYOUT_BACKEND"   # "mineru" (default) | "docling"
# What the worker gets of the server's environment: never its API keys or database urls. An allowlist,
# as MCP's stdio transport does for its servers: the OS's own variables and the parser's.
_WORKER_ENV = frozenset({
    "APPDATA", "COMPUTERNAME", "HOME", "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH",
    "LOCALAPPDATA", "NO_PROXY", "NUMBER_OF_PROCESSORS", "OS", "PATH", "PATHEXT", "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE",
    "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USERNAME", "USERPROFILE", "WINDIR"})
_WORKER_ENV_PREFIXES = ("CUDA_", "DOCLING_", "HF_", "HTTP_PROXY", "HTTPS_PROXY", "KMP_", "MINERU_", "MKL_",
                        "MODELSCOPE_", "NVIDIA_", "OMP_", "PYTORCH_", "TORCH_", "TRANSFORMERS_")
_SECRET_NAME = ("KEY", "SECRET", "TOKEN", "PASSWORD")
BACKENDS = ("mineru", "docling")
READY_TIMEOUT_S = 300            # model load + smoke read (first run also downloads the weights)
PAGE_TIMEOUT_S = 120             # one page image
BATCH_PAGES = 16                 # PDF pages per worker request
BATCH_TIMEOUT_BASE_S = 120       # one PDF batch may take BASE + PER_PAGE * its pages
BATCH_TIMEOUT_PER_PAGE_S = 30
IDLE_EXIT_S = 300                # stop the worker after this long without work (frees the VRAM)
RETRY_START_S = 600              # after a failed start, do not try again for this long
STOP_WAIT_S = 10

# Degenerate output (decode loops).
MAX_PAGE_CHARS = 20000           # longer than any real page
REPEAT_TAIL_CHARS = 200          # the tail checked for a loop ...
REPEAT_MIN_N, REPEAT_MAX_N, REPEAT_MIN_TIMES = 2, 20, 5   # ... one n-gram repeated >= 5 times
SPARSE_PAGE_CHARS = 12000        # this much text from a page whose layout blocks cover ...
SPARSE_PAGE_COVER = 0.30         # ... less than this share of the image is out of proportion

_WORKER = str(Path(__file__).with_name("layout_worker.py"))
_TRAILING_MARKUP = re.compile(r"(?:\s|</[A-Za-z][^<>]*>)+\Z")   # closing tags / whitespace


def judge_page(markdown: str, blocks: list[dict]) -> str | None:
    """Why this page output is not a reading of the page (None: it is). Pure."""
    if len(markdown) > MAX_PAGE_CHARS:
        return "too_long"
    tail = _TRAILING_MARKUP.sub("", markdown)[-REPEAT_TAIL_CHARS:]
    for n in range(REPEAT_MIN_N, REPEAT_MAX_N + 1):
        k = len(tail) - 1                       # the periodic run (period n) ending at the end
        while k >= n and tail[k] == tail[k - n]:
            k -= 1
        if len(tail) - 1 - k + n >= n * REPEAT_MIN_TIMES:
            return "repeat"
    cover = sum(float(b.get("area") or 0) for b in blocks)
    if len(markdown) > SPARSE_PAGE_CHARS and cover < SPARSE_PAGE_COVER:
        return "too_long"
    return None


def batch_timeout(pages: int) -> float:
    return BATCH_TIMEOUT_BASE_S + BATCH_TIMEOUT_PER_PAGE_S * pages


class _Dead(Exception):
    """The worker is gone (failed start, crash, timeout): the remaining pages stay unread,
    each with `reason` ("unavailable" / "timeout" / "crashed")."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


class _BatchFailed(Exception):
    """A PDF batch got no usable reply twice (ok:false, bad raw, timeout, crash)."""


def worker_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The part of `environ` the worker may see (`_WORKER_ENV`)."""
    out = {}
    for name, value in environ.items():
        up = name.upper()
        if any(s in up for s in _SECRET_NAME):
            continue
        if up in _WORKER_ENV or up.startswith(_WORKER_ENV_PREFIXES):
            out[name] = value
    return out


class LayoutWorker:
    """VisionReader (and PDF parser) backed by a MinerU / Docling worker process."""

    name: Literal["local"] = "local"

    def __init__(self, python: str | None = None, worker: str | None = None,
                 clock: Callable[[], float] = time.monotonic, backend: str | None = None):
        self._python = python if python is not None else (os.environ.get(ENV_PYTHON) or None)
        self.backend = backend or os.environ.get(ENV_BACKEND) or "mineru"
        if self.backend not in BACKENDS:
            raise ValueError(f"{ENV_BACKEND} must be one of {BACKENDS}, not {self.backend!r}")
        self._worker = worker or _WORKER
        self._clock = clock
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._lines: queue.Queue[str | None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._failed_at: float | None = None
        self._next_id = 0
        self._version = ""                      # the parser's version, from the ready line
        self._idle: threading.Timer | None = None
        gpu.register(self)

    def available(self) -> bool:
        if not self._python or not Path(self._python).is_file():
            return False
        return self._failed_at is None or self._clock() - self._failed_at >= RETRY_START_S

    def loaded(self) -> bool:
        """The worker is up: a read now does not wait for its models to load."""
        return self._proc is not None and self._proc.poll() is None

    def read(self, png_pages: list[bytes]) -> list[PageImageRead]:
        out: list[PageImageRead] = []
        if not png_pages:
            return out
        with gpu.exclusive(self), self._lock:
            self._cancel_idle()
            try:
                for png in png_pages:
                    if not self.available():
                        raise _Dead("unavailable", "not configured or start failed recently")
                    out.append(self._read_page(png, len(out) + 1))
            except _Dead as exc:
                left = len(png_pages) - len(out)
                log.error("local layout (%s): %d of %d pages left unread", exc, left,
                          len(png_pages))
                out += [PageImageRead(None, False, exc.reason)] * left
            except Exception:
                log.exception("local layout: unexpected error; worker killed, %d of %d pages "
                              "left unread", len(png_pages) - len(out), len(png_pages))
                self._kill()
                out += [PageImageRead(None, False, "crashed")] * (len(png_pages) - len(out))
            finally:
                self._schedule_idle()
        return out

    def parse_pdf(self, data: bytes, pages: int, *,
                  on_batch: Callable[[int, int], None] | None = None,
                  should_stop: Callable[[], bool] | None = None) -> SourceDocument | None:
        """The PDF's layout as one document, parsed BATCH_PAGES pages per request (1-based
        inclusive ranges); None (logged) when the worker is unavailable or a batch fails twice.
        `on_batch(done_pages, pages)` once the worker is up (0 done: its start takes about two minutes
        on a cold process, 7.6 GB of VRAM, and says so) and after each batch. `should_stop()` before each batch after
        the first (e.g. the source was deleted): True ends the parse with None (logged); a
        check that raises is logged and the parse goes on."""
        if pages <= 0:
            log.warning("local layout: PDF not parsed: no pages")
            return None
        if not self.available():
            log.warning("local layout: PDF not parsed: unavailable (not configured or start "
                        "failed recently)")
            return None
        with gpu.exclusive(self), self._lock:
            self._cancel_idle()
            fd, path = tempfile.mkstemp(suffix=".pdf", prefix="evowiki-layout-")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                docs: list[SourceDocument] = []
                for first in range(1, pages + 1, BATCH_PAGES):
                    if first > 1 and _stopped(should_stop):
                        log.warning("local layout: PDF parse stopped after %d of %d pages",
                                    first - 1, pages)
                        return None
                    if first == 1:
                        self._ensure_started()
                        if on_batch is not None:
                            on_batch(0, pages)
                    last = min(first + BATCH_PAGES - 1, pages)
                    docs.append(self._parse_batch(path, first, last))
                    if on_batch is not None:
                        on_batch(last, pages)
                return concat(docs)
            except _Dead as exc:
                log.warning("local layout: PDF not parsed (%s)", exc)
                return None
            except _BatchFailed as exc:
                log.warning("local layout: PDF not parsed: %s", exc)
                return None
            except Exception:
                log.exception("local layout: PDF not parsed: unexpected error; worker killed")
                self._kill()
                return None
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
                self._schedule_idle()

    def close(self) -> None:
        with self._lock:
            self._cancel_idle()
            self._stop()

    def release_gpu(self) -> None:
        """Another GPU user (an embedding batch) holds the GPU: stop the worker to free its VRAM.
        No request runs (requests hold the GPU first); when close() or the idle exit holds our
        lock they are stopping it already."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._cancel_idle()
            if self._proc is not None:
                log.info("local layout worker stopped to free the GPU for embedding")
                self._stop()
        finally:
            self._lock.release()

    # -- one page --

    def _read_page(self, png: bytes, n: int) -> PageImageRead:
        reason = ""
        for attempt in (1, 2):
            self._ensure_started()
            reply = self._ask({"op": "image", "png_b64": base64.b64encode(png).decode("ascii")},
                              PAGE_TIMEOUT_S)
            md, blocks = reply.get("markdown"), reply.get("blocks")
            if reply.get("ok") is True and isinstance(md, str) and isinstance(blocks, list) \
                    and all(isinstance(b, dict) for b in blocks):
                bad = judge_page(md, blocks)
                if bad is None:
                    eyes = needs_eyes(md, blocks)
                    return PageImageRead(md, eyes, thin_only=eyes and not has_figure_block(blocks))
                reason, detail = bad, f"{len(md)} chars, tail {md.rstrip()[-80:]!r}"
            elif reply.get("ok") is False:
                reason = detail = f"error:{str(reply.get('error'))[:200]}"
            else:
                reason, detail = "bad_reply", f"{reply!r:.200}"
            log.warning("local layout page %d attempt %d failed (%s): %s", n, attempt, reason,
                        detail)
            if attempt == 1:
                self._kill()                    # the fault can stick to the process: fresh one
        return PageImageRead(None, False, reason)

    # -- one PDF batch --

    def _parse_batch(self, path: str, first: int, last: int) -> SourceDocument:
        why = ""
        for attempt in (1, 2):
            self._ensure_started()              # _Dead("unavailable") ends the whole PDF
            try:
                reply = self._ask({"op": "pdf", "path": path, "first": first, "last": last},
                                  batch_timeout(last - first + 1))
            except _Dead as exc:                # timeout / crash: the worker is already killed
                why = str(exc)
            else:
                if reply.get("ok") is True:
                    try:
                        return self._adapt(reply.get("raw"), first)
                    except Exception as exc:  # noqa: BLE001 - an unexpected raw shape
                        why = f"bad_reply: {type(exc).__name__}: {exc}"[:300]
                elif reply.get("ok") is False:
                    why = f"error:{str(reply.get('error'))[:200]}"
                else:
                    why = f"bad_reply: {reply!r:.200}"
            log.warning("local layout pages %d-%d attempt %d failed: %s", first, last, attempt,
                        why)
            if attempt == 1:
                self._kill()                    # the fault can stick to the process: fresh one
        raise _BatchFailed(f"pages {first}-{last} failed twice: {why}")

    def _adapt(self, raw: Any, first: int) -> SourceDocument:
        if self.backend == "mineru":
            if not isinstance(raw, list) or not all(isinstance(x, dict) for x in raw):
                raise TypeError("MinerU raw is not a list of items")
            # page_idx is absolute in the whole PDF, so first only sets where the pages start
            return from_mineru(raw, first_page=first, version=self._version)
        if not isinstance(raw, dict):
            raise TypeError("Docling raw is not a document dict")
        return from_docling(raw, version=self._version)

    # -- the worker process --

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._kill()
        if not self.available():
            raise _Dead("unavailable", "not configured or start failed recently")
        assert self._python
        env = {**worker_env(os.environ), ENV_BACKEND: self.backend, "PYTHONIOENCODING": "utf-8",
               "HF_HUB_DISABLE_TELEMETRY": "1"}
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                [self._python, "-I", "-u", self._worker], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self._failed_at = self._clock()
            raise _Dead("unavailable", f"cannot start {self._python}: {exc}") from exc
        self._proc, self._lines = proc, queue.Queue()
        self._stderr_tail.clear()
        threading.Thread(target=_pump_stdout, args=(proc, self._lines), daemon=True).start()
        threading.Thread(target=_pump_stderr, args=(proc, self._stderr_tail),
                         daemon=True).start()
        msg = self._next_message(READY_TIMEOUT_S)
        if isinstance(msg, dict) and msg.get("ready") is True:
            self._version = str(msg.get("version") or "")
            log.info("local layout worker ready in %.1f s (%s %s)", time.monotonic() - t0,
                     msg.get("backend") or self.backend, self._version or "?")
            return
        why = msg.get("error") if isinstance(msg, dict) else msg
        self._kill()
        self._failed_at = self._clock()
        raise _Dead("unavailable", f"start failed: {why}; stderr: {self._tail()}")

    def _ask(self, req: dict[str, Any], timeout: float) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdin is not None
        self._next_id += 1
        rid = self._next_id
        line = json.dumps({"id": rid, **req}, ensure_ascii=True)
        try:
            self._proc.stdin.write(line.encode("ascii") + b"\n")
            self._proc.stdin.flush()
        except OSError as exc:
            self._kill()
            raise _Dead("crashed", f"write failed: {exc}; stderr: {self._tail()}") from exc
        deadline = time.monotonic() + timeout
        while True:
            msg = self._next_message(deadline - time.monotonic())
            if not isinstance(msg, dict):
                self._kill()
                raise _Dead("timeout" if msg == "timeout" else "crashed",
                            f"{msg}; stderr: {self._tail()}")
            if msg.get("id") == rid:
                return msg
            if msg.get("id") is None and msg.get("ok") is False:
                return msg      # the worker could not parse our request: this one failed

            log.warning("local layout: ignoring reply for another request: %.200r", msg)

    def _next_message(self, timeout: float) -> dict[str, Any] | str:
        """The next protocol message, or why there is none ("timeout" / "exited ...")."""
        assert self._lines is not None and self._proc is not None
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return "timeout"
            try:
                line = self._lines.get(timeout=left)
            except queue.Empty:
                return "timeout"
            if line is None:
                try:
                    code = self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    code = None
                return f"exited (code {code})"
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("local layout: non-protocol stdout line: %.200s", line)
                continue
            if isinstance(msg, dict):
                return msg
            log.warning("local layout: non-protocol stdout line: %.200s", line)

    def _tail(self) -> str:
        return " | ".join(list(self._stderr_tail)[-5:]) or "(empty)"

    def _kill(self) -> None:
        proc, self._proc, self._lines = self._proc, None, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            log.error("local layout worker pid %d did not die after kill", proc.pid)
        if proc.stdin is not None:     # stdout / stderr: their pump threads close at EOF
            try:
                proc.stdin.close()
            except OSError:
                pass

    def _stop(self) -> None:
        """Ask the worker to exit (stdin EOF), kill it if it does not."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None and proc.stdin is not None:
            try:
                proc.stdin.close()
                proc.wait(timeout=STOP_WAIT_S)
            except (OSError, subprocess.TimeoutExpired):
                pass                            # _kill below takes over
        self._kill()

    # -- idle exit --

    def _schedule_idle(self) -> None:
        if self._proc is None:
            return
        self._idle = threading.Timer(IDLE_EXIT_S, self._idle_exit)
        self._idle.daemon = True
        self._idle.start()

    def _cancel_idle(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None

    def _idle_exit(self) -> None:
        if not self._lock.acquire(blocking=False):
            return                              # a request is running; it schedules a new timer
        try:
            if threading.current_thread() is self._idle:   # not a timer replaced meanwhile
                log.info("local layout worker idle for %s s: stopping it", IDLE_EXIT_S)
                self._idle = None
                self._stop()
        finally:
            self._lock.release()


def _pump_stdout(proc: subprocess.Popen[bytes], lines: "queue.Queue[str | None]") -> None:
    assert proc.stdout is not None
    with proc.stdout:
        for raw in proc.stdout:
            lines.put(raw.decode("utf-8", errors="replace").strip())
    lines.put(None)


def _pump_stderr(proc: subprocess.Popen[bytes], tail: deque[str]) -> None:
    assert proc.stderr is not None
    with proc.stderr:
        for raw in proc.stderr:
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                tail.append(text)
                log.debug("layout worker: %s", text)


def _stopped(should_stop: Callable[[], bool] | None) -> bool:
    if should_stop is None:
        return False
    try:
        return bool(should_stop())
    except Exception:
        log.warning("local layout: stop check failed; parse goes on", exc_info=True)
        return False
