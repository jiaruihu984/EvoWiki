"""Durable ports for one reading-assistant turn (B1c, spec 2026-10-14 §1–§2): the turn generator
`loop.run_turn` is the workflow body unchanged; everything it reads or writes outside the gateway
and the loop's tool steps goes through a port whose calls are recorded steps inside a durable
workflow and plain calls outside one (`agent.durable`).

  DurableSessionStore   info / entries / recent_sessions / append are steps (the log append is
                        idempotent on the entry id, so a step re-run after the row landed returns
                        the stored row)
  DurableAssistantData  the six context reads are steps
  DurableAttachmentReader  the chat attachments' rows are one step (`assistant.attachments.load`),
                        documents as their sizes (`attachments.digested`), not their text; the
                        texts the plan gives (whole text or contents) are a second step
                        (`assistant.attachments.texts`), so a turn records at most what it sends;
                        the original files are read outside steps (their bytes are never
                        recorded) and checked against the recorded sha256
  sees_images           one step per call (`assistant.sees_images`): the account's routing as the
                        first run saw it (the budget's routed provider is a step in the loop)
  MemoryPorts.durable   the memory index of an answered turn is one step
                        (`assistant.memory.index`)
  durable_turn_deps     the turn's deps with those ports, the wiki ports' `.durable()` copies,
                        recorded ids and clock; the gateway is already durable

Steps return JSON, so models are dumped with `model_dump(mode="json")` and validated back."""

import hashlib
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any

from evowiki.agent import durable
from evowiki.assistant.attachments import (
    Attachment,
    AttachmentGone,
    AttachmentReader,
    Digest,
    TextMode,
    digested,
)
from evowiki.assistant.loop import TurnDeps
from evowiki.assistant.ports import AssistantData, PageView
from evowiki.assistant.session import Entry, EntryType, SessionInfo, SessionStore
from evowiki.goals.context import PageDigest
from evowiki.goals.language import Language
from evowiki.llm.usage import Account
from evowiki.sources.models import PageRead, SectionRow, SourceRow


class DurableSessionStore:
    def __init__(self, inner: SessionStore) -> None:
        self._inner = inner

    def create(self, info: SessionInfo) -> None:
        self._inner.create(info)

    def info(self, session_id: str) -> SessionInfo:
        return SessionInfo.model_validate(durable.step(
            "assistant.info", lambda: self._inner.info(session_id).model_dump(mode="json")))

    def entries(self, session_id: str) -> list[Entry]:
        raw = durable.step("assistant.entries", lambda: [
            e.model_dump(mode="json") for e in self._inner.entries(session_id)])
        return [Entry.model_validate(r) for r in raw]

    def append(self, session_id: str, entry_id: str, type: EntryType, payload: dict[str, Any],
               at: datetime) -> Entry:
        return Entry.model_validate(durable.step(
            "assistant.append",
            lambda: self._inner.append(session_id, entry_id, type, payload, at)
            .model_dump(mode="json")))

    def request_stop(self, session_id: str, message_id: str, at: datetime) -> None:
        self._inner.request_stop(session_id, message_id, at)

    def stop_requested(self, session_id: str, message_id: str) -> bool:
        """A step in the body, so a replay takes the way the first run took; a plain read off the
        body (the poll a model call in flight is cancelled by)."""
        return durable.step("assistant.stop_requested",
                            lambda: self._inner.stop_requested(session_id, message_id))

    def recent_sessions(self, user_id: str, *, limit: int, page_id: str | None = None,
                        exclude: str | None = None) -> list[SessionInfo]:
        """A step: the turn's carry-over / close-previous (B1d) read it in the workflow body."""
        raw = durable.step("assistant.recent_sessions", lambda: [
            i.model_dump(mode="json") for i in self._inner.recent_sessions(
                user_id, limit=limit, page_id=page_id, exclude=exclude)])
        return [SessionInfo.model_validate(r) for r in raw]

    def find_proposal(self, proposal_id: str) -> tuple[SessionInfo, Entry]:
        return self._inner.find_proposal(proposal_id)

    def user_selections(self, user_id: str,
                        after: str | None = None) -> list[tuple[str, SessionInfo, Entry]]:
        return self._inner.user_selections(user_id, after)


class DurableAssistantData:
    def __init__(self, inner: AssistantData) -> None:
        self._inner = inner

    def page(self, user_id: str, page_id: str) -> PageView | None:
        raw = durable.step("assistant.data.page", lambda: (
            None if (v := self._inner.page(user_id, page_id)) is None
            else v.model_dump(mode="json")))
        return None if raw is None else PageView.model_validate(raw)

    def pages(self, user_id: str) -> Sequence[PageDigest]:
        raw = durable.step("assistant.data.pages", lambda: [
            d.model_dump(mode="json") for d in self._inner.pages(user_id)])
        return [PageDigest.model_validate(r) for r in raw]

    def k_text(self, user_id: str, concept_ids: Sequence[str]) -> str:
        return durable.step("assistant.data.k_text",
                            lambda: self._inner.k_text(user_id, concept_ids))

    def guidelines(self, user_id: str) -> Sequence[str]:
        return durable.step("assistant.data.guidelines",
                            lambda: list(self._inner.guidelines(user_id)))

    def language(self, user_id: str) -> Language:
        return durable.step("assistant.data.language", lambda: self._inner.language(user_id))

    def prereqs(self, user_id: str, concept_ids: Sequence[str]) -> tuple[str, ...]:
        return tuple(durable.step("assistant.data.prereqs",
                                  lambda: list(self._inner.prereqs(user_id, concept_ids))))

    def named_nodes(self, user_id: str, text: str) -> tuple[str, ...]:
        return tuple(durable.step("assistant.data.named_nodes",
                                  lambda: list(self._inner.named_nodes(user_id, text))))


def attachment_json(a: Attachment) -> dict[str, Any]:
    src = asdict(a.source)
    for k in ("created_at", "updated_at", "deleted_at"):
        src[k] = None if src[k] is None else src[k].isoformat()
    src["unreadable_pages"] = list(src["unreadable_pages"])
    src["glossary"] = list(src["glossary"])
    src["imprecise_pages"] = list(src["imprecise_pages"])
    src["image_pages"] = list(src["image_pages"])
    return {"source": src, "uses": list(a.uses), "linked": a.linked,
            "sections": [asdict(s) for s in a.sections],
            "reads": [asdict(r) for r in a.reads.values()], "chars": a.chars,
            "digest": None if a.digest is None else asdict(a.digest),
            "last_used": None if a.last_used is None else a.last_used.isoformat()}


def attachment_from_json(d: dict[str, Any]) -> Attachment:
    src = dict(d["source"])
    for k in ("created_at", "updated_at", "deleted_at"):
        src[k] = None if src[k] is None else datetime.fromisoformat(src[k])
    src["unreadable_pages"] = tuple(src["unreadable_pages"])
    src["glossary"] = tuple(src.get("glossary", ()))     # absent in steps recorded before 0060
    src["imprecise_pages"] = tuple(src.get("imprecise_pages", ()))   # likewise before 0105
    src["earlier_titles"] = tuple(src.get("earlier_titles", ()))     # likewise before 0109
    src["image_pages"] = tuple(src.get("image_pages", ()))           # likewise before 0108
    return Attachment(SourceRow(**src), tuple(d["uses"]), d["linked"],
                      tuple(SectionRow(**s) for s in d["sections"]),
                      {r["page"]: PageRead(**r) for r in d["reads"]}, d["chars"],
                      None if d.get("digest") is None else Digest(**d["digest"]),
                      None if d.get("last_used") is None
                      else datetime.fromisoformat(d["last_used"]))


class DurableAttachmentReader:
    """`load` is a recorded step (the rows, documents as their `Digest`; an AttachmentGone is
    recorded too), so a replay plans from what the first run read even if the material was
    deleted since. `texts` is a recorded step of only the texts the plan gives: the step values
    stay within what the turn sends (a whole text only while it fits the turn's budget, else at
    most TOC_MAX_TOKENS of contents), however long the material. `file` reads the original
    outside any step (bytes are never recorded) and fails closed (AttachmentGone) unless it
    still hashes to the loaded row's sha256."""

    def __init__(self, inner: AttachmentReader) -> None:
        self._inner = inner

    def load(self, user_id: str, ids: Sequence[str], *,
             content: bool = True) -> list[Attachment]:
        raw = durable.step("assistant.attachments.load", lambda: [
            attachment_json(digested(a) if content else a)
            for a in self._inner.load(user_id, ids, content=content)])
        return [attachment_from_json(r) for r in raw]

    def texts(self, user_id: str, wanted: Mapping[str, TextMode]) -> dict[str, str]:
        return dict(durable.step("assistant.attachments.texts",
                                 lambda: self._inner.texts(user_id, dict(wanted))))

    def file(self, user_id: str, source_id: str, *, sha256: str | None = None) -> bytes:
        data = self._inner.file(user_id, source_id, sha256=sha256)
        if sha256 is None or hashlib.sha256(data).hexdigest() != sha256:
            raise AttachmentGone(f"the original of attachment {source_id} is not the one read")
        return data


def step_sees_images(inner: Callable[[Account | None], bool]) -> Callable[[Account | None], bool]:
    """`sees_images` as one recorded step per call (a tier change before a replay changes
    nothing)."""
    def sees(account: Account | None) -> bool:
        return bool(durable.step("assistant.sees_images", lambda: bool(inner(account))))
    return sees


_MADE: "weakref.WeakSet[Any]" = weakref.WeakSet()   # ports/functions this module made durable


def _once(port: Any, make: Callable[[], Any]) -> Any:
    """`port` if it is already one of ours, else `make()` (remembered as durable)."""
    if _is_made(port):
        return port
    out = make()
    _remember(out)
    return out


def _remember(obj: Any) -> None:
    try:
        _MADE.add(obj)
    except TypeError:        # not weak-referencable / hashable: cannot be recognised later
        pass


def _is_made(obj: Any) -> bool:
    try:
        return obj in _MADE
    except TypeError:
        return False


def durable_turn_deps(deps: TurnDeps) -> TurnDeps:
    """`deps` with every port durable. Pass-through outside a workflow, so the result is also
    safe to use for an inline turn. Idempotent: ports this function made durable are kept, not
    wrapped again. A tools factory without `durable()` is refused (it would run in the workflow
    body, outside a step); a fixed list of tools is kept."""
    data = _once(deps.data, lambda: deps.data.durable() if hasattr(deps.data, "durable")
                 else DurableAssistantData(deps.data))
    tools = deps.tools
    if callable(tools):
        if not hasattr(tools, "durable"):
            raise TypeError("a tools factory needs a durable() copy to run in a workflow")
        tools = _once(tools, tools.durable)
    store = _once(deps.store, lambda: DurableSessionStore(deps.store))
    new_id = _once(deps.new_id, lambda: durable.ids(deps.new_id))
    clock = _once(deps.clock, lambda: (lambda: durable.now(deps.clock)))
    reader = deps.attachments
    attachments = None if reader is None else _once(reader, lambda: DurableAttachmentReader(reader))
    seer = deps.sees_images
    sees_images = None if seer is None else _once(seer, lambda: step_sees_images(seer))
    memory = None if deps.memory is None else deps.memory.durable()
    return replace(deps, store=store, data=data, tools=tools, new_id=new_id,
                   clock=clock, attachments=attachments, sees_images=sees_images, memory=memory)
