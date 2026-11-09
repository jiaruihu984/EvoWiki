"""Agent run traces and single-shot request inputs in Postgres (migration 0064, design 2026-10-21
§9.1). The payload shapes are agent/trace.py's; this module only stores and reads them.

Every insert is `on conflict do nothing` on its key: a DBOS workflow body is re-executed from the
top on recovery and calls the sink again with the same run ids and step numbers, so the first
write stands and a replay adds nothing. Each sink call commits on its own; a step written before
a crash stays.

Retention (`store.retention`, migration 0092) expires old texts and deletes old runs and the texts
nothing points at. A sink call may reuse a text row that already exists and only later (same
transaction) write the step that points at it, so sink writes hold the shared side of one advisory
lock until they commit and retention takes the exclusive side (PRUNE_LOCK): it never sees a text
that is about to be referenced as unreferenced."""

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from evowiki.agent.tools import ToolResult
from evowiki.agent.trace import (
    REPLAY_RUN_PREFIX,
    REPLAY_TRIGGER,
    model_hashes,
    model_payload,
    request_from,
    resolve_model,
    text_hash,
    tool_payload,
)
from evowiki.llm.chat import ChatRequest, ChatTurn
from evowiki.llm.models import LLMCall, LLMRequest
from evowiki.llm.run_context import code_version, output_hash
from evowiki.store.db import fetch_all, fetch_one

_LOCK = "pg_advisory_xact_lock{}(hashtext('trace:prune'))"
PRUNE_LOCK = _LOCK.format("")


def _sink_lock(conn: psycopg.Connection) -> None:
    conn.execute("select " + _LOCK.format("_shared"))


def _put_texts(conn: psycopg.Connection, texts: dict[str, str]) -> None:
    if texts:
        with conn.cursor() as cur:
            # sorted by hash: two sinks inserting overlapping texts lock rows in the same order
            cur.executemany("insert into llm_text (hash, text) values (%s, %s) "
                            "on conflict do nothing", sorted(texts.items()))


def _get_texts(conn: psycopg.Connection, hashes: Iterable[str]) -> dict[str, str]:
    rows = fetch_all(conn, "select hash, text from llm_text where hash = any(%s)",
                     (list(set(hashes)),))
    return {r["hash"]: r["text"] for r in rows}


def _step(conn: psycopg.Connection, run_id: str, seq: int, kind: str, payload: dict[str, Any],
          call_id: str | None, ts: datetime) -> None:
    conn.execute("insert into agent_step (run_id, seq, kind, payload, call_id, ts) "
                 "values (%s, %s, %s, %s, %s, %s) on conflict do nothing",
                 (run_id, seq, kind, Jsonb(payload), call_id, ts))


class PgTraceSink:
    """The agent loops' TraceSink and the traced steps' StepStore (llm/run_context.py).

    `step_key` (migration 0090) names the step; an agent loop's run, started without one, gets
    its agent name. `code_version` is the process's (`run_context.code_version`). Steps the
    gateway appends (kind 'call' for a JSON call, a chat step marked `via`, kind 'reject') are
    unique per run and call id, so a re-executed workflow body adds none twice."""

    def __init__(self, pool: ConnectionPool, clock: Callable[[], datetime]) -> None:
        self._pool = pool
        self._clock = clock

    def start(self, run_id: str, *, user_id: str, agent: str, subject: str, pack_version: int,
              input: dict, step_key: str | None = None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "insert into agent_run (run_id, user_id, agent, subject, pack_version, input, "
                "started_at, step_key, code_version) values (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict do nothing",
                (run_id, user_id, agent, subject, pack_version, Jsonb(input), self._clock(),
                 step_key or agent, code_version()))

    def chat(self, run_id: str, seq: int, request: ChatRequest, turn: ChatTurn | None,
             call_id: str | None, error: str | None) -> None:
        """A chat call outside an agent loop: a model step like a loop's, marked `via`."""
        payload, texts = model_payload(request, turn, error)
        with self._pool.connection() as conn:
            _sink_lock(conn)
            _put_texts(conn, texts)
            _step(conn, run_id, seq, "model", {**payload, "via": "gateway"}, call_id,
                  self._clock())

    def call(self, run_id: str, seq: int, request: LLMRequest, schema_name: str,
             call: LLMCall | None, error: str | None) -> None:
        """A JSON call: its input is the llm_request of `input_call` (the call itself, or the one
        a logical call was served from); a failed call has none."""
        payload = {"purpose": request.purpose, "subject": request.subject,
                   "prompt_version": call.prompt_version if call else request.prompt_version,
                   "schema": schema_name, "error": error,
                   "input_call": (call.served_from or call.id) if call else None,
                   "served_from": call.served_from if call else None,
                   "output_hash": output_hash(call.raw_output) if call else None}
        if request.params.get("source_nodes"):
            payload["source_nodes"] = request.params["source_nodes"]
        with self._pool.connection() as conn:
            _step(conn, run_id, seq, "call", payload, call.id if call else None, self._clock())

    def reject(self, run_id: str, seq: int, reason: str, call: LLMCall | None,
               detail: dict[str, Any]) -> None:
        """A reply the step refused after it parsed (`traced_step` StepRun.reject)."""
        payload = {"reason": reason, "detail": detail,
                   "output_hash": output_hash(call.raw_output) if call else None}
        with self._pool.connection() as conn:
            _step(conn, run_id, seq, "reject", payload, call.id if call else None,
                  self._clock())

    def model(self, run_id: str, seq: int, request: ChatRequest, turn: ChatTurn | None,
              call_id: str | None, error: str | None) -> None:
        payload, texts = model_payload(request, turn, error)
        with self._pool.connection() as conn:
            _sink_lock(conn)
            _put_texts(conn, texts)
            _step(conn, run_id, seq, "model", payload, call_id, self._clock())

    def tool(self, run_id: str, seq: int, call_id: str, name: str, args: dict,
             result: ToolResult) -> None:
        with self._pool.connection() as conn:
            _step(conn, run_id, seq, "tool", tool_payload(call_id, name, args, result), None,
                  self._clock())

    def finish(self, run_id: str, *, stop: str | None, error: str | None) -> None:
        with self._pool.connection() as conn:
            done = conn.execute(
                "update agent_run set finished_at = %s, stop = %s, error = %s "
                "where run_id = %s and finished_at is null",
                (self._clock(), stop, error, run_id)).rowcount
            if not done and fetch_one(conn, "select 1 from agent_run where run_id = %s",
                                      (run_id,)) is None:
                raise LookupError(f"agent run {run_id!r} was never started")


class PgRequestLog:
    """The full input of a single-shot JSON call, keyed by its llm_call id."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def record(self, call_id: str, system: str, prompt: str, schema_name: str, *,
               source_user_id: str | None = None, source_nodes: Sequence[str] = ()) -> None:
        s, p = text_hash(system), text_hash(prompt)
        with self._pool.connection() as conn:
            if source_nodes:
                from evowiki.memory.forget import ForgetBusy
                from evowiki.store.dreaming import sources_visible
                if source_user_id is None:
                    raise ValueError('request provenance needs its user')
                got = conn.execute('select pg_try_advisory_xact_lock(hashtext(%s)) as ok',
                                   ('memory:user:' + source_user_id,)).fetchone()
                if not got['ok'] or not sources_visible(conn, source_user_id, source_nodes):
                    raise ForgetBusy('request source is unavailable; retry after memory changes')
                with conn.cursor() as cur:
                    cur.executemany('insert into memory_dependency(user_id,source,target,owner) '
                                    'values (%s,%s,%s,%s) on conflict do nothing',
                                    [(source_user_id, node, 'call:' + call_id, 'call:' + call_id)
                                     for node in source_nodes])
            _sink_lock(conn)
            _put_texts(conn, {s: system, p: prompt})
            conn.execute("insert into llm_request (call_id, system_hash, prompt_hash, "
                         "schema_name) values (%s, %s, %s, %s) on conflict do nothing",
                         (call_id, s, p, schema_name))


def run(conn: psycopg.Connection, run_id: str) -> dict | None:
    return fetch_one(conn, "select * from agent_run where run_id = %s", (run_id,))


def _resolved(conn: psycopg.Connection, rows: list[dict]) -> list[dict]:
    """A step store.retention expired (`expired` in its payload) comes back as stored: its texts
    are gone. A JSON call step gets its input from llm_request (none once that expired)."""
    def live(r: dict) -> bool:
        return r["kind"] == "model" and not r["payload"].get("expired")
    texts = _get_texts(conn, (h for r in rows if live(r) for h in model_hashes(r["payload"])))
    inputs = _call_inputs(conn, [r["payload"].get("input_call") for r in rows
                                 if r["kind"] == "call"])
    out = []
    for r in rows:
        body = resolve_model(r["payload"], texts) if live(r) else dict(r["payload"])
        if r["kind"] == "call":
            body |= inputs.get(body.get("input_call"), {})
        out.append({"seq": r["seq"], "kind": r["kind"], "call_id": r["call_id"], "ts": r["ts"],
                    **body})
    return out


def _call_inputs(conn: psycopg.Connection, call_ids: list[str | None]) -> dict[str, dict]:
    """The `system` and `prompt` each JSON call was sent (llm_request), by call id."""
    ids = [c for c in call_ids if c]
    if not ids:
        return {}
    rows = fetch_all(conn, "select r.call_id, s.text as system, p.text as prompt from llm_request r "
                           "join llm_text s on s.hash = r.system_hash "
                           "join llm_text p on p.hash = r.prompt_hash where r.call_id = any(%s)",
                     (ids,))
    return {r["call_id"]: {"system": r["system"], "prompt": r["prompt"]} for r in rows}


def steps(conn: psycopg.Connection, run_id: str) -> list[dict]:
    """The run's steps in order, texts resolved: a model step carries `system`, `tools` and the
    full `messages` it was sent; a tool step its `tool_call_id`, `name`, `args` and `result`; a
    JSON call step (kind 'call') its `system` and `prompt` (none for a failed call); a 'reject'
    step its `reason`, `detail` and the refused reply's `output_hash`."""
    rows = fetch_all(conn, "select seq, kind, payload, call_id, ts from agent_step "
                           "where run_id = %s order by seq", (run_id,))
    return _resolved(conn, rows)


def request_at(conn: psycopg.Connection, run_id: str, seq: int) -> ChatRequest:
    """The full request model step `seq` of the run sent (phase 2 replays it)."""
    rows = fetch_all(conn, "select seq, kind, payload, call_id, ts from agent_step "
                           "where run_id = %s and seq = %s", (run_id, seq))
    if not rows or rows[0]["kind"] != "model":
        raise LookupError(f"run {run_id!r} has no model step {seq}")
    if rows[0]["payload"].get("expired"):
        raise LookupError(f"run {run_id!r}: the texts of step {seq} expired (store.retention)")
    return request_from(_resolved(conn, rows)[0])


def signal_run_ids(conn: psycopg.Connection, *, older_than: datetime) -> set[str]:
    """Runs started before `older_than` whose subject is a page that has a signal recorded since
    then. A signal stores no run id, so the page is the only link between it and a run."""
    rows = fetch_all(
        conn, "select r.run_id from agent_run r where r.started_at < %s and exists ("
              "select 1 from wiki_event e where e.user_id = r.user_id "
              "and e.type = 'signal_recorded' and e.occurred_at >= %s "
              "and e.payload->'signal'->>'page_id' = r.subject)", (older_than, older_than))
    return {r["run_id"] for r in rows}


def deep_runs_for_page(conn: psycopg.Connection, user_id: str, page_id: str, *, before: datetime,
                       limit: int = 5) -> list[dict]:
    """The newest finished deep write runs of the page started before `before`, newest first:
    `run_id`, `input` (what a replay rebuilds from), `started_at`, `stop`, `text_expired_at` (set:
    retention expired its texts and emptied `input`, so it cannot be replayed). Harness replays are
    left out (their run id prefix or job marker): a replay is never the source of another."""
    return fetch_all(
        conn, "select run_id, input, started_at, stop, text_expired_at from agent_run "
        "where user_id = %s and agent = 'deep.write' and subject = %s and started_at < %s "
        "and finished_at is not null and not starts_with(run_id, %s) "
        "and coalesce(input->'job'->>'trigger', '') <> %s "
        "order by started_at desc, run_id desc limit %s",
        (user_id, page_id, before, REPLAY_RUN_PREFIX, REPLAY_TRIGGER, limit))
