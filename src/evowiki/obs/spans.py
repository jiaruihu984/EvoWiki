"""One agent run as OTLP spans, metadata only (observability plan ruling 1).

What a span carries: ids, the agent / step_key, purpose, model and provider, prompt / code / pack
versions, hashes (input, system prompt, tool definitions), cost, latency, token counts, status and
the type of a stop or an error. Never a prompt, a message, a reply, a tool argument or result, a
subject or an error message: those can hold the learner's words. The raw text is one click away
(`evowiki.trace_url`, an owner-scoped EvoWiki endpoint reading Postgres).

Ids Phoenix sees are derived, so it holds no EvoWiki run id or session id:
- the trace id is sha256(run_id)[:32] (`trace_id`), the store computes the same in SQL;
- span ids are sha256(run_id/seq)[:16]; a resend makes the same ids;
- `session.id` (OpenInference: Phoenix groups traces into sessions by it) is
  `s-` + sha256(session_id)[:32] for an assistant run `assistant:{session}:{message}`.

Span kinds follow OpenInference (`openinference.span.kind`): the run is AGENT, a model step LLM, a
tool step TOOL."""

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import Link, SpanContext, SpanKind, Status, StatusCode, TraceFlags

SCOPE = InstrumentationScope("evowiki.obs", "1")
ASSISTANT_PREFIX = "assistant:"
_CODE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]{0,79}")


def trace_id(run_id: str) -> str:
    return hashlib.sha256(run_id.encode()).hexdigest()[:32]


def span_id(run_id: str, part: str) -> int:
    return int(hashlib.sha256(f"{run_id}/{part}".encode()).hexdigest()[:16], 16) or 1


def session_of(run_id: str) -> tuple[str, str] | None:
    """(session_id, message_id) of an assistant turn's run, else None."""
    if not run_id.startswith(ASSISTANT_PREFIX):
        return None
    sid, _, rest = run_id.removeprefix(ASSISTANT_PREFIX).partition(":")
    mid = rest.split(":", 1)[0]
    return (sid, mid) if sid and mid else None


def session_key(session_id: str) -> str:
    return "s-" + hashlib.sha256(session_id.encode()).hexdigest()[:32]


def reason(text: str | None) -> str | None:
    """The type of a stop or an error, not its message: what comes before the first ':' (an
    exception's class, a stop code) when it is a code-like name, else "other". A message can
    quote the learner or the model."""
    if not text:
        return None
    head = text.split(":", 1)[0].strip()
    return head if _CODE.fullmatch(head) else "other"


def resource(project: str) -> Resource:
    return Resource.create({"service.name": "evowiki", "openinference.project.name": project})


def _ns(t: datetime) -> int:
    return int(t.timestamp() * 1_000_000_000)


def _ctx(tid: int, sid: int) -> SpanContext:
    return SpanContext(tid, sid, is_remote=False, trace_flags=TraceFlags(TraceFlags.SAMPLED))


def _clean(attrs: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v is not None}


def _call_attrs(call: Mapping[str, Any] | None) -> dict[str, Any]:
    """The metadata of the step's llm_call row (store.otel_export.steps_of drops its texts)."""
    c = call or {}
    return {"llm.model_name": c.get("model"), "llm.provider": c.get("provider"),
            "evowiki.purpose": c.get("purpose"), "evowiki.prompt_version": c.get("prompt_version"),
            "evowiki.model_version": c.get("model_version"),
            "evowiki.input_hash": c.get("input_hash"), "evowiki.cost_usd": c.get("cost"),
            "evowiki.latency_s": c.get("latency"), "evowiki.replayable": c.get("replayable"),
            "evowiki.served_from": c.get("served_from")}


def run_spans(run: Mapping[str, Any], steps: Sequence[Mapping[str, Any]],
              artifacts: Sequence[Mapping[str, Any]], *, resource: Resource,
              link_base: str, session_id: str | None = None) -> list[ReadableSpan]:
    """`run`: an agent_run row (to_jsonb: step_key / code_version read when present), `steps`:
    store.otel_export.steps_of rows, `artifacts`: artifacts_of rows, `session_id`: the assistant
    session the run belongs to (its own id's, or its parent chain's: store.lineage).

    Step kinds: 'model' (an agent loop's model call, or a chat call outside one, `via`), 'tool',
    'call' (a JSON call through the gateway) and 'reject' (a parsed reply the step refused: its
    reason is exported as a code or "other", never as text). A nested run (input.parent_run) links
    to its parent's trace."""
    run_id = run["run_id"]
    tid_hex = trace_id(run_id)
    tid = int(tid_hex, 16)
    root = _ctx(tid, span_id(run_id, "run"))
    started = as_time(run["started_at"])
    last = started
    spans: list[ReadableSpan] = []
    cost = 0.0
    tokens_in = tokens_out = 0
    if session_id is None and (sess := session_of(run_id)) is not None:
        session_id = sess[0]
    common = _clean({"user.id": run["user_id"],
                     "session.id": session_key(session_id) if session_id else None})
    for s in steps:
        ts = as_time(s["ts"])
        payload = s["payload"] or {}
        call = s.get("call") or {}
        kind = s["kind"]
        latency = call.get("latency")
        start = max(last, ts - timedelta(seconds=latency)) if latency is not None else last
        if kind in ("model", "call"):
            if kind == "model":
                turn = payload.get("turn") or {}
                ti, to = int(turn.get("input_tokens") or 0), int(turn.get("output_tokens") or 0)
            else:
                ti, to = int(s.get("input_tokens") or 0), int(s.get("output_tokens") or 0)
            tokens_in += ti
            tokens_out += to
            cost += call.get("cost") or 0.0
            err = reason(payload.get("error"))
            attrs = {
                **_call_attrs(call),
                "openinference.span.kind": "LLM",
                "llm.token_count.prompt": ti,
                "llm.token_count.completion": to,
                "llm.token_count.total": ti + to,
                "evowiki.call_id": s.get("call_id"),
                "evowiki.purpose": call.get("purpose") or payload.get("purpose"),
                "evowiki.prompt_version": (call.get("prompt_version")
                                           or payload.get("prompt_version")),
                "evowiki.system_hash": payload.get("system_hash"),
                "evowiki.tools_hash": payload.get("tools_hash"),
                "evowiki.tool_calls": (len((payload.get("turn") or {}).get("tool_calls") or ())
                                       if kind == "model" else None),
                "evowiki.via": payload.get("via"),
                "evowiki.schema": payload.get("schema"),
                "evowiki.input_call": payload.get("input_call"),
                "evowiki.served_from": call.get("served_from") or payload.get("served_from"),
                "evowiki.output_hash": payload.get("output_hash"),
                "evowiki.error_type": err,
            }
            name = f"llm {call.get('model') or payload.get('purpose') or ''}".strip()
        elif kind == "reject":
            err = reason(payload.get("reason")) or "rejected"
            start = last
            attrs = {"openinference.span.kind": "CHAIN", "evowiki.call_id": s.get("call_id"),
                     "evowiki.reject_reason": err,
                     "evowiki.output_hash": payload.get("output_hash")}
            name = "reject"
        else:
            result = payload.get("result") or {}
            err = reason(result.get("error"))
            start = last
            attrs = {"openinference.span.kind": "TOOL", "tool.name": payload.get("name"),
                     "evowiki.tool_call_id": payload.get("tool_call_id"),
                     "evowiki.error_type": err}
            name = f"tool {payload.get('name') or ''}".strip()
        spans.append(ReadableSpan(
            name=name, context=_ctx(tid, span_id(run_id, str(s["seq"]))), parent=root,
            resource=resource,
            attributes=_clean({**common, **attrs, "evowiki.step_kind": kind,
                               "evowiki.seq": s["seq"]}),
            kind=SpanKind.INTERNAL, instrumentation_scope=SCOPE,
            status=Status(StatusCode.ERROR, err) if err else Status(StatusCode.OK),
            start_time=_ns(start), end_time=_ns(max(ts, start))))
        last = max(last, ts)
    finished = as_time(run["finished_at"]) if run.get("finished_at") else None
    end = max(finished or last, last)
    err = reason(run.get("error"))
    events = [Event("artifact", _clean({
        "artifact.id": a["artifact"].get("artifact_id"),
        "artifact.kind": a["artifact"].get("kind"),
        "artifact.version_key": a["artifact"].get("version_key"),
        "artifact.parent_ids": tuple(p["id"] for p in a["parents"]) or None,
        "artifact.parent_roles": tuple(p.get("role") or "" for p in a["parents"]) or None,
    }), timestamp=_ns(as_time(a["artifact"]["created_at"]) if a["artifact"].get("created_at")
                      else end)) for a in artifacts]
    parent_run = (run.get("input") or {}).get("parent_run")
    links = ([Link(_ctx(int(trace_id(parent_run), 16), span_id(parent_run, "run")))]
             if parent_run else [])
    spans.insert(0, ReadableSpan(
        name=run.get("step_key") or run["agent"], context=root, parent=None, resource=resource,
        links=links,
        attributes=_clean({
            **common,
            "openinference.span.kind": "AGENT",
            "evowiki.agent": run["agent"],
            "evowiki.step_key": run.get("step_key"),
            "evowiki.parent_trace_id": trace_id(parent_run) if parent_run else None,
            "evowiki.auto": (run.get("input") or {}).get("auto") is True or None,
            "evowiki.code_version": run.get("code_version"),
            "evowiki.pack_version": run.get("pack_version"),
            "evowiki.stop": reason(run.get("stop")),
            "evowiki.error_type": err,
            "evowiki.unfinished": True if finished is None else None,
            "evowiki.cost_usd": cost,
            "llm.token_count.prompt": tokens_in,
            "llm.token_count.completion": tokens_out,
            "llm.token_count.total": tokens_in + tokens_out,
            "evowiki.steps": len(steps),
            "evowiki.artifact_ids": tuple(a["artifact"]["artifact_id"] for a in artifacts) or None,
            "evowiki.trace_url": f"{link_base.rstrip('/')}/wiki/traces/otel/{quote(tid_hex)}",
        }),
        events=events, kind=SpanKind.INTERNAL, instrumentation_scope=SCOPE,
        status=Status(StatusCode.ERROR, err) if err else Status(StatusCode.OK),
        start_time=_ns(started), end_time=_ns(end)))
    return spans


def as_time(v: Any) -> datetime:
    """A timestamp from to_jsonb (ISO text) or from a row (datetime)."""
    return v if isinstance(v, datetime) else datetime.fromisoformat(v)
