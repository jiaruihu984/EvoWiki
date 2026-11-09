"""Render checker and component library for page-writing workflows (spec 2026-10-14 §4, B1b).

A render check opens a headless browser and a library read sees what other runs wrote, so inside a
durable workflow each is a checkpointed step returning JSON (`RenderCheck` / `LibraryComponent`
dumps, re-validated in the body); on replay the recorded result is used and no browser is opened.
Outside a workflow `step` is a plain call, so these wrappers change nothing. `put` is a database
write, not a read, and is delegated. A refine job's snapshot save (`DurableJobStore`) is a step too:
it is an append that a replay must not repeat. The image search is two steps (`DurableImages`)."""

import base64
from collections.abc import Mapping, Sequence

from evowiki.agent.durable import step
from evowiki.deep.contracts import (
    AppProbe,
    BlockLook,
    InteractiveDraft,
    LibraryComponent,
    RefineDraft,
    RefineJob,
    RenderCheck,
)
from evowiki.deep.evoui.contracts import EvoUIBlock
from evowiki.deep.images import ImageCandidate, ImageRejected, ImageSearch, PreparedImage
from evowiki.deep.library import ComponentLibrary
from evowiki.deep.store import JobStore
from evowiki.deep.tools import Checker


class DurableChecker:
    def __init__(self, inner: Checker) -> None:
        self.inner = inner

    def check(self, draft: InteractiveDraft) -> RenderCheck:
        raw = step("render.check", lambda: self.inner.check(draft).model_dump(mode="json"))
        return RenderCheck.model_validate(raw)

    def check_block(self, block_id: str, block: EvoUIBlock, images: Mapping[str, str] | None = None,
                    touch: Sequence[AppProbe] = ()) -> RenderCheck:
        raw = step("render.check_block",
                   lambda: self.inner.check_block(block_id, block, images, touch).model_dump(mode="json"))
        return RenderCheck.model_validate(raw)

    def look_block(self, block_id: str, block: EvoUIBlock, probes: Sequence[AppProbe],
                   images: Mapping[str, str] | None = None) -> BlockLook:
        raw = step("render.look_block",
                   lambda: self.inner.look_block(block_id, block, probes, images).model_dump(mode="json"))
        return BlockLook.model_validate(raw)


    def look_interactive(self, draft: InteractiveDraft) -> BlockLook:
        raw = step("render.look_interactive",
                   lambda: self.inner.look_interactive(draft).model_dump(mode="json"))
        return BlockLook.model_validate(raw)


class DurableLibrary:
    def __init__(self, inner: ComponentLibrary) -> None:
        self.inner = inner

    def list(self) -> list[LibraryComponent]:
        raw = step("library.list",
                   lambda: [c.model_dump(mode="json") for c in self.inner.list()])
        return [LibraryComponent.model_validate(r) for r in raw]

    def get(self, component_id: str) -> LibraryComponent | None:
        raw = step("library.get", lambda: (c.model_dump(mode="json")
                                           if (c := self.inner.get(component_id)) else None))
        return None if raw is None else LibraryComponent.model_validate(raw)

    def put(self, component: LibraryComponent) -> None:
        self.inner.put(component)


class DurableJobStore:
    """`JobStore` whose `save` is the step `refine.save` (an exception is re-raised in the body, where
    `DeepService.run` logs it). Reads are plain delegates: `run` only writes."""

    def __init__(self, inner: JobStore) -> None:
        self.inner = inner

    def save(self, user_id: str, job: RefineJob, draft: RefineDraft | None = None) -> bool:
        return step("refine.save", lambda: self.inner.save(user_id, job, draft) or True)

    def last_draft(self, user_id: str, job_id: str) -> RefineDraft | None:
        return self.inner.last_draft(user_id, job_id)

    def latest(self, user_id: str, page_id: str) -> RefineJob | None:
        return self.inner.latest(user_id, page_id)

    def get(self, user_id: str, job_id: str) -> RefineJob | None:
        return self.inner.get(user_id, job_id)


class DurableImages:
    """The writer's image search (find_image / place_image) as steps: a search result and a
    downloaded image are what a replay must see again, so each is recorded (`images.search`,
    `images.fetch`) and no request is repeated. A rejected download is recorded too and re-raised."""

    def __init__(self, inner: ImageSearch) -> None:
        self.inner = inner

    def search(self, query: str, limit: int) -> list[ImageCandidate]:
        raw = step("images.search", lambda: [c.model_dump(mode="json")
                                             for c in self.inner.search(query, limit)])
        return [ImageCandidate.model_validate(r) for r in raw]

    def thumbnail(self, candidate: ImageCandidate) -> PreparedImage | None:
        def run() -> dict | None:
            img = self.inner.thumbnail(candidate)
            return None if img is None else {"data_b64": img.data_b64, "width": img.width, "height": img.height}
        raw = step("images.thumbnail", run)
        return None if raw is None else PreparedImage(data=base64.b64decode(raw["data_b64"]), width=raw["width"],
                                                      height=raw["height"])

    def fetch(self, candidate: ImageCandidate) -> PreparedImage:
        def run() -> dict:
            try:
                img = self.inner.fetch(candidate)
            except ImageRejected as e:
                return {"rejected": str(e)}
            return {"data_b64": img.data_b64, "width": img.width, "height": img.height}
        raw = step("images.fetch", run)
        if "rejected" in raw:
            raise ImageRejected(raw["rejected"])
        return PreparedImage(data=base64.b64decode(raw["data_b64"]), width=raw["width"],
                             height=raw["height"])
