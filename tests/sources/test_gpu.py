"""GPU embeddings (EVOWIKI_EMBED_PROVIDERS=cuda) against fake fastembed / onnxruntime modules, and
the process GPU lock shared with LayoutWorker (fake worker). No real GPU, no model download."""

import json
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from evowiki.retrieval import embed
from evowiki.retrieval.embed import (
    DEFAULT_EMBED_MODEL,
    GPU_BATCH,
    FastEmbedEmbedder,
    embedder_from_env,
)
from evowiki.sources import gpu, layout_client
from evowiki.sources.layout_client import LayoutWorker
from evowiki.sources.vision import PageImageRead

FAKE_WORKER = str(Path(__file__).with_name("fake_layout_worker.py"))
TEXT = "线性回归用来建模自变量与因变量之间的线性关系，目标是让误差的平方和最小。"
PAGE = json.dumps({"markdown": TEXT, "blocks": [], "do": "ok"}).encode()


class Fakes:
    """fastembed + onnxruntime stand-ins: `active` = the providers the session ends up on."""

    def __init__(self):
        self.active = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        self.available = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        self.preloaded = 0
        self.made: list[dict] = []          # TextEmbedding kwargs, one per load
        self.batches: list[int | None] = []
        self.gate = threading.Event()
        self.gate.set()
        self.inside = threading.Event()     # set while passage_embed runs

    def modules(self) -> tuple[ModuleType, ModuleType]:
        fakes = self
        ort = ModuleType("onnxruntime")
        ort.__version__ = "1.31.0"
        ort.get_available_providers = lambda: list(fakes.available)

        def preload_dlls():
            fakes.preloaded += 1
        ort.preload_dlls = preload_dlls

        class TextEmbedding:
            @staticmethod
            def list_supported_models():
                return [{"model": DEFAULT_EMBED_MODEL, "dim": 3}]

            def __init__(self, **kw):
                fakes.made.append(kw)
                self.model = SimpleNamespace(
                    model=SimpleNamespace(get_providers=lambda: list(fakes.active)))

            def passage_embed(self, texts, batch_size=None):
                fakes.batches.append(batch_size)
                fakes.inside.set()
                assert fakes.gate.wait(10)
                fakes.inside.clear()
                return iter([[1.0, 0.0, 0.0] for _ in texts])

            def query_embed(self, texts):
                return iter([[0.0, 1.0, 0.0] for _ in texts])

        fe = ModuleType("fastembed")
        fe.__version__ = "0.8.1"
        fe.TextEmbedding = TextEmbedding
        return fe, ort


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch):
    """The GPU tenants and the shared embedders are process-global: each test starts without
    them."""
    monkeypatch.setattr(gpu, "_TENANTS", type(gpu._TENANTS)())
    monkeypatch.setattr(embed, "_SHARED", {})


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()
    fe, ort = f.modules()
    monkeypatch.setitem(sys.modules, "fastembed", fe)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    return f


@pytest.fixture
def ocr(tmp_path, monkeypatch):
    path = tmp_path / "starts.log"
    monkeypatch.setenv("FAKE_LAYOUT_LOG", str(path))
    monkeypatch.delenv("FAKE_LAYOUT_START", raising=False)
    monkeypatch.setattr(layout_client, "_WORKER_ENV_PREFIXES", (*layout_client._WORKER_ENV_PREFIXES, "FAKE_"))
    o = LayoutWorker(python=sys.executable, worker=FAKE_WORKER)
    o.starts = lambda: (path.read_text(encoding="utf-8").splitlines().count("start")
                        if path.exists() else 0)
    yield o
    o.close()


# -- the provider check (fail closed) --

def test_cuda_requested_but_session_on_cpu_is_an_error(fakes):
    fakes.active = ["CPUExecutionProvider"]          # e.g. CUDA 12 wheels: silent fallback
    e = FastEmbedEmbedder(providers="cuda")
    with pytest.raises(RuntimeError, match="refusing a CPU embedder"):
        e.check_gpu()
    with pytest.raises(RuntimeError, match="refusing a CPU embedder"):
        e.embed_documents(["a"])
    assert fakes.preloaded == 2 and e._model is None


def test_onnxruntime_without_cuda_provider_is_an_error(fakes):
    fakes.available = ["CPUExecutionProvider"]       # the CPU package overwrote onnxruntime-gpu
    with pytest.raises(RuntimeError, match="has no CUDA provider"):
        FastEmbedEmbedder(providers="cuda").embed_query("q")
    assert fakes.made == []


def test_cuda_embedder_preloads_dlls_batches_64_and_caps_memory(fakes):
    e = FastEmbedEmbedder(providers="cuda", gpu_mem_mb=4096)
    assert e.dim == 3 and e.model_version == f"{DEFAULT_EMBED_MODEL}@d3" and fakes.made == []  # lazy
    assert e.embed_documents(["a", "b"]) == [[1.0, 0.0, 0.0]] * 2
    assert e.embed_query("q") == [0.0, 1.0, 0.0]
    assert fakes.preloaded == 1 and fakes.batches == [GPU_BATCH]
    (kw,) = fakes.made
    assert kw["providers"] == [("CUDAExecutionProvider", {
        "gpu_mem_limit": str(4096 * 1024 * 1024), "arena_extend_strategy": "kSameAsRequested"})]


def test_check_gpu_loads_then_frees(fakes):
    e = FastEmbedEmbedder(providers="cuda")
    e.check_gpu()
    assert len(fakes.made) == 1 and e._model is None
    assert fakes.made[0]["providers"] == [("CUDAExecutionProvider", {})]


def test_default_is_cpu_unchanged(fakes):
    e = embedder_from_env({})
    assert isinstance(e, FastEmbedEmbedder) and not e.on_gpu
    e.check_gpu()                                     # no-op
    assert fakes.made == []
    with gpu._LOCK:                                   # the CPU embedder never takes the GPU lock
        assert e.embed_documents(["a"]) == [[1.0, 0.0, 0.0]]
    assert fakes.made == [{"model_name": DEFAULT_EMBED_MODEL}] and fakes.batches == [None]
    assert fakes.preloaded == 0


def test_one_embedder_per_process(fakes, monkeypatch):
    env = {"EVOWIKI_EMBEDDER": "fastembed", "EVOWIKI_EMBED_PROVIDERS": "cuda"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    service = embedder_from_env(dict(env))
    assert embedder_from_env(dict(env)) is service
    assert embedder_from_env({**env, "EVOWIKI_EMBED_GPU_MEM_MB": "2048"}) is not service


def test_embedder_from_env_gpu_settings():
    e = embedder_from_env({"EVOWIKI_EMBED_PROVIDERS": "cuda", "EVOWIKI_EMBED_GPU_MEM_MB": "2048"})
    assert e.on_gpu and e._gpu_mem_mb == 2048
    assert not embedder_from_env({"EVOWIKI_EMBED_PROVIDERS": "cpu"}).on_gpu
    with pytest.raises(ValueError, match="EVOWIKI_EMBED_PROVIDERS"):
        embedder_from_env({"EVOWIKI_EMBED_PROVIDERS": "CUDAExecutionProvider"})
    with pytest.raises(ValueError, match="EVOWIKI_EMBED_GPU_MEM_MB"):
        embedder_from_env({"EVOWIKI_EMBED_GPU_MEM_MB": "4GB"})


# -- the GPU lock shared with local OCR --

def test_idle_layout_worker_is_closed_before_an_embedding_batch(fakes, ocr):
    assert ocr.read([PAGE]) == [PageImageRead(TEXT, False)]
    proc = ocr._proc
    assert proc is not None and proc.poll() is None
    e = FastEmbedEmbedder(providers="cuda")
    e.embed_documents(["a"])
    assert ocr._proc is None and proc.poll() is not None and ocr._idle is None
    assert ocr.read([PAGE]) == [PageImageRead(TEXT, False)]     # loads again lazily
    assert ocr.starts() == 2
    assert e._model is None                          # ... and the read freed the embedder


def test_ocr_read_waits_while_an_embedding_batch_holds_the_gpu(fakes, ocr):
    e = FastEmbedEmbedder(providers="cuda")
    fakes.gate.clear()
    embedding = threading.Thread(target=e.embed_documents, args=(["a"],))
    embedding.start()
    assert fakes.inside.wait(10)
    out: list = []
    reading = threading.Thread(target=lambda: out.extend(ocr.read([PAGE])))
    reading.start()
    time.sleep(0.5)
    assert out == [] and reading.is_alive() and ocr.starts() == 0   # not even started
    fakes.gate.set()
    embedding.join(10)
    reading.join(30)
    assert out == [PageImageRead(TEXT, False)]


@pytest.mark.parametrize("call", ["embed_documents", "embed_query"])
def test_embedding_waits_while_an_ocr_read_holds_the_gpu(fakes, call):
    order: list[str] = []

    class Busy:
        def release_gpu(self):
            order.append("released")

    busy = Busy()
    gpu.register(busy)
    e = FastEmbedEmbedder(providers="cuda")
    with gpu.exclusive(busy):
        t = threading.Thread(target=lambda: order.append(
            "embedded" if (e.embed_documents(["a"]) if call == "embed_documents"
                           else e.embed_query("q")) else "empty"))
        t.start()
        time.sleep(0.3)
        assert order == [] and fakes.made == []       # a query waits for the page too
    t.join(10)
    assert order == ["released", "embedded"]
