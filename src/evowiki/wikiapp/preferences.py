"""Where a preference or a fact about the learner lands (stage 3 T6; rule provenance contract
§5, §7 #4 / #5 / #11 / #23; memory spec §1.1, §2.3): `land_preference`, the one function the chat's
`remember_preference` / `remember_about_me` and Memory's consolidation call.

What lands where is decided here, in code, by the provenance (memory contract `Provenance`); no
Skill text, tool description or harness key changes it:

- `explicit_current` — his own words in THIS message (`current`: the evidence names this message,
  role user, and its quote is a verbatim piece of it, ≥ MIN_QUOTE characters or the whole message):
  a teaching rule lands active at once (origin `user_chat`, authority `user`, through
  `teachspec.write(actor="user")`), with a ledger row of kind STATED_KIND (executed, undoable: the
  chat's quiet 「记下了」 line). A fact about him needs a profile store (`profile`); the chat's
  `remember_about_me` records its fact through its own ledger row instead.
- `explicit_history` (words from an earlier message: Memory's backfill, or a quote from an earlier
  turn) and `inferred` (no words of his: what the assistant concluded) — ALWAYS pending (user
  ruling 2026-10-28; `history_policy` can only be "pending"): a teaching rule becomes a pending
  spec item (origin `memory_backfill` / `assistant_drafted`, authority `pending`, through
  `teachspec.write(actor="system")`; a pending item teaches nothing, `teachspec.applicable`) and a
  proposed ledger row of kind PENDING_KIND in the same commit; a fact becomes a proposed
  `profile_fact` row. Confirming executes the row (the card's 确认, or `confirm` after `offer`);
  rejecting or undoing moves it out of proposed / executed. An inferred record is never turned
  active in place: only his confirmation does that.
- `observed` — refused: a preference is never an observation (K evidence goes through K).
- `pending=True` → pending whatever the provenance.

One rule, one card: a text close to one of the spec's rules — a near-duplicate
(`textsim.near_duplicate`), or `same_as` (the caller's semantic judge, `teach.rule_judge` verdict
"same", said it restates that item) — writes nothing and returns that rule: a pending one as
pending with its proposed card's `pending_action_id`, one in force as active (no `action_id`: no
row was written); either is marked `existing`. A pending twin whose card is gone is refused
(STATED_PENDING). His own words now (explicit_current) with no near-duplicate ask `judge` (the
same `teach.rule_judge` verdict "same" Memory uses) against his rules in force and pending cards;
a failed judge call is logged and the rule lands as new (never lost for want of the check).

Refused also: an empty or too long text, evidence without his words (for explicit_*), a non-global
scope (the spec has no page / goal / concept scope), and — no resurrection — a near-duplicate of a
rule he rejected or revoked (`guidelines.blocked_rule_texts`, `textsim.near_duplicate`), except his
own restatement in this message (explicit_current), which lands and is recorded `reaccepted`.

`replaces` (a spec item id; for a fact, the owned profile line it updates): the new rule
supersedes it. Active: one version, the old item goes
(`revoked`, its provenance, evidence and versions kept); undo restores it. Pending: the old item
stays in force until he confirms the new one.

`idempotency_key`: the ledger row's key (`pref:<key>`): the same key again while its row is still
proposed returns that row (`pending_action_id`), never a second one; an executed one returns the
item as active; a rejected / undone one is refused.

Every write needs the database (the ledger row lands in the wiki commit): a repo without a pool
raises RuntimeError (Memory's lander keeps the candidate for a retry). Never a silent partial."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from evowiki.agent.effects import payload_hash
from evowiki.memory.contracts import EvidenceRef, MemoryScope, Provenance
from evowiki.profile.note import FACT_SECTIONS, WHO, add_fact, line_key
from evowiki.store import bot as ledger_store
from evowiki.store.bot import ActionRow
from evowiki.teach.rule_judge import Against
from evowiki.teach.textsim import near_duplicate, squash
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.guidelines import (
    MAX_TEXT,
    STATED_EMPTY,
    STATED_LONG,
    STATED_PENDING,
    STATED_REJECTED,
    blocked_rule_texts,
    stated_evidence,
)
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.repo import Repo, Snapshot
from evowiki.wikiapp.teach import commit_spec

Kind = Literal["teaching", "about_me"]

STATED_KIND = "stated_rule"      # a rule landed from his words in this message (executed, undo)
PENDING_KIND = "rule"            # a rule waiting for his confirmation (a proposed card)
FACT_KIND = "profile_fact"       # a fact about him (`bot.executors_profile`)
RULE_SCOPE = "teaching_params"
FACT_SCOPE = "suggestion"
SOURCE = "assistant"             # agent_action.source: the assistant's (Memory's cards show there)
KEY_PREFIX = "pref:"
STATED_TITLE = "记下了："         # the quiet chat line 「记下了：<rule>」
PENDING_TITLE = "记一条讲解规则："  # the card (the plugin's rule card shows what follows)
FACT_TITLE = "记进档案："
FACT_KNOWN = "档案里已经有这句话，或者有一条相近的在等他确认"
MIN_QUOTE = 4

log = logging.getLogger(__name__)

NOT_QUOTED_NOW = "quote 要一字不改地抄他这一条消息里的话（至少 4 个字，或者就是他整条消息）"
NO_WORDS = "要有他自己说这件事的原话（哪条消息、原话）"
OBSERVED = "偏好不能从观察里记：只记他自己说的"
NOT_GLOBAL = "现在只能记对所有页面都适用的原则"
NO_RULE = "没有这条要替换的规则：{}"
DECIDED = "这条之前已经处理过（他确认、拒绝或撤销过），不再重记"
NO_SECTION = "档案里没有这一栏：{}"
NO_PROFILE = "现在记不了档案"


class Landed(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["active", "pending", "refused"]
    item_id: str | None = None             # active / pending: spec item id, or profile line key
    pending_action_id: str | None = None   # pending: the proposed ledger row (its card)
    superseded: tuple[str, ...] = ()       # active: the item ids `replaces` took out of force
    reason: str | None = None              # refused: why (the tool error text)
    action_id: str | None = None           # the ledger row written (active: the undo row)
    existing: bool = False                 # an existing rule returned (nothing new written)


@dataclass(frozen=True)
class CurrentMessage:
    """The learner message being answered: its id and words (it, and his replies to this turn's
    own questions). An explicit_current quote must come from here."""

    message_id: str
    words: tuple[str, ...]


def _refused(reason: str) -> Landed:
    return Landed(status="refused", reason=reason)


def message_ref(session_id: str | None, message_id: str) -> str:
    """origin_ref of words in a session message: `<session>/<message>` (what session deletion
    finds: `items_from_session`)."""
    return f"{session_id or ''}/{message_id}"


def _quoted_now(quote: str, words: Sequence[str]) -> bool:
    q = squash(quote)
    if not q:
        return False
    return any(q in squash(w) and (len(q) >= MIN_QUOTE or q == squash(w)) for w in words)


def _said(evidence: Sequence[EvidenceRef]) -> list[EvidenceRef]:
    return [e for e in evidence if e.message_id is not None and e.role == "user" and e.quote]


def _pool(repo: Any) -> Any:
    pool = getattr(repo, "pool", None)
    if pool is None:
        pool = getattr(getattr(repo, "inner", None), "pool", None)
    if pool is None:
        raise RuntimeError("landing a preference needs the database (no pool on this repo)")
    return pool


def _row(user_id: str, *, kind: str, scope: str, summary: str, why: str, content: dict,
         key: str, now: datetime, new_id: Ids, executed: bool = False,
         undo: dict | None = None, result: dict | None = None) -> ActionRow:
    return ActionRow(action_id=new_id("act"), user_id=user_id, kind=kind, scope=scope,
                     level="auto_undo" if executed else "ask",
                     status="executed" if executed else "proposed", source=SOURCE,
                     summary=summary[:200], why=why, content=content,
                     content_hash=payload_hash(content), idempotency_key=key, created_at=now,
                     decided_at=now if executed else None, undo=undo, result=result)


def _insert(conn: Any, row: ActionRow) -> ActionRow:
    """The row, or the one already stored under its key (same user, same content)."""
    if ledger_store.insert_action(conn, row):
        return row
    got = ledger_store.get_action_by_key(conn, row.idempotency_key)
    if got is None or got.user_id != row.user_id or got.content_hash != row.content_hash:
        raise RuntimeError(f"ledger key {row.idempotency_key!r} was used for another action")
    return got


def _found(repo: Any, user_id: str, key: str) -> Landed | None:
    with _pool(repo).connection() as conn:
        row = ledger_store.get_action_by_key(conn, key)
    if row is None:
        return None
    if row.user_id != user_id:
        raise RuntimeError(f"ledger key {key!r} belongs to another user")
    item = row.content.get("item_id") or row.content.get("line_key")
    if row.status in ("proposed", "approved"):
        return Landed(status="pending", item_id=item, pending_action_id=row.action_id,
                      action_id=row.action_id)
    if row.status == "executed":
        return Landed(status="active", item_id=item, action_id=row.action_id)
    return _refused(DECIDED)


def close_pending(user_id: str, item_id: str, now: datetime) -> Callable[[Any], None]:
    """A commit extra: the proposed PENDING_KIND row of `item_id` (if any) becomes `rejected`
    (the item was removed elsewhere, e.g. the guidelines page)."""
    def write(conn: Any) -> None:
        if conn is None:
            return
        conn.execute("update agent_action set status = 'rejected', decided_at = %s "
                     "where user_id = %s and kind = %s and status = 'proposed' "
                     "and content ->> 'item_id' = %s", (now, user_id, PENDING_KIND, item_id))
    return write


def land_preference(repo: Repo, snap: Snapshot, *, kind: Kind, text: str,
                    provenance: Provenance, evidence: Sequence[EvidenceRef],
                    scope: MemoryScope, section: str | None = None,
                    replaces: str | None = None, pending: bool = False,
                    same_as: str | None = None, judge: Any = None,
                    history_policy: Literal["pending"] = "pending",
                    idempotency_key: str | None = None, session_id: str | None = None,
                    now: datetime, new_id: Ids,
                    commit_extra: Sequence[Callable[[Any], None]] = (),
                    current: CurrentMessage | None = None, domain: str | None = None,
                    profile: Any = None) -> Landed:
    """See the module doc. `session_id`: the session whose chat shows the pending card (None: his
    next chat; it is also listed in each turn's ops line). `current`: the message being answered
    (needed for explicit_current). `domain`: a rule only for pages of that domain (None: all).
    `profile`: a profile store with `update_in(conn, user_id, fn)` (an active fact).
    `same_as`: a spec item id the caller's judge found this rule restates (a teaching rule).
    `judge`: a `teach.rule_judge.RuleJudge` for his own words now (module doc)."""
    if history_policy != "pending":
        raise ValueError("explicit_history always waits for his confirmation")
    evidence = tuple(evidence)
    t = " ".join(text.split())
    if not t:
        return _refused(STATED_EMPTY)
    if len(t) > MAX_TEXT:
        return _refused(STATED_LONG)
    if provenance == "observed":
        return _refused(OBSERVED)
    if scope.scope_type != "global":
        return _refused(NOT_GLOBAL)
    said = _said(evidence)
    if provenance in ("explicit_current", "explicit_history") and not said:
        return _refused(NO_WORDS)
    if not evidence:
        return _refused(NO_WORDS)
    now_ev: EvidenceRef | None = None
    if provenance == "explicit_current":
        now_ev = next((e for e in said if current is not None
                       and e.message_id == current.message_id
                       and _quoted_now(e.quote or "", current.words)), None)
        if now_ev is None:
            return _refused(NOT_QUOTED_NOW)
    active = now_ev is not None and not pending
    key = KEY_PREFIX + (idempotency_key or new_id("pk"))
    if idempotency_key is not None:
        found = _found(repo, snap.user_id, key)
        if found is not None:
            return found
    ev = now_ev or (said[0] if said else None)
    ref = message_ref(ev.session_id, ev.message_id) if ev is not None and ev.message_id else None
    why = stated_evidence(ev.quote) if ev is not None and ev.quote else ""
    if kind == "about_me":
        return _land_fact(repo, snap, text=t, section=section, old_line=replaces or "",
                          provenance=provenance, ev=ev,
                          ref=ref, why=why, active=active, key=key, session_id=session_id,
                          now=now, new_id=new_id, commit_extra=commit_extra, profile=profile)
    return _land_rule(repo, snap, text=t, provenance=provenance, ev=ev, ref=ref, why=why,
                      active=active, replaces=replaces, same_as=same_as,
                      judge=judge if provenance == "explicit_current" else None, key=key,
                      session_id=session_id, domain=domain, now=now, new_id=new_id,
                      commit_extra=commit_extra)


def _proposed_card(repo: Any, user_id: str, item_id: str) -> str | None:
    """The action id of the proposed / approved PENDING_KIND row (the card) of `item_id`."""
    with _pool(repo).connection() as conn:
        row = conn.execute("select action_id from agent_action where user_id = %s and kind = %s "
                           "and status in ('proposed', 'approved') "
                           "and content ->> 'item_id' = %s order by created_at desc limit 1",
                           (user_id, PENDING_KIND, item_id)).fetchone()
    if row is None:
        return None
    return row["action_id"]


def _existing(repo: Any, user_id: str, twin: Any) -> Landed:
    """The rule `twin` as what this landing returns (module doc, 「One rule, one card」)."""
    if twin.authority != "pending":
        return Landed(status="active", item_id=twin.item_id, existing=True)
    card = _proposed_card(repo, user_id, twin.item_id)
    if card is None:
        return _refused(STATED_PENDING)
    return Landed(status="pending", item_id=twin.item_id, pending_action_id=card,
                  action_id=card, existing=True)


def _judged_twin(judge: Any, items: Sequence[Any], text: str, domain: str | None) -> Any:
    """The rule in force or pending card the judge says `text` restates (verdict "same"), or
    None; a failed call is logged and is None."""
    mine = [i for i in items if i.authority in ("user", "pending")]
    if not mine:
        return None
    by_ref = {f"{i.item_id}@{i.rev}": i for i in mine}
    try:
        v = judge.conflict(text, "any", domain,
                           [Against(ref, i.text, "rule", i.scope, i.domain)
                            for ref, i in by_ref.items()])
    except Exception:  # broad on purpose: the check is extra, his rule still lands
        log.warning("remember_preference: the rule judge failed; landing as new", exc_info=True)
        return None
    if v.verdict != "same":
        return None
    return next((by_ref[r] for r in v.against if r in by_ref), None)


def _land_rule(repo: Repo, snap: Snapshot, *, text: str, provenance: Provenance,
               ev: EvidenceRef | None, ref: str | None, why: str, active: bool,
               replaces: str | None, same_as: str | None, judge: Any, key: str,
               session_id: str | None,
               domain: str | None,
               now: datetime, new_id: Ids,
               commit_extra: Sequence[Callable[[Any], None]]) -> Landed:
    state = snap.state
    spec = tspec.effective_spec(state)
    old = None
    if replaces is not None:
        old = next((i for i in spec.items if i.item_id == replaces), None)
        if old is None:
            return _refused(NO_RULE.format(replaces))
    twin = next((i for i in spec.items if i is not old
                 and (i.item_id == same_as or near_duplicate(text, i.text))), None)
    if twin is None and judge is not None and same_as is None:
        twin = _judged_twin(judge, [i for i in spec.items if i is not old], text, domain)
    if twin is not None:
        return _existing(repo, snap.user_id, twin)
    if not active and any(near_duplicate(text, b) for b in blocked_rule_texts(state)):
        return _refused(STATED_REJECTED)
    item_id = new_id("si")
    uid = snap.user_id
    rows: list[ActionRow] = []
    if active:
        add = tspec.AddRule(item_id, text, domain=domain, origin="user_chat", authority="user",
                            origin_ref=ref)
        op: tspec.RuleOp = tspec.ReplaceRule(old.item_id, add) if old is not None else add
        undo = {"mode": "stated", "item_id": item_id, "text": text,
                **({"old": old.model_dump(mode="json")} if old is not None else {})}
        row = _row(uid, kind=STATED_KIND, scope=RULE_SCOPE, summary=STATED_TITLE + text, why=why,
                   content={"item_id": item_id, "text": text, "quote": ev.quote if ev else None,
                            "provenance": provenance, "origin_ref": ref,
                            **({"replaces": old.item_id} if old is not None else {})},
                   key=key, now=now, new_id=new_id, executed=True, undo=undo,
                   result={"item_id": item_id,
                           "superseded": [old.item_id] if old is not None else []})
        actor: Any = "user"
    else:
        origin: Any = "memory_backfill" if provenance == "explicit_history" else "assistant_drafted"
        op = tspec.AddRule(item_id, text, domain=domain, origin=origin, authority="pending",
                           origin_ref=ref)
        row = _row(uid, kind=PENDING_KIND, scope=RULE_SCOPE, summary=PENDING_TITLE + text,
                   why=why,
                   content={"confirm": True, "item_id": item_id, "text": text,
                            "quote": ev.quote if ev else None, "provenance": provenance,
                            "origin_ref": ref, "session_id": session_id,
                            **({"replaces": old.item_id} if old is not None else {})},
                   key=key, now=now, new_id=new_id)
        actor = "system"
    pool = _pool(repo)        # fail before anything is written when the ledger cannot be

    def ledger_row(conn: Any) -> None:
        if conn is None:
            with pool.connection() as c, c.transaction():
                rows.append(_insert(c, row))
        else:
            rows.append(_insert(conn, row))

    commit_spec(repo, snap, lambda s: tspec.write(s, op, actor=actor), actor=actor,
                via="chat", action="guideline", origin=ref, now=now, new_id=new_id,
                evidence={item_id: why} if why else None,
                commit_extra=[ledger_row, *commit_extra])
    got = rows[0] if rows else row
    if active:
        return Landed(status="active", item_id=item_id, action_id=got.action_id,
                      superseded=(old.item_id,) if old is not None else ())
    return Landed(status="pending", item_id=item_id, pending_action_id=got.action_id,
                  action_id=got.action_id)


def fact_meta(provenance: Provenance, ref: str | None, why: str) -> dict[str, Any]:
    return {"provenance": provenance, "origin_ref": ref, "evidence": why or None}


def _fact_known(repo: Repo, user_id: str, section: str, text: str) -> bool:
    """`text` restates or is part of a line of his in `section`, or of a fact card waiting for him."""
    with _pool(repo).connection() as conn:
        row = conn.execute("select body -> 'owned' -> %s as lines from learner_profile "
                           "where user_id = %s", (section, user_id)).fetchone()
        waiting = conn.execute("select content ->> 'text' as text from agent_action "
                               "where user_id = %s and kind = %s and status = 'proposed'",
                               (user_id, FACT_KIND)).fetchall()
    lines = [*(row["lines"] or () if row else ()), *(r["text"] or "" for r in waiting)]
    return any(near_duplicate(text, x) or squash(text) in squash(x) for x in lines)


def _land_fact(repo: Repo, snap: Snapshot, *, text: str, section: str | None, old_line: str,
               provenance: Provenance, ev: EvidenceRef | None, ref: str | None, why: str,
               active: bool, key: str, session_id: str | None, now: datetime, new_id: Ids,
               commit_extra: Sequence[Callable[[Any], None]], profile: Any) -> Landed:
    sec = section or WHO          # Memory's facts carry no section: 我是谁 (his card shows it)
    if sec not in FACT_SECTIONS:
        return _refused(NO_SECTION.format(sec))
    meta = fact_meta(provenance, ref, why)
    content = {"section": sec, "quote": ev.quote if ev else None, "text": text,
               "old_line": " ".join(old_line.split()),
               "meta": meta, "line_key": line_key(sec, text)}
    pool = _pool(repo)
    if active:
        if profile is None:
            return _refused(NO_PROFILE)
        row = _row(snap.user_id, kind=FACT_KIND, scope=FACT_SCOPE, summary=STATED_TITLE + text,
                   why=why, content=content, key=key, now=now, new_id=new_id, executed=True,
                   undo=dict(content))
        from evowiki.profile.note import FactMeta
        m = FactMeta.model_validate({**meta, "action_id": row.action_id})
        with pool.connection() as conn, conn.transaction():
            got = _insert(conn, row)
            profile.update_in(conn, snap.user_id, lambda p: add_fact(p, sec, text, content["old_line"], m))
            for write in commit_extra:
                write(conn)
        return Landed(status="active", item_id=content["line_key"], action_id=got.action_id)
    if _fact_known(repo, snap.user_id, sec, text):
        return _refused(FACT_KNOWN)
    row = _row(snap.user_id, kind=FACT_KIND, scope=FACT_SCOPE,
               summary=f"{FACT_TITLE}「{sec}」{text}", why=why,
               content={**content, "confirm": True, "session_id": session_id}, key=key, now=now,
               new_id=new_id)
    with pool.connection() as conn, conn.transaction():
        got = _insert(conn, row)
        for write in commit_extra:
            write(conn)
    return Landed(status="pending", item_id=content["line_key"], pending_action_id=got.action_id,
                  action_id=got.action_id)


# ---- a deleted session (memory spec §4.2 step 6; rule provenance contract §5) ----

REDACTED = "来源会话已删除"
DELETED = "deleted:"


@dataclass(frozen=True)
class SessionItems:
    """What a session produced that is still kept: the spec items whose words came from it
    (`origin_ref` `<session>/…`, pending ones included) and his profile facts likewise
    (section, line, line key)."""

    rules: tuple[Any, ...]
    facts: tuple[tuple[str, str, str], ...]


def _from(ref: Any, session_id: str) -> bool:
    return isinstance(ref, str) and ref.startswith(f"{session_id}/")


def items_from_session(state: Any, profile: Any, session_id: str) -> SessionItems:
    """The spec items and the profile facts whose provenance points into `session_id`: what the
    delete dialog lists for him to tick."""
    rules = tuple(i for i in tspec.effective_spec(state).items if _from(i.origin_ref, session_id))
    facts = []
    for section, lines in profile.owned.items():
        for line in lines:
            m = profile.fact_meta.get(line_key(section, line))
            if m is not None and _from(m.origin_ref, session_id):
                facts.append((section, line, line_key(section, line)))
    return SessionItems(rules, tuple(facts))


def _deleted(ref: str) -> str:
    return (DELETED + ref)[:200]


def redact_session_evidence(conn: Any, user_id: str, session_id: str, *,
                            request_id: str) -> dict[str, int]:
    """Inside the memory session's forget transaction (it has called
    `evowiki_forget_open(request_id)`; nothing here sets the flag or commits): every quote of the
    session's words in his preferences becomes REDACTED and each `origin_ref` into the session
    becomes `deleted:<ref>`. The rules and facts themselves stay (the ones he ticked go through
    the normal delete / undo commits).
    - wiki_event (UPDATE of `payload` only): every `teaching_spec_saved` whose items name the
      session (their `origin_ref`, the commit `origin`, those items' evidence, in every version);
      every `rule_decided` whose `ref` names it keeps the decision, only `ref` is redacted;
    - wiki_snapshot: the cached state is dropped (the next load replays the redacted log);
    - learner_profile: the facts' `fact_meta` (`origin_ref`, `evidence`);
    - agent_action: the preference rows that cite the session (`quote`, `why`, `origin_ref`, a
      fact's meta; the content hash follows).
    wiki_snapshot and learner_profile have no audit trigger: each is noted with
    `evowiki_forget_note`. Idempotent. Returns the rows changed per table."""
    import json

    from psycopg.types.json import Jsonb
    out = {"wiki_event": 0, "wiki_snapshot": 0, "learner_profile": 0, "agent_action": 0}
    rows = conn.execute("select event_id, type, payload from wiki_event where user_id = %s "
                        "and type in ('teaching_spec_saved', 'rule_decided') order by seq",
                        (user_id,)).fetchall()
    ids = {it["item_id"] for r in rows if r["type"] == "teaching_spec_saved"
           for it in ((r["payload"].get("spec") or {}).get("items") or ())
           if _from(it.get("origin_ref"), session_id)}
    for r in rows:
        p = json.loads(json.dumps(r["payload"]))
        changed = False
        if r["type"] == "teaching_spec_saved":
            for it in (p.get("spec") or {}).get("items") or ():
                if _from(it.get("origin_ref"), session_id):
                    it["origin_ref"] = _deleted(it["origin_ref"])
                    changed = True
            if _from(p.get("origin"), session_id):
                p["origin"] = _deleted(p["origin"])
                changed = True
            ev = p.get("evidence") or {}
            for k in ev:
                if k in ids and ev[k] != REDACTED:
                    ev[k] = REDACTED
                    changed = True
        elif _from(p.get("ref"), session_id):
            p["ref"] = _deleted(p["ref"])
            changed = True
        if changed:
            conn.execute("update wiki_event set payload = %s where event_id = %s",
                         (Jsonb(p), r["event_id"]))
            out["wiki_event"] += 1
    if out["wiki_event"]:
        n = conn.execute("delete from wiki_snapshot where user_id = %s", (user_id,)).rowcount
        out["wiki_snapshot"] = n
        conn.execute("select evowiki_forget_note(%s, %s, %s)",
                     (request_id, "preferences.wiki_snapshot", n))
    row = conn.execute("select body from learner_profile where user_id = %s for update",
                       (user_id,)).fetchone()
    if row is not None:
        body = json.loads(json.dumps(row["body"]))
        hit = False
        for m in (body.get("fact_meta") or {}).values():
            if _from(m.get("origin_ref"), session_id):
                m["origin_ref"] = _deleted(m["origin_ref"])
                m["evidence"] = REDACTED
                hit = True
        if hit:
            conn.execute("update learner_profile set body = %s where user_id = %s",
                         (Jsonb(body), user_id))
            out["learner_profile"] = 1
            conn.execute("select evowiki_forget_note(%s, %s, %s)",
                         (request_id, "preferences.learner_profile", 1))
    like = f"{session_id}/%"
    acts = conn.execute(
        "select action_id, content from agent_action where user_id = %s and kind in (%s, %s, %s) "
        "and (content ->> 'origin_ref' like %s or content -> 'meta' ->> 'origin_ref' like %s)",
        (user_id, STATED_KIND, PENDING_KIND, FACT_KIND, like, like)).fetchall()
    for a in acts:
        c = json.loads(json.dumps(a["content"]))
        if c.get("quote"):
            c["quote"] = REDACTED
        if _from(c.get("origin_ref"), session_id):
            c["origin_ref"] = _deleted(c["origin_ref"])
        m = c.get("meta")
        if isinstance(m, dict) and _from(m.get("origin_ref"), session_id):
            m["origin_ref"] = _deleted(m["origin_ref"])
            m["evidence"] = REDACTED
        conn.execute("update agent_action set content = %s, content_hash = %s, why = %s "
                     "where action_id = %s", (Jsonb(c), payload_hash(c), REDACTED, a["action_id"]))
        out["agent_action"] += 1
    return out
