"""Possibly conflicting memories (user ruling 2026-10-29): after a consolidation commits, the
learner's whole memory library is read once by a model and the groups it calls clearly
contradictory wait in 个性化 「可能矛盾的记忆」 for him. Nothing changes until he settles a group:
every entry stays in force as it was.

The library (`library`): what is in force for him — his rules (authority user, legacy_unknown
origins included), the lines he wrote in his profile under 我是谁 / 想学什么 (PROFILE_SECTIONS;
怎么学比较好 is rules now, 已经会的 comes from K) and the active decisions of Memory.
A rule card waiting for him is not in force, so not in it; RSI's own rules neither (their
conflicts with his are the rule contract's, `rules.guard`). Each entry has its id, category,
text, date and source.

When (`ConflictService.check`): after a committed consolidation (`MemoryPorts.consolidate`); a
user's last read library hash (`memory_conflict_scan`) equal to the current one → no model call.
An entry remember_* added between consolidations is read with the next one.

The call: purpose CONFLICTS_PURPOSE, the fast tier, no thinking, the library in one prompt; a
library over TOKEN_BUDGET first goes through a `Narrower` (local vectors; none is wired at
today's scale: such a library is skipped with a warning and read again next time).

Code checks each group the model gives: at least two distinct ids, every id in this user's
library; a set he kept both of before (`kept_both`) or already open is not raised again.

Settling (`resolve`): `keep` one entry (「保留这条」) → the others go out of force the normal way
of each, in one commit: a rule through S3's delete (RemoveRule, actor user: `revoked` recorded),
a decision superseded (its status history appended), a profile line removed as the profile edit
removes it; the group `resolved`; a ledger row RESOLVE_KIND (executed, its undo data what came
out) in the activity log. Undoing it (`ConflictResolveExecutor`) puts each back its own normal
way (a rule re-added by him: `reaccepted`; a decision active again; the profile line with its
provenance) and reopens the group. `keep_both` (「两条都保留」) → `kept_both`, never raised
again. `replace_with` (「他打字改」, the chat's resolve_memory_conflict): every entry of the group
goes out as above and what he said lands in force in the same commit — a rule his (origin
user_chat, authority user, his quote as evidence: what land_preference lands from his words now)
or a line of his profile (provenance explicit_current) — with the same undoable ledger row; its
undo also takes the new one out. (land_preference itself commits alone with its own ledger row:
it cannot take the group's rules out in the same version, so the commit is made here.)

Forgetting a session or an item (memory/forget.py) deletes the groups naming what it removes."""

import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from evowiki.assistant.contracts import (
    MemoryConflictItemOut,
    MemoryConflictOut,
    MemoryConflictSource,
)
from evowiki.llm.models import THINKING_PARAM, LLMCall, LLMRequest
from evowiki.llm.usage import Account
from evowiki.profile.note import STORED
from evowiki.store.db import fetch_all, fetch_one

CONFLICTS_PURPOSE = "memory.conflicts"
CONFLICTS_PROMPT_VERSION = "conflicts@v4"
CONFLICTS_SYSTEM = ("下面是一个学习者的记忆库：他的讲解规则、他在档案里写的话、对话里定下的决定。"
                    "找出明显互相矛盾的条目：同一个人不可能同时都对，或照着一条做就违反另一条。"
                    "说的是不同的事（学过高数、不会 Python），或一条是另一条里的细节（先给例子、讲公式时先解释符号），就能同时成立；"
                    "拿不准的填 can_both_hold=true。why 说内容，不写条目 id。")
TOKEN_BUDGET = 8000                # a library over this is narrowed first (`Narrower`)
GROUP_MAX = 12
GROUPS_MAX = 20

Status = Literal["open", "kept_both", "resolved"]


@dataclass(frozen=True)
class Replacement:
    """「他打字改」: what he said instead, in force at once (his words this turn are his yes and
    the evidence): a teaching rule (`section` None) or a line of his profile (`section`, one of
    PROFILE_SECTIONS). `quote`: his words; `origin_ref`: `<session>/<message>`."""

    text: str
    quote: str
    origin_ref: str
    section: str | None = None
    domain: str | None = None
CATEGORY_ZH = {"rule": "规则", "profile": "档案", "decision": "决定"}
# the profile sections that hold what he wrote himself (profile.note.STORED: 我是谁 / 想学什么;
# 怎么学比较好 is a view of his rules, 已经会的 of K)
PROFILE_SECTIONS = STORED
RESOLVE_KIND = "memory_conflict_resolve"      # the ledger row of 「保留这条」 (undo puts back)
UNDO_CHANGED = "这几条之后又被改过，不能撤销"
_CHAT = ("user_chat",)
_DRAWN = ("memory_backfill", "assistant_drafted")

log = logging.getLogger(__name__)


class ConflictIn(BaseModel):
    # required of the model; the default is for groups already stored and scripted replies
    model_config = ConfigDict(extra="forbid",
                              json_schema_extra=lambda s: s["required"].append("can_both_hold"))

    ids: list[str] = Field(min_length=2, max_length=GROUP_MAX)
    why: str = Field(min_length=1, max_length=300, description="一句话说清哪里矛盾")
    can_both_hold: bool = Field(default=False, description="写完 why 再判断：这几条能同时成立吗")


class ConflictsReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflicts: list[ConflictIn] = Field(default_factory=list, max_length=GROUPS_MAX)


# ---- the library ----

def _rule_source(origin: str) -> MemoryConflictSource:
    if origin in _CHAT:
        return "chat"
    return "consolidation" if origin in _DRAWN else "rule_page"


def library(state: Any, profile: Any, decisions: Iterable[Any],
            rule_dates: Mapping[str, datetime] = {}) -> list[MemoryConflictItemOut]:
    """His memory library (module doc), in a stable order: rules, profile lines,
    decisions. `state`: his wiki state (None: no wiki); `profile`: his Profile (None: none);
    `decisions`: his active decision items; `rule_dates`: spec item id -> when it first saved."""
    from evowiki.profile.note import line_key
    from evowiki.wikiapp.teachspec import effective_spec

    out: list[MemoryConflictItemOut] = []
    if state is not None:
        evidence = getattr(state, "item_evidence", {}) or {}
        for i in effective_spec(state).items:
            if i.authority != "user":
                continue
            out.append(MemoryConflictItemOut(
                id=i.item_id, category="rule",
                text=i.text, date=rule_dates.get(i.item_id), source=_rule_source(i.origin),
                where=i.domain or None, where_kind="domain" if i.domain else None,
                origin_ref=i.origin_ref, origin_deleted=_deleted(i.origin_ref),
                quote=evidence.get(i.item_id)))
    if profile is not None:
        for section, lines in profile.owned.items():
            if section not in PROFILE_SECTIONS:
                continue
            for line in lines:
                key = line_key(section, line)
                meta = profile.fact_meta.get(key)
                ref = meta.origin_ref if meta else None
                out.append(MemoryConflictItemOut(
                    id=key, category="profile", text=line, source="profile", section=section,
                    origin_ref=ref, origin_deleted=_deleted(ref),
                    quote=meta.evidence if meta else None))
    pages = getattr(state, "pages", {}) if state is not None else {}
    for d in decisions:
        said = next((e for e in d.evidence if e.role == "user" and e.quote), None)
        where = None if d.scope_type == "global" else d.scope_id
        if d.scope_type == "page" and where in pages:
            where = pages[where].title
        out.append(MemoryConflictItemOut(
            id=d.item_id, category="decision", text=d.content, date=d.valid_from,
            source="consolidation", where=where,
            where_kind=None if d.scope_type == "global" else d.scope_type,
            origin_ref=f"{said.session_id}/{said.message_id}" if said else None,
            quote=said.quote if said else None))
    return out


def _deleted(ref: str | None) -> bool:
    from evowiki.wikiapp.preferences import DELETED
    return bool(ref) and ref.startswith(DELETED)


def with_titles(entries: Sequence[MemoryConflictItemOut],
                titles: Mapping[str, str]) -> list[MemoryConflictItemOut]:
    """`origin_title` from `titles` (session id -> its conversation title) for each entry whose
    `origin_ref` names a session that is still there."""
    out = []
    for e in entries:
        sid = (e.origin_ref or "").partition("/")[0]
        if e.origin_ref and not e.origin_deleted and sid in titles:
            e = e.model_copy(update={"origin_title": titles[sid]})
        out.append(e)
    return out


def where_text(e: MemoryConflictItemOut) -> str:
    """「只用于…」 as the model and the chat say it ("" for everything)."""
    if e.where is None:
        return ""
    return {"domain": f"只用于「{e.where}」的内容", "page": f"只用于页面「{e.where}」"}.get(
        e.where_kind or "", f"只用于{e.where}")


def library_hash(entries: Sequence[MemoryConflictItemOut]) -> str:
    blob = json.dumps(sorted([e.id, e.category, e.text, e.where or ""] for e in entries),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def render(entries: Sequence[MemoryConflictItemOut]) -> str:
    def line(e: MemoryConflictItemOut) -> str:
        when = e.date.date().isoformat() if e.date else "日期不详"
        where = f"，{where_text(e)}" if e.where else ""
        col = f"，「{e.section}」一栏" if e.section else ""
        return f"- [{e.id}]（{CATEGORY_ZH[e.category]}{col}，{when}{where}）{e.text}"
    return "\n".join(line(e) for e in entries)


def tokens(text: str) -> int:
    """A rough count (Chinese ~1 token per character, other text ~4 characters a token)."""
    cjk = sum(1 for ch in text if "　" <= ch <= "鿿" or "＀" <= ch <= "￯")
    return cjk + (len(text) - cjk) // 4


class Narrower(Protocol):
    """A library over TOKEN_BUDGET: the groups of entries worth reading together (local vector
    neighbours), each under the budget. Not wired at today's scale."""

    def narrow(self, entries: Sequence[MemoryConflictItemOut]) -> list[list[MemoryConflictItemOut]]: ...


def checked(groups: Iterable[ConflictIn], ids: set[str],
            settled: Iterable[frozenset[str]]) -> list[tuple[tuple[str, ...], str]]:
    """The groups that pass (module doc): sorted distinct ids, all known, not settled before
    (`settled`: kept_both and open groups), each once."""
    seen = set(settled)
    out = []
    for g in groups:
        if g.can_both_hold:
            continue
        key = frozenset(g.ids)
        if len(key) < 2 or not key <= ids or key in seen:
            continue
        seen.add(key)
        out.append((tuple(sorted(key)), " ".join(g.why.split())))
    return out


def one_version(spec: Any, ops: Sequence[Any], *, start: Any = None) -> Any:
    """`ops` written by him (actor user, each through the rule checkpoint) as ONE next version of
    `spec` (commit_spec takes exactly previous + 1); `start`: `spec` with earlier changes."""
    from evowiki.wikiapp import teachspec as tspec
    s = spec if start is None else start
    for op in ops:
        s = tspec.write(s, op, actor="user")
    return s.model_copy(update={"version": spec.version + 1})


# ---- storage ----

def drop_with(conn, user_id: str, item_ids: Sequence[str]) -> int:
    """Delete this user's groups naming any of `item_ids` (a forgotten session or item)."""
    if not item_ids:
        return 0
    return conn.execute("delete from memory_conflict where user_id = %s and item_ids && %s",
                        (user_id, sorted(set(item_ids)))).rowcount


def _groups(conn, user_id: str, statuses: Sequence[Status]) -> list[dict[str, Any]]:
    return fetch_all(conn, "select conflict_id, item_ids, why, status, detected_at, resolved_at "
                           "from memory_conflict where user_id = %s and status = any(%s) "
                           "order by detected_at, conflict_id", (user_id, list(statuses)))


def _titles(conn, user_id: str, entries: Sequence[MemoryConflictItemOut]) -> dict[str, str]:
    """session id -> its conversation title (`conversation_title` of its first message), for the
    sessions the entries' words came from that are still his."""
    from evowiki.assistant.session import conversation_title
    sids = sorted({(e.origin_ref or "").partition("/")[0] for e in entries
                   if e.origin_ref and not e.origin_deleted} - {""})
    if not sids:
        return {}
    rows = fetch_all(conn, "select distinct on (e.session_id) e.session_id, e.payload ->> 'text' "
                           "as text from assistant_entry e join assistant_session s using "
                           "(session_id) where s.user_id = %s and e.session_id = any(%s) "
                           "and e.type = 'user' order by e.session_id, e.seq",
                     (user_id, sids))
    return {r["session_id"]: conversation_title(r["text"] or "") for r in rows}


def _rule_dates(conn, user_id: str) -> dict[str, datetime]:
    rows = fetch_all(conn, "select it ->> 'item_id' as id, min(e.occurred_at) as at "
                           "from wiki_event e, jsonb_array_elements(e.payload -> 'spec' -> 'items') it "
                           "where e.user_id = %s and e.type = 'teaching_spec_saved' group by 1",
                     (user_id,))
    return {r["id"]: r["at"] for r in rows}


class ConflictService:
    """`check` (after a consolidation), `open` (GET), `resolve` (POST) over Postgres, the wiki
    repo (S3's spec) and the profile store."""

    def __init__(self, pool: Any, repo: Any, profiles: Any, *, clock: Callable[[], datetime],
                 new_id: Callable[[str], str], narrower: Narrower | None = None):
        self._pool, self._repo, self._profiles = pool, repo, profiles
        self._clock, self._new_id, self._narrower = clock, new_id, narrower

    def _state(self, user_id: str) -> Any:
        try:
            return self._repo.load(user_id).state
        except LookupError:               # no wiki yet: no rules
            return None

    def library(self, user_id: str) -> list[MemoryConflictItemOut]:
        from evowiki.memory.store import PgMemoryStore

        decisions = PgMemoryStore(self._pool).items(user_id, kinds=["decision"],
                                                    statuses=["active"])
        with self._pool.connection() as conn:
            dates = _rule_dates(conn, user_id)
            entries = library(self._state(user_id), self._profiles.get(user_id), decisions,
                              dates)
            return with_titles(entries, _titles(conn, user_id, entries))

    def check(self, user_id: str, gateway: Any, account: Account | None) -> list[LLMCall]:
        """Read his library for conflicts unless it is the one read last time. Returns the model
        calls made. A failed call propagates (the hash is not stored: the next check reads
        again); nothing found is stored as read."""
        entries = self.library(user_id)
        h = library_hash(entries)
        with self._pool.connection() as conn:
            row = fetch_one(conn, "select library_hash from memory_conflict_scan "
                                  "where user_id = %s", (user_id,))
        if row is not None and row["library_hash"] == h:
            return []
        parts = [entries] if len(entries) >= 2 else []
        if parts and tokens(render(entries)) > TOKEN_BUDGET:
            if self._narrower is None:
                log.warning("memory library of %s is over %d tokens and no narrower is wired: "
                            "not checked", user_id, TOKEN_BUDGET)
                return []
            parts = self._narrower.narrow(entries)
        found: list[ConflictIn] = []
        calls: list[LLMCall] = []
        fast = None if account is None else account.model_copy(update={"tier_pin": "fast"})
        for part in parts:
            request = LLMRequest(purpose=CONFLICTS_PURPOSE, prompt_version=CONFLICTS_PROMPT_VERSION,
                                 subject=user_id, system=CONFLICTS_SYSTEM, prompt=render(part),
                                 params={THINKING_PARAM: False})
            parsed, call = gateway.call("generator", request, ConflictsReply,
                                        account=fast if gateway.metered else None)
            found += parsed.conflicts
            calls.append(call)
        now = self._clock()
        with self._pool.connection() as conn, conn.transaction():
            settled = [frozenset(r["item_ids"])
                       for r in _groups(conn, user_id, ("open", "kept_both"))]
            text = {e.id: e.text for e in entries}
            for ids, why in checked(found, set(text), settled):
                # one contradiction, one group: rescans with a new rule named {a,b}, then {a,b,c},
                # then {b,c}: he was shown three groups to settle one thing (live 2026-11-05)
                if any(frozenset(ids) <= s for s in settled):
                    continue
                conn.execute("delete from memory_conflict where user_id = %s and status = 'open' "
                             "and item_ids <@ %s", (user_id, list(ids)))
                settled.append(frozenset(ids))
                for i in ids:                # he reads this line: an id the model wrote is its words
                    why = why.replace(i, f"「{text[i]}」")
                conn.execute("insert into memory_conflict (conflict_id, user_id, item_ids, why, "
                             "status, detected_at) values (%s, %s, %s, %s, 'open', %s) "
                             "on conflict do nothing",
                             (self._new_id("mc"), user_id, list(ids), why, now))
            conn.execute("insert into memory_conflict_scan (user_id, library_hash, scanned_at) "
                         "values (%s, %s, %s) on conflict (user_id) do update set "
                         "library_hash = excluded.library_hash, scanned_at = excluded.scanned_at",
                         (user_id, h, now))
        return calls

    def open(self, user_id: str) -> list[MemoryConflictOut]:
        """His open groups whose entries all still exist (one gone some other way is not shown)."""
        by_id = {e.id: e for e in self.library(user_id)}
        with self._pool.connection() as conn:
            rows = _groups(conn, user_id, ("open",))
        return [MemoryConflictOut(conflict_id=r["conflict_id"], why=r["why"],
                            detected_at=r["detected_at"],
                            items=[by_id[i] for i in r["item_ids"]])
                for r in rows if all(i in by_id for i in r["item_ids"])]

    def resolve(self, user_id: str, conflict_id: str, *, keep: str | None = None,
                keep_both: bool = False,
                replace_with: Replacement | None = None) -> tuple[Status, str | None]:
        """Settle one open group (module doc): its new status and, for `keep` / `replace_with`,
        the ledger row that undoes it. LookupError: no such open group of his; ValueError:
        `keep` is not in it, or a bad replacement."""
        if sum((keep is not None, keep_both, replace_with is not None)) != 1:
            raise ValueError("give exactly one of keep / keep_both / replace_with")
        with self._pool.connection() as conn:
            row = fetch_one(conn, "select item_ids, status from memory_conflict "
                                  "where user_id = %s and conflict_id = %s",
                            (user_id, conflict_id))
        if row is None or row["status"] != "open":
            raise LookupError(conflict_id)
        now = self._clock()
        status: Status = "kept_both" if keep_both else "resolved"

        def settle(conn) -> None:
            n = conn.execute("update memory_conflict set status = %s, resolved_at = %s "
                             "where conflict_id = %s and status = 'open'",
                             (status, now, conflict_id)).rowcount
            if n != 1:
                raise LookupError(conflict_id)

        if keep_both:
            with self._pool.connection() as conn, conn.transaction():
                settle(conn)
            return status, None
        if replace_with is not None:
            if (replace_with.section is not None
                    and replace_with.section not in PROFILE_SECTIONS):
                raise ValueError(f"not a profile section: {replace_with.section}")
            by_id = {e.id: e for e in self.library(user_id)}
            others = [by_id[i] for i in row["item_ids"] if i in by_id]
            return status, self._take_out(user_id, conflict_id, others, None, now, settle,
                                          added=replace_with)
        if keep not in row["item_ids"]:
            raise ValueError(f"{keep} is not in {conflict_id}")
        by_id = {e.id: e for e in self.library(user_id)}
        others = [by_id[i] for i in row["item_ids"] if i != keep and i in by_id]
        kept = by_id.get(keep)
        return status, self._take_out(user_id, conflict_id, others, kept, now, settle)

    def _take_out(self, user_id: str, conflict_id: str, others: Sequence[MemoryConflictItemOut],
                  kept: MemoryConflictItemOut | None, now: datetime,
                  settle: Callable[[Any], None], added: Replacement | None = None) -> str:
        """The other entries out of force, each its own normal way, `added` in force, with
        `settle` and the RESOLVE_KIND ledger row (its undo data: what came out and what came
        in), in one commit. Returns the row's action id."""
        from evowiki.agent.effects import payload_hash
        from evowiki.bot.executors_profile import _without
        from evowiki.memory.store import set_status_sql
        from evowiki.profile.note import FactMeta, add_fact, line_key
        from evowiki.store import bot as ledger_store
        from evowiki.store.bot import ActionRow
        from evowiki.wikiapp import teachspec as tspec
        from evowiki.wikiapp.guidelines import stated_evidence
        from evowiki.wikiapp.teach import commit_spec

        out_ids = {e.id for e in others}
        new_rule = added is not None and added.section is None
        snap = (self._repo.load(user_id)
                if new_rule or any(e.category == "rule" for e in others) else None)
        rules = ([i for i in tspec.effective_spec(snap.state).items if i.item_id in out_ids]
                 if snap is not None else [])
        lines = {e.id for e in others if e.category == "profile"}
        decisions = [e.id for e in others if e.category == "decision"]
        by = kept.id if kept is not None and kept.category == "decision" else None
        taken = []
        if lines:
            profile = self._profiles.get(user_id)
            for sec, owned in profile.owned.items():
                for line in owned:
                    key = line_key(sec, line)
                    if key in lines:
                        meta = profile.fact_meta.get(key)
                        taken.append([sec, line, meta.model_dump(mode="json") if meta else None])
        why = stated_evidence(added.quote) if added is not None else ""
        new: dict[str, Any] = {}
        if added is not None and new_rule:
            new = {"rule": self._new_id("si"), "text": " ".join(added.text.split())}
        elif added is not None:
            new = {"line": [added.section, " ".join(added.text.split())]}
        content = {"conflict_id": conflict_id, "keep": kept.id if kept else None,
                   "others": [e.id for e in others],
                   **({"replace_with": added.text, "quote": added.quote,
                       "origin_ref": added.origin_ref} if added is not None else {})}
        undo = {"conflict_id": conflict_id,
                "rules": [i.model_dump(mode="json") for i in rules],
                "decisions": decisions, "lines": taken, "added": new}
        summary = (f"保留「{kept.text}」，撤下另外 {len(others)} 条" if kept
                   else f"改成「{added.text}」，撤下原来的 {len(others)} 条" if added
                   else "撤下矛盾的记忆")
        action = ActionRow(action_id=self._new_id("act"), user_id=user_id, kind=RESOLVE_KIND,
                           scope="preference", level="auto_undo", status="executed",
                           source="user", summary=summary[:200], why="", content=content,
                           content_hash=payload_hash(content),
                           idempotency_key=f"{RESOLVE_KIND}:{conflict_id}:{self._new_id('k')}",
                           created_at=now, decided_at=now, undo=undo,
                           result={"others": content["others"]})

        def offline(conn) -> None:
            for d in decisions:
                set_status_sql(conn, d, "superseded", at=now, by="conflict", superseded_by=by)
            if lines:
                def change(p):
                    for sec, owned in list(p.owned.items()):
                        for line in owned:
                            if line_key(sec, line) in lines and line in p.owned.get(sec, ()):
                                p = _without(p, sec, line, "")
                    return p
                self._profiles.update_in(conn, user_id, change)
            if "line" in new:
                sec, text = new["line"]
                meta = FactMeta(provenance="explicit_current", origin_ref=added.origin_ref,
                                evidence=why, action_id=action.action_id)
                self._profiles.update_in(conn, user_id,
                                         lambda p: add_fact(p, sec, text, "", meta))
            if not ledger_store.insert_action(conn, action):
                raise RuntimeError(f"ledger key {action.idempotency_key} taken")
            settle(conn)

        if snap is None or not (rules or new_rule):
            with self._pool.connection() as conn, conn.transaction():
                offline(conn)
            return action.action_id

        def change(s):
            ops: list[Any] = [tspec.RemoveRule(r.item_id) for r in rules]
            if new_rule:
                ops.append(tspec.AddRule(new["rule"], new["text"], domain=added.domain,
                                         origin="user_chat", authority="user",
                                         origin_ref=added.origin_ref))
            return one_version(s, ops)
        commit_spec(self._repo, snap, change, actor="user", action="guideline", now=now,
                    new_id=self._new_id, via="chat" if added else "ui",
                    origin=added.origin_ref if added else action.action_id,
                    evidence={new["rule"]: why} if new_rule else None, commit_extra=[offline])
        return action.action_id


class ConflictResolveExecutor:
    """The ledger's executor of RESOLVE_KIND rows (written executed by `resolve`): undo puts
    back what came out, each its own normal way, and reopens the group, in one commit: a rule
    re-added by him (S3 records `reaccepted`), a decision active again (history appended), a
    profile line with its provenance. Anything changed since -> refused, nothing written."""

    def __init__(self, pool: Any, repo: Any, profiles: Any, *, clock: Callable[[], datetime],
                 new_id: Callable[[str], str]):
        self._pool, self._repo, self._profiles = pool, repo, profiles
        self._clock, self._new_id = clock, new_id

    def execute(self, user_id: str, action: Any, effect: Callable[[Any], None]) -> dict:
        from evowiki.bot.actions import ActionRefused
        raise ActionRefused("这条已经做过了")

    def rejected(self, user_id: str, action: Any) -> None:
        return None

    def undo(self, user_id: str, action: Any) -> dict:
        from evowiki.bot.actions import ActionRefused
        from evowiki.memory.store import set_status_sql
        from evowiki.profile.note import FactMeta, line_key
        from evowiki.store import bot as ledger_store
        from evowiki.teach.contracts import SpecItem
        from evowiki.wikiapp import teachspec as tspec
        from evowiki.wikiapp.teach import commit_spec

        u = action.undo or {}
        if not u:
            raise ActionRefused("这条修改没有保存撤销数据，不能撤销")
        now = self._clock()
        rules = [SpecItem.model_validate(r) for r in u.get("rules") or ()]
        decisions = list(u.get("decisions") or ())
        lines = [(sec, line, meta) for sec, line, meta in u.get("lines") or ()]
        added = u.get("added") or {}
        snap = self._repo.load(user_id) if rules or "rule" in added else None
        if snap is not None:
            live = {i.item_id: i for i in tspec.effective_spec(snap.state).items}
            if any(r.item_id in live for r in rules):
                raise ActionRefused(UNDO_CHANGED)
            if "rule" in added and (added["rule"] not in live
                                    or live[added["rule"]].text != added["text"]):
                raise ActionRefused(UNDO_CHANGED)

        def back(conn) -> None:
            for d in decisions:
                r = fetch_one(conn, "select status from memory_item where user_id = %s "
                                    "and item_id = %s", (user_id, d))
                if r is None or r["status"] != "superseded":
                    raise ActionRefused(UNDO_CHANGED)
                set_status_sql(conn, d, "active", at=now, by="conflict_undo", superseded_by=None)
            if "line" in added:
                from evowiki.bot.executors_profile import _without
                sec, text = added["line"]

                def drop(p):
                    if text not in p.owned.get(sec, ()):
                        raise ActionRefused(UNDO_CHANGED)
                    return _without(p, sec, text, "")
                self._profiles.update_in(conn, user_id, drop)
            if lines:
                def change(p):
                    owned = {k: list(v) for k, v in p.owned.items()}
                    metas = dict(p.fact_meta)
                    for sec, line, meta in lines:
                        if line not in owned.setdefault(sec, []):
                            owned[sec].append(line)
                        if meta:
                            metas[line_key(sec, line)] = FactMeta.model_validate(meta)
                    return p.model_copy(update={"owned": {k: tuple(v) for k, v in owned.items()},
                                                "fact_meta": metas})
                self._profiles.update_in(conn, user_id, change)
            n = conn.execute("update memory_conflict set status = 'open', resolved_at = null "
                             "where user_id = %s and conflict_id = %s and status = 'resolved'",
                             (user_id, u["conflict_id"])).rowcount
            if n != 1:
                raise ActionRefused(UNDO_CHANGED)
            ledger_store.set_action_status(conn, action.action_id, status="undone", at=now,
                                           from_statuses=("executed",))

        if snap is None:
            with self._pool.connection() as conn, conn.transaction():
                back(conn)
        else:
            def restore(s):
                base = s
                if "rule" in added:
                    s = tspec.remove_item(s, added["rule"])
                return one_version(base, [tspec.AddRule(r.item_id, r.text, r.scope, r.domain,
                                                        origin=r.origin, authority="user",
                                                        origin_ref=r.origin_ref) for r in rules],
                                   start=s)
            commit_spec(self._repo, snap, restore, actor="user", action="guideline", now=now,
                        new_id=self._new_id, via="ledger_undo", origin=action.action_id,
                        commit_extra=[back])
        return {"restored": [r.item_id for r in rules] + decisions
                + [line_key(sec, line) for sec, line, _ in lines],
                "removed": added.get("rule") or (line_key(*added["line"]) if "line" in added
                                                 else None)}
