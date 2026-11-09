"""Forgetting one assistant session in the harness-RSI records (memory plan Task 12–14; migration 0082
forget gate). `forget_session` runs inside the caller's forget transaction: the caller has called
`evowiki_forget_open(request_id)`, which opens the gate; this function sets no flag, touches no
trigger and never commits. Every statement goes through the 0082 guards (only the redaction
columns may change) and their audit.

What a session leaves in harness RSI, and what happens to it (the session's runs are
`assistant:<session>:<message>`, so its marker is `assistant:<session>:`):
- the rounds it took part in: a round whose cases name one of its turns, or whose improver steps
  mention the marker (the improver read its signals or traces). For each such round, round-wide,
  because the session's words can sit inside any free text the improver or the judge wrote:
  `reason`, every candidate's `rationale`, the decision's `rationale` (and a code-patch request's
  `goal` / `title`) become REDACTED; the session's turn cases keep their kind but lose their ids
  and signal ids (DELETED), and those signal ids leave `signal_ids`; the comparisons of its turns
  lose `quote` / `reason`. `diff` stays: it is what the change set, and `undo_change` needs it.
- the changes of those rounds: `reason` (the improver's why) becomes REDACTED; `diff` stays.
- the experience book: every lesson of those rounds keeps its verdict and change; its evidence
  gets `source_deleted: true` (situation is left as it is: it holds no session text).
- deleted outright: the replay runs of its turns (`harness-replay:…:assistant:<session>:…`) and
  the improver runs of those rounds (their tool results carry the session's text), with their
  steps; the llm_call / llm_attempt / llm_request rows of those runs' model steps, and every
  llm_call / llm_attempt whose subject names one of its turns (the judge's comparisons); the
  llm_text rows only they referenced.

- the session's own words wherever the improver copied them into a change (`quotes`: the user
  messages the memory side's taint closure found, or else this session's `type='user'` entries,
  read in the same transaction, so this step runs before the entries are deleted). Each quote is
  cut into sentence / clause fragments of at least MIN_FRAGMENT characters; a fragment found in a
  string value (compared without whitespace, `textsim.find_squashed`) is replaced by PLACEHOLDER,
  in every change of the user (not only the rounds above: a copied sentence carries no session id):
  `harness_change.diff` (and the change gets `source_deleted: true`, so `undo_change` refuses it:
  its before / after can no longer restore anything), `harness_round.diff` and the candidates'
  diffs, `harness_experience.change`, `harness_version` before / after / why, and the pack in
  force (`harness_value`): a value that quotes the session is written again as a new pack version
  without the quote (source "rollback", why LIVE_WHY), before the history is redacted, so that
  new version's `before` is redacted too.

Idempotent (the memory side's reconcile runs it again on a committed request): a deleted row is
not found again, a value already redacted is left alone, a lesson already marked is skipped.
Returns the rows handled per table. Known limit: a signal of the session that never became a case
(left out by the case cap) is not linked to the session in the round's record, so its id stays in
`signal_ids` (an opaque id, no text)."""

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import psycopg

from evowiki.store import harness as pack_store
from evowiki.store.db import fetch_all
from evowiki.teach.textsim import find_squashed, squash

REDACTED = "（来源已删除）"
DELETED = "deleted"
PLACEHOLDER = "（已删除的原话）"
LIVE_WHY = "来源会话已删除，去掉了其中的原话"
MIN_FRAGMENT = 8                       # characters (whitespace not counted) a copied piece needs
_CUT = re.compile(r"[。！？!?；;，,、：:\n“”「」\"]+")


def _marker(session_id: str) -> str:
    return f"assistant:{session_id}:"


def _like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _mine(value: Any, marker: str) -> bool:
    return isinstance(value, str) and marker in value


def _redact_cases(cases: dict, marker: str) -> tuple[dict, set[str]]:
    """The cases with the session's turns anonymised, and the signal ids they carried."""
    dropped: set[str] = set()
    out = dict(cases)
    for part in ("problem", "anchors"):
        rows = []
        for c in cases.get(part) or []:
            if _mine(c.get("case_id"), marker) or _mine(c.get("run_id"), marker):
                dropped.update(c.get("signal_ids") or ())
                c = {**c, "case_id": DELETED, "run_id": DELETED, "signal_ids": []}
            rows.append(c)
        out[part] = rows
    for part in ("unusable", "skipped"):
        if part in cases:
            out[part] = [{**u, "id": DELETED} if _mine(u.get("id"), marker) else u
                         for u in cases[part] or []]
    return out, dropped


def _redact_decision(decision: dict) -> dict:
    out = dict(decision)
    if out.get("rationale"):
        out["rationale"] = REDACTED
    patch = out.get("patch")
    if isinstance(patch, dict):
        out["patch"] = {**patch, **{k: REDACTED for k in ("goal", "title") if patch.get(k)}}
    return out


def _rounds(conn: psycopg.Connection, user_id: str, marker: str) -> list[dict]:
    like = _like(marker)
    return fetch_all(
        conn, "select * from harness_round where user_id = %s and (cases::text like %s or "
        "comparisons::text like %s or exists (select 1 from agent_step s where s.run_id = "
        "'harness:' || harness_round.round_id || ':improver' and s.payload::text like %s)) "
        "order by started_at", (user_id, like, like, like))


def _round_update(r: dict, marker: str) -> dict[str, Any]:
    """The redacted columns of round `r` that differ from what is stored."""
    new: dict[str, Any] = {}
    if r["reason"] and r["reason"] != REDACTED:
        new["reason"] = REDACTED
    dropped: set[str] = set()
    if r["cases"]:
        cases, dropped = _redact_cases(r["cases"], marker)
        if cases != r["cases"]:
            new["cases"] = cases
    if r["signal_ids"] and dropped:
        ids = [i for i in r["signal_ids"] if i not in dropped]
        if ids != r["signal_ids"]:
            new["signal_ids"] = ids
    if r["candidates"]:
        cands = [{**c, "rationale": REDACTED} if c.get("rationale") else c
                 for c in r["candidates"]]
        if cands != r["candidates"]:
            new["candidates"] = cands
    if r["decision"]:
        dec = _redact_decision(r["decision"])
        if dec != r["decision"]:
            new["decision"] = dec
    if r["comparisons"]:
        cmps = [{**c, "quote": REDACTED, "reason": REDACTED} if _mine(c.get("case_id"), marker)
                and (c.get("quote") != REDACTED or c.get("reason") != REDACTED) else c
                for c in r["comparisons"]]
        if cmps != r["comparisons"]:
            new["comparisons"] = cmps
    return new


def fragments(quotes: Sequence[str]) -> tuple[str, ...]:
    """The pieces of `quotes` worth matching: sentences / clauses of ≥ MIN_FRAGMENT characters
    (whitespace not counted), longest first, so a whole clause goes before a part of it."""
    out = {f.strip() for q in quotes for f in _CUT.split(q) if len(squash(f)) >= MIN_FRAGMENT}
    return tuple(sorted(out, key=lambda f: (-len(squash(f)), f)))


def scrub(value: Any, frags: Sequence[str]) -> Any:
    """`value` (JSON) with every fragment in its strings replaced by PLACEHOLDER."""
    if isinstance(value, str):
        for f in frags:
            while (span := find_squashed(f, value)) is not None:
                value = value[:span[0]] + PLACEHOLDER + value[span[1]:]
        return value
    if isinstance(value, list):
        return [scrub(v, frags) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v, frags) for k, v in value.items()}
    return value


def _session_quotes(conn: psycopg.Connection, session_id: str) -> list[str]:
    return [r["t"] for r in fetch_all(
        conn, "select payload->>'text' as t from assistant_entry where session_id = %s and "
        "type = 'user' and payload->>'text' is not null", (session_id,))]


def _j(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _scrub_quotes(conn: psycopg.Connection, user_id: str, frags: Sequence[str],
                  now: datetime) -> dict[str, int]:
    """Module doc, the session's own words. The pack in force first, then the history."""
    n = dict.fromkeys(("harness_value", "harness_change_diff", "harness_round_diff",
                       "harness_experience_change", "harness_version"), 0)
    if not frags:
        return n
    pack_store.lock_key(conn, user_id)
    for r in fetch_all(conn, "select key, value from harness_value where user_id = %s "
                             "order by key", (user_id,)):
        new = scrub(r["value"], frags)
        if new != r["value"]:
            pack_store.set_value(conn, user_id, r["key"], new, source="rollback", why=LIVE_WHY,
                                 now=now)
            n["harness_value"] += 1
    for r in fetch_all(conn, "select change_id, diff from harness_change where user_id = %s",
                       (user_id,)):
        new = scrub(r["diff"], frags)
        if new != r["diff"]:
            conn.execute("update harness_change set diff = %s where change_id = %s",
                         (_j({**new, "source_deleted": True}), r["change_id"]))
            n["harness_change_diff"] += 1
    for r in fetch_all(conn, "select round_id, diff, candidates from harness_round "
                             "where user_id = %s", (user_id,)):
        diff, cands = scrub(r["diff"], frags), scrub(r["candidates"], frags)
        if diff != r["diff"] or cands != r["candidates"]:
            conn.execute("update harness_round set diff = %s, candidates = %s where round_id = %s",
                         (_j(diff), _j(cands), r["round_id"]))
            n["harness_round_diff"] += 1
    for r in fetch_all(conn, "select exp_id, change from harness_experience where user_id = %s",
                       (user_id,)):
        new = scrub(r["change"], frags)
        if new != r["change"]:
            conn.execute("update harness_experience set change = %s where exp_id = %s",
                         (_j(new), r["exp_id"]))
            n["harness_experience_change"] += 1
    for r in fetch_all(conn, "select version, before, after, why from harness_version "
                             "where user_id = %s", (user_id,)):
        b, a, w = scrub(r["before"], frags), scrub(r["after"], frags), scrub(r["why"], frags)
        if (b, a, w) != (r["before"], r["after"], r["why"]):
            conn.execute("update harness_version set before = %s, after = %s, why = %s "
                         "where user_id = %s and version = %s",
                         (_j(b), _j(a), w, user_id, r["version"]))
            n["harness_version"] += 1
    return n


def forget_session(conn: psycopg.Connection, user_id: str, session_id: str, *,
                   quotes: Sequence[str] | None = None,
                   now: datetime | None = None) -> dict[str, int]:
    """Deletes and redacts what harness RSI keeps of session `session_id` (module doc), inside the
    caller's open forget transaction. `quotes`: the user's words to remove wherever a change copied
    them (default: this session's user entries, so call it before they are deleted). Returns rows
    handled per table."""
    marker = _marker(session_id)
    like = _like(marker)
    frags = fragments(_session_quotes(conn, session_id) if quotes is None else quotes)
    n: dict[str, int] = _scrub_quotes(conn, user_id, frags, now or datetime.now(UTC))

    rounds = _rounds(conn, user_id, marker)
    round_ids = [r["round_id"] for r in rounds]
    for r in rounds:
        new = _round_update(r, marker)
        if new:
            sets = ", ".join(f"{k} = %s" for k in new)
            vals = [v if k == "reason" else json.dumps(v, ensure_ascii=False)
                    for k, v in new.items()]
            conn.execute(f"update harness_round set {sets} where round_id = %s",
                         (*vals, r["round_id"]))
            n["harness_round"] = n.get("harness_round", 0) + 1
    if round_ids:
        n["harness_change"] = conn.execute(
            "update harness_change set reason = %s where round_id = any(%s) and reason is not null "
            "and reason <> %s", (REDACTED, round_ids, REDACTED)).rowcount
        n["harness_experience"] = conn.execute(
            "update harness_experience set evidence = evidence || '{\"source_deleted\": true}' "
            "where user_id = %s and evidence->>'round_id' = any(%s) "
            "and not coalesce((evidence->>'source_deleted')::boolean, false)",
            (user_id, round_ids)).rowcount

    runs = [r["run_id"] for r in fetch_all(
        conn, "select run_id from agent_run where user_id = %s and ((starts_with(run_id, "
        "'harness-replay:') and run_id like %s) or run_id = any(%s))",
        (user_id, like, [f"harness:{rid}:improver" for rid in round_ids]))]
    calls = [r["call_id"] for r in fetch_all(
        conn, "select distinct call_id from agent_step where run_id = any(%s) and call_id is not "
        "null", (runs,))]
    calls += [r["id"] for r in fetch_all(
        conn, "select id from llm_call where subject like %s and id <> all(%s)", (like, calls))]
    hashes = [r["h"] for r in fetch_all(
        conn, "select payload->>'system_hash' as h from agent_step where run_id = any(%s) "
        "union select payload->>'tools_hash' from agent_step where run_id = any(%s) "
        "union select jsonb_array_elements_text(payload->'messages') from agent_step "
        "where run_id = any(%s) and jsonb_typeof(payload->'messages') = 'array' "
        "union select system_hash from llm_request where call_id = any(%s) "
        "union select prompt_hash from llm_request where call_id = any(%s)",
        (runs, runs, runs, calls, calls)) if r["h"]]

    n["agent_step"] = conn.execute("delete from agent_step where run_id = any(%s)",
                                   (runs,)).rowcount
    n["agent_run"] = conn.execute("delete from agent_run where run_id = any(%s)", (runs,)).rowcount
    n["llm_request"] = conn.execute("delete from llm_request where call_id = any(%s)",
                                    (calls,)).rowcount
    n["llm_attempt"] = conn.execute(
        "delete from llm_attempt where subject like %s or input_hash in "
        "(select input_hash from llm_call where id = any(%s))", (like, calls)).rowcount
    n["llm_call"] = conn.execute("delete from llm_call where id = any(%s)", (calls,)).rowcount
    n["llm_text"] = conn.execute(
        "delete from llm_text t where t.hash = any(%s) and not exists (select 1 from ("
        "select payload->>'system_hash' as h from agent_step "
        "union all select payload->>'tools_hash' from agent_step "
        "union all select jsonb_array_elements_text(payload->'messages') from agent_step "
        "where jsonb_typeof(payload->'messages') = 'array' "
        "union all select system_hash from llm_request "
        "union all select prompt_hash from llm_request) refs where refs.h = t.hash)",
        (hashes,)).rowcount
    return {k: v for k, v in n.items()}

def forget_rounds(conn: psycopg.Connection, user_id: str, round_ids: Sequence[str]) -> dict[str, int]:
    """Erase derived generation content reached through indexed round provenance.

    Applied change identities/statuses stay auditable. Their actual operational diff remains
    available for undo; source-bearing explanations and the improver's copies are removed.
    """
    from evowiki.memory.forget import _DESCENDANTS, delete_runs

    rows = fetch_all(conn, "select round_id,improver_run_id from harness_round "
                     "where user_id=%s and round_id=any(%s)", (user_id, list(round_ids)))
    ids = [r['round_id'] for r in rows]
    if not ids:
        return {}
    counts = {}
    counts['harness_round'] = conn.execute(
        "update harness_round set reason=%s,finding=%s,signal_ids='[]',"
        "cases='{\"source_deleted\":true}',candidates='[]',comparisons='[]',"
        "decision='{\"source_deleted\":true}',diff='{\"source_deleted\":true}' "
        "where user_id=%s and round_id=any(%s)", (REDACTED, REDACTED, user_id, ids)).rowcount
    counts['harness_change'] = conn.execute(
        "update harness_change set reason=%s,diff=diff || '{\"source_deleted\":true}' "
        "where user_id=%s and round_id=any(%s)", (REDACTED, user_id, ids)).rowcount
    counts['harness_experience'] = conn.execute(
        "update harness_experience set situation='{\"source_deleted\":true}',change='{}',"
        "evidence=jsonb_build_object('source_deleted',true,'round_id',evidence->>'round_id',"
        "'change_id',evidence->>'change_id') where user_id=%s and "
        "(evidence->>'round_id'=any(%s) or evidence->>'change_id' in "
        "(select change_id from harness_change where user_id=%s and round_id=any(%s)))",
        (user_id, ids, user_id, ids)).rowcount
    counts['harness_gate'] = conn.execute(
        "update harness_gate set reasons='[]',detail='{\"source_deleted\":true}' "
        "where user_id=%s and round_id=any(%s)", (user_id, ids)).rowcount
    roots = [r['improver_run_id'] for r in rows if r['improver_run_id']]
    runs = [r['run_id'] for r in fetch_all(conn, _DESCENDANTS.format(
        roots="run_id=any(%(roots)s) or subject=any(%(ids)s) or exists "
              "(select 1 from unnest(%(ids)s::text[]) rid where "
              "starts_with(agent_run.run_id,'harness:' || rid || ':'))"),
        {'u': user_id, 'roots': roots, 'ids': ids})]
    conn.execute("select pg_advisory_xact_lock(hashtext('trace:prune'))")
    counts.update(delete_runs(conn, user_id, runs, subjects=ids))
    return counts
