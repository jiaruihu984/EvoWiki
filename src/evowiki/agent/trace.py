"""Execution traces of agent loop runs (design 2026-10-21 §9.1): what each model call was sent and
answered, and what each tool call returned, in order. The loop reports to a `TraceSink`; the
Postgres sink is store/trace.py, and `NO_TRACE` keeps runs without a sink unchanged.

The payload builders here are pure, so the stored shape is defined in one place. A model step
stores the full message list of the request it sent — as content hashes, one per message — not
only the messages added since the previous step: the loop elides old tool results in later
requests (loop.elide) and compaction folds old turns, so earlier messages change between requests
and appending new ones to the previous list would rebuild a request that was never sent. The
system prompt, the tool definitions and every message are texts in llm_text, stored once by hash;
the reply and tool results stay inline (each is stored once).

Attachment parts are kept as what they were, not their content: an image keeps its media type,
sha256 and name with `data_b64` emptied, a material (DocPart) its source id, title and the first
DOC_TRACE_CHARS characters. So a trace holds no image bytes and at most DOC_TRACE_CHARS of a
material's text, and deleting the material does not touch it. This is about traces only: the
material's text is also in the sources tables (deleting the material deletes it there) and in
DBOS step outputs (`assistant.attachments.texts`, `source.parse` and the page reads), which
deleting the material does not purge. A step that sent such parts cannot be rebuilt
(`request_from` raises NotReplayable)."""

import hashlib
import json
import logging
from typing import Any, Protocol

from evowiki.agent.tools import ToolResult
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn, Part, ToolDef

# Traced steps (observability plan 2026-10-29 §5 1a): every product step is an agent_run. They live
# in llm/run_context.py (the gateway appends to the current run and must not import this package);
# re-exported here as the interface step code uses.
from evowiki.llm.run_context import (  # noqa: F401
    StepRun,
    bind_run,
    current_run_id,
    reject,
    traced_step,
)

DOC_TRACE_CHARS = 200          # of a material's text, kept in a trace


class TraceSink(Protocol):
    """Every call may be repeated with the same arguments (a durable replay re-executes the
    workflow body); a repeat changes nothing."""

    def start(self, run_id: str, *, user_id: str, agent: str, subject: str, pack_version: int,
              input: dict) -> None: ...

    def model(self, run_id: str, seq: int, request: ChatRequest, turn: ChatTurn | None,
              call_id: str | None, error: str | None) -> None: ...

    def tool(self, run_id: str, seq: int, call_id: str, name: str, args: dict,
             result: ToolResult) -> None: ...

    def finish(self, run_id: str, *, stop: str | None, error: str | None) -> None: ...


class _NoTrace:
    def start(self, run_id: str, *, user_id: str, agent: str, subject: str, pack_version: int,
              input: dict) -> None:
        pass

    def model(self, run_id: str, seq: int, request: ChatRequest, turn: ChatTurn | None,
              call_id: str | None, error: str | None) -> None:
        pass

    def tool(self, run_id: str, seq: int, call_id: str, name: str, args: dict,
             result: ToolResult) -> None:
        pass

    def finish(self, run_id: str, *, stop: str | None, error: str | None) -> None:
        pass


NO_TRACE: TraceSink = _NoTrace()

# A harness replay (harness/replay.py) is traced like any run, under a run id with this prefix and
# with `input.job.trigger == REPLAY_TRIGGER`; reads of past runs as replay sources leave both out.
REPLAY_RUN_PREFIX = "harness-replay:"
REPLAY_TRIGGER = "harness_replay"

log = logging.getLogger("evowiki.agent.trace")


def finish_failed(trace: TraceSink, run_id: str, exc: BaseException) -> None:
    """Finish a run that raised `exc` (the caller re-raises it). A failed finish never replaces
    the run's own exception: it is logged and noted on `exc`. (A run that succeeded calls
    `trace.finish` directly, so there a failed finish raises: fail closed.)"""
    try:
        trace.finish(run_id, stop=None, error=type(exc).__name__)
    except Exception as err:      # the run's own error is the one that propagates
        log.exception("trace run %s: finish failed", run_id)
        exc.add_note(f"trace finish of {run_id} failed: {type(err).__name__}: {err}")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class NotReplayable(ValueError):
    """A traced model step sent attachments whose content the trace does not keep."""


def _traced_part(p: Part) -> dict[str, Any]:
    d = p.model_dump(mode="json")
    if p.kind == "image":
        d["data_b64"] = ""
    elif p.kind == "doc":
        d["text"] = (f"{p.text[:DOC_TRACE_CHARS]}…（资料 source_id={p.source_id}，"
                     f"共 {len(p.text)} 字，未记录全文）")
    return d


def _traced_message(m: ChatMessage) -> dict[str, Any]:
    d = m.model_dump(mode="json")
    if m.parts:
        d["parts"] = [_traced_part(p) for p in m.parts]
    return d


def model_payload(request: ChatRequest, turn: ChatTurn | None,
                  error: str | None) -> tuple[dict[str, Any], dict[str, str]]:
    """The payload of a model step and the texts (hash -> text) it refers to."""
    texts: dict[str, str] = {}

    def ref(text: str) -> str:
        h = text_hash(text)
        texts[h] = text
        return h

    payload = {
        "purpose": request.purpose,
        "prompt_version": request.prompt_version,
        "subject": request.subject,
        "system_hash": ref(request.system),
        "tools_hash": ref(_json([t.model_dump(mode="json") for t in request.tools])),
        "tool_choice": request.tool_choice,
        "params": request.params,
        "messages": [ref(_json(_traced_message(m))) for m in request.messages],
        "turn": None if turn is None else turn.model_dump(mode="json"),
        "error": error,
    }
    return payload, texts


def tool_payload(call_id: str, name: str, args: dict, result: ToolResult) -> dict[str, Any]:
    return {"tool_call_id": call_id, "name": name, "args": args,
            "result": result.model_dump(mode="json")}


def model_hashes(payload: dict[str, Any]) -> list[str]:
    """The llm_text hashes a model step's payload refers to."""
    return [payload["system_hash"], payload["tools_hash"], *payload["messages"]]


def resolve_model(payload: dict[str, Any], texts: dict[str, str]) -> dict[str, Any]:
    """A model step's payload with its hashes replaced by the texts (messages and tool defs as
    dicts). A missing text raises KeyError: a step whose texts are gone cannot be read back."""
    out = {k: v for k, v in payload.items()
           if k not in ("system_hash", "tools_hash", "messages")}
    out["system"] = texts[payload["system_hash"]]
    out["tools"] = json.loads(texts[payload["tools_hash"]])
    out["messages"] = [json.loads(texts[h]) for h in payload["messages"]]
    return out


def request_from(step: dict[str, Any]) -> ChatRequest:
    """The ChatRequest a resolved model step was sent. NotReplayable when it sent images or
    materials: their content was not traced, and a request without it is not the one sent."""
    if any(p.get("kind") in ("image", "doc") for m in step["messages"]
           for p in m.get("parts", ())):
        raise NotReplayable(f"model step {step.get('seq')} sent attachments (images / materials) "
                            "whose content traces do not keep; it cannot be replayed")
    return ChatRequest(
        purpose=step["purpose"], prompt_version=step["prompt_version"], subject=step["subject"],
        system=step["system"], tools=tuple(ToolDef.model_validate(t) for t in step["tools"]),
        messages=tuple(ChatMessage.model_validate(m) for m in step["messages"]),
        tool_choice=step["tool_choice"], params=step["params"])
