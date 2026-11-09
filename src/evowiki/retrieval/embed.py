import gc
import hashlib
import logging
import math
import os
import re
from collections.abc import Mapping, Sequence
from itertools import pairwise
from typing import Protocol, runtime_checkable

_TOKEN = re.compile(r"[a-z0-9]+|[㐀-鿿]")  # ASCII words, single CJK characters
DEFAULT_EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
ENV_PROVIDERS = "EVOWIKI_EMBED_PROVIDERS"     # unset / "cpu": CPU; "cuda": GPU, no CPU fallback
ENV_GPU_MEM_MB = "EVOWIKI_EMBED_GPU_MEM_MB"   # optional cap of the CUDA arena (MB)
GPU_BATCH = 64                                # faster than fastembed's 256 on the GPU (measured)

log = logging.getLogger("evowiki.retrieval.embed")


@runtime_checkable
class Embedder(Protocol):
    """Every vector is tagged with (id, model_version, dim); vectors of different tags are never
    compared (decision 11)."""

    id: str
    model_version: str
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Deterministic signed feature hashing (unigrams + bigrams), L2-normalized. Tests only."""

    id = "hash"

    def __init__(self, dim: int = 384):
        self.dim = dim
        self.model_version = f"hash-v1-d{dim}"

    def _one(self, text: str) -> list[float]:
        toks = _TOKEN.findall(text.lower())
        feats = [(t, 1.0) for t in toks] + [(f"{a} {b}", 0.5) for a, b in pairwise(toks)]
        v = [0.0] * self.dim
        for f, w in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += w if (h >> 32) & 1 else -w
        n = math.sqrt(sum(x * x for x in v))
        if n == 0.0:
            v[0] = 1.0  # pgvector cosine distance is undefined for the zero vector
            return v
        return [x / n for x in v]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._one(text)


class FastEmbedEmbedder:
    """Local ONNX embeddings (optional extra `embed`); multilingual by default (decision 11).
    The model is downloaded once on first use. `model_version` and `dim` resolve lazily, without
    loading the model.

    `providers="cuda"` (extra `embed-gpu`, env EVOWIKI_EMBED_PROVIDERS=cuda) runs the session on
    the CUDA execution provider, batches of GPU_BATCH. ONNX Runtime falls back to the CPU silently
    when the CUDA libraries do not load (e.g. CUDA 12 wheels under onnxruntime-gpu 1.31), so the
    loaded session is checked and a session without CUDAExecutionProvider is an error, never a
    slow CPU embedder. On the GPU every call holds `sources.gpu.exclusive` (it evicts an idle
    local OCR worker first) and the session is dropped when another GPU user takes the card
    (`release_gpu`); it loads again on the next call. A query (`embed_query`, at search time)
    follows the same rule: while a local OCR read runs it waits for that page (~9 s); an idle,
    loaded OCR worker is stopped (~30 s to load again on its next page). Vectors match the CPU
    ones (cosine >= 0.9999996), so `model_version` (model name + dimension) depends on neither
    the device nor the fastembed version."""

    def __init__(self, model_name: str = DEFAULT_EMBED_MODEL, providers: str | None = None,
                 gpu_mem_mb: int | None = None):
        if providers not in (None, "cpu", "cuda"):
            raise ValueError(f"unknown {ENV_PROVIDERS}: {providers!r} (expected 'cuda' or 'cpu')")
        self.id = f"fastembed:{model_name}"
        self._name = model_name
        self.on_gpu = providers == "cuda"
        self._gpu_mem_mb = gpu_mem_mb
        self._model = None
        self._version: str | None = None
        self._dim: int | None = None
        if self.on_gpu:
            from evowiki.sources import gpu
            gpu.register(self)

    def _meta(self) -> None:
        if self._dim is None:
            import fastembed  # optional extra `embed` (or `embed-gpu`: same module)

            spec = next(m for m in fastembed.TextEmbedding.list_supported_models()
                        if m["model"] == self._name)
            self._dim = int(spec["dim"])
            # the vector space is named by the model, not the library: a fastembed upgrade that
            # keeps the same weights must not orphan every stored vector
            self._version = f"{self._name}@d{self._dim}"

    def _load(self):
        if self._model is None:
            import fastembed

            self._meta()
            self._model = (self._load_cuda(fastembed) if self.on_gpu
                           else fastembed.TextEmbedding(model_name=self._name))
        return self._model

    def _load_cuda(self, fastembed):
        import onnxruntime as ort

        if (not hasattr(ort, "preload_dlls")
                or "CUDAExecutionProvider" not in ort.get_available_providers()):
            raise RuntimeError(
                f"{ENV_PROVIDERS}=cuda but onnxruntime {ort.__version__} has no CUDA provider "
                "(install the `embed-gpu` extra; if the CPU package `onnxruntime` overwrote it: "
                "uv sync --extra embed-gpu --reinstall-package onnxruntime-gpu)")
        ort.preload_dlls()          # the CUDA / cuDNN DLLs of the nvidia-* wheels
        opts: dict[str, str] = {}
        if self._gpu_mem_mb:
            opts = {"gpu_mem_limit": str(self._gpu_mem_mb * 1024 * 1024),
                    "arena_extend_strategy": "kSameAsRequested"}
        model = fastembed.TextEmbedding(model_name=self._name,
                                        providers=[("CUDAExecutionProvider", opts)])
        active = model.model.model.get_providers()
        if "CUDAExecutionProvider" not in active:
            raise RuntimeError(
                f"{ENV_PROVIDERS}=cuda but the embedding session runs on {active}: the CUDA "
                "libraries did not load (onnxruntime-gpu 1.31 needs the CUDA 13 wheels of the "
                "`embed-gpu` extra); refusing a CPU embedder")
        log.info("embedder %s on the GPU (%s)", self._name, ", ".join(active))
        return model

    def check_gpu(self) -> None:
        """Service start: load the model once so a CUDA session that fell back to the CPU
        fails here (raises), then free the VRAM again. No-op on the CPU."""
        if self.on_gpu:
            from evowiki.sources import gpu
            with gpu.exclusive(self):
                self._load()
                self.release_gpu()

    def release_gpu(self) -> None:
        """Drop the CUDA session (frees its VRAM); called under the GPU lock (`sources.gpu`)."""
        if self._model is not None:
            self._model = None
            gc.collect()
            log.info("embedder %s: GPU session released", self._name)

    @property
    def model_version(self) -> str:
        self._meta()
        return self._version

    @property
    def dim(self) -> int:
        self._meta()
        return self._dim

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.on_gpu:
            return [[float(x) for x in v] for v in self._load().passage_embed(list(texts))]
        from evowiki.sources import gpu
        with gpu.exclusive(self):
            return [[float(x) for x in v]
                    for v in self._load().passage_embed(list(texts), batch_size=GPU_BATCH)]

    def embed_query(self, text: str) -> list[float]:
        if not self.on_gpu:
            return [float(x) for x in next(iter(self._load().query_embed([text])))]
        from evowiki.sources import gpu
        with gpu.exclusive(self):
            return [float(x) for x in next(iter(self._load().query_embed([text])))]


_SHARED: dict[tuple[str, str | None, int | None], "FastEmbedEmbedder"] = {}


def embedder_from_env(env: Mapping[str, str] = os.environ) -> Embedder:
    """The embedder the environment names. One FastEmbedEmbedder per configuration per process
    (the service, the curator's candidate search and the CLI share it): one model load, and on the
    GPU one session and one `sources.gpu` tenant instead of several that evict each other."""
    kind = env.get("EVOWIKI_EMBEDDER", "fastembed")
    if kind == "hash":
        return HashingEmbedder()
    if kind == "fastembed":
        mem = env.get(ENV_GPU_MEM_MB) or None
        if mem is not None and not (mem.isdigit() and int(mem) > 0):
            raise ValueError(f"{ENV_GPU_MEM_MB} must be a positive number of MB, not {mem!r}")
        key = (env.get("EVOWIKI_EMBED_MODEL", DEFAULT_EMBED_MODEL),
               env.get(ENV_PROVIDERS) or None, int(mem) if mem else None)
        if key not in _SHARED:
            _SHARED[key] = FastEmbedEmbedder(key[0], providers=key[1], gpu_mem_mb=key[2])
        return _SHARED[key]
    raise ValueError(f"unknown EVOWIKI_EMBEDDER: {kind!r} (expected 'fastembed' or 'hash')")
