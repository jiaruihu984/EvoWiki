"""One GPU per process: GPU embedding batches and the local layout worker take turns (design §5,
T16).

The machine this runs on has one 12 GB card. The local layout worker
(`layout_client.LayoutWorker`, MinerU or Docling) needs most of it and an ONNX embedding session
on CUDA (`retrieval.embed.FastEmbedEmbedder`, providers "cuda") several more, so they cannot both
stay loaded. Every GPU user runs its work inside
`exclusive(self)`: the process-wide lock is taken, then every *other* registered tenant is asked to
free its VRAM (`release_gpu`); a tenant loads again lazily the next time it runs. The embedder
holds the lock per batch (one `embed_documents` / `embed_query` call), the layout worker per `read`
(one page in the importer) or `parse_pdf` (one document), so a long layout run and an embedding run
interleave; each switch reloads the other model (tens of seconds for the layout worker, ~9 s for
the embedder). A search-time query embedding follows the same rule: during a layout request it
waits for it; an idle loaded layout worker is stopped.
The process has one embedder per configuration (`retrieval.embed.embedder_from_env`), so the
service, the curator and the CLI share one GPU session.

The lock is in-process only: two service processes on one GPU are not supported (each would
believe it owns the card)."""

import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol


class GpuTenant(Protocol):
    def release_gpu(self) -> None:
        """Free this tenant's VRAM. Called with the GPU lock held by another tenant, so the
        tenant is not running GPU work; it must not block on that lock."""


_LOCK = threading.Lock()
_TENANTS: "weakref.WeakSet[GpuTenant]" = weakref.WeakSet()


def register(tenant: GpuTenant) -> None:
    """Make `tenant` one that others evict before they use the GPU (held weakly)."""
    _TENANTS.add(tenant)


@contextmanager
def exclusive(owner: GpuTenant) -> Iterator[None]:
    """Hold the GPU: wait for the lock, then release every other tenant's VRAM."""
    with _LOCK:
        for t in list(_TENANTS):
            if t is not owner:
                t.release_gpu()
        yield
