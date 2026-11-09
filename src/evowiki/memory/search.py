"""Memory search (spec §3.2 / §3.3): hard filters, then BM25 + exact cosine fused by RRF, then
ranking boosts, then (auto mode only) an admission gate.

  auto     what the turn recalls by itself: episodes, active decisions and open questions only;
           a preference / fact about the learner is never returned (its authority is the
           teaching spec / profile, already in the prompt). A row enters only through the vector
           channel (cosine >= threshold) or the strict lexical channel (a shared concept with a
           BM25 hit, a contiguous query phrase of two or more words found verbatim, or an
           English term / identifier found verbatim); a lone keyword hit is not enough.
  history  what the learner asks to look up: every kind and status, labelled; any BM25 hit
           counts (no strict lexical gate), a vector-only row still needs cosine >= threshold.
           A preference / fact about him is labelled by what its authority says now
           (`authority_status`, worked out at read time and never written back): pending ->
           「待确认，未生效」, gone ->「已删除，未生效」; no `authority` to ask (or it fails) ->
           「没能核对是否生效」 (never shown as in force unchecked).

An episode is what was said; what the turn changed lives in the ledger. In both modes `undone` (session
and turn -> what of that turn's actions he undid, read at read time) puts the undo first in the hit's
text, so a recalled 「已设好」 never reads as still standing.

Every read is this user's rows only (the store filters by user_id first; the BM25 corpus is
built from them). Sizes, boosts and the threshold are module constants: the harness pack never
touches them."""

import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from evowiki.memory.store import Episode, Item, MemoryStore, tag
from evowiki.retrieval.embed import Embedder
from evowiki.sources.search import bm25_rank, rrf_ids, tokenize
from evowiki.teach.textsim import squash

AUTO_TAU = 0.35                    # vector channel threshold (calibrated in plan Task 14)
AUTO_KINDS = ("episode", "decision", "open_question")
BOOST_CONCEPT = 1.5
BOOST_PAGE = 1.3
BOOST_DECISION = 1.2
HALF_LIFE_DAYS = 30.0              # episodes only; decisions / open questions never decay
PHRASE_MIN_CHARS = 3               # a contiguous query phrase this long (squashed) counts
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{2,}")
_NO_EXCLUDE: Mapping[str, int] = MappingProxyType({})

log = logging.getLogger(__name__)

Mode = Literal["auto", "history"]
AuthorityStatus = Literal["active", "pending", "gone"]
AUTHORITY_LABELS: Mapping[str, str] = MappingProxyType({
    "active": "", "pending": "待确认，未生效", "gone": "已删除，未生效"})
UNCHECKED = "没能核对是否生效"


# an S3 ledger draft's status (a pending preference / fact he has not answered yet) -> where the
# memory item stands; a draft not found, rejected, undone or failed is gone
DRAFT_STATUS: Mapping[str, AuthorityStatus] = MappingProxyType({
    "proposed": "pending", "approved": "pending", "executed": "active"})


def authority_status(item: Item, state: Any, profile_sections: Mapping[str, Sequence[str]],
                     draft_status: Callable[[str], str | None] | None = None
                     ) -> AuthorityStatus:
    """Where a preference / fact about him stands now, read from its authority (pure, read only;
    the result is never stored: `memory_item.status` stays NULL for these kinds).
    - Waiting for him (only `pending_action_id`, S3's ledger draft): `draft_status(id)` through
      DRAFT_STATUS; without `draft_status` (or no reference at all) -> pending.
    - A preference with its teaching-spec item (`spec_item_id`): gone from the effective spec ->
      gone, authority pending -> pending, else active.
    - A fact with its profile line (`profile_ref` {section, text} or {key}): that line in the
      profile -> active, else gone."""
    if item.kind not in ("preference", "about_me"):
        raise ValueError(f"{item.kind} has no authority outside memory")
    if item.spec_item_id is None and not item.profile_ref:
        if item.pending_action_id is None or draft_status is None:
            return "pending"
        return DRAFT_STATUS.get(draft_status(item.pending_action_id) or "", "gone")
    if item.kind == "preference":
        if item.spec_item_id is None:
            return "gone"
        from evowiki.wikiapp.teachspec import effective_spec
        hit = next((i for i in effective_spec(state).items if i.item_id == item.spec_item_id),
                   None)
        if hit is None:
            return "gone"
        return "pending" if hit.authority == "pending" else "active"
    ref = item.profile_ref or {}
    line = ref.get("text") or ref.get("key")
    if "section" in ref:
        return "active" if line in profile_sections.get(str(ref["section"]), ()) else "gone"
    return "active" if any(line in lines for lines in profile_sections.values()) else "gone"


UNDONE_NOTE = "（这一轮的改动他后来撤销了，现在不生效：{titles}）"
# session id, turn's message id -> the titles of that turn's actions he undid
UndoneOf = Callable[[str, str], Sequence[str]]
# user id -> (item -> its authority status), built once per search (one state / profile read)
AuthorityOf = Callable[[str], Callable[[Item], AuthorityStatus]]


class Hit(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str                        # ep:{session}/{message} | item:{item_id}
    kind: str                      # "episode" | ItemKind
    at: datetime
    page_id: str | None
    provenance: str | None         # None for an episode
    status: str | None
    session_id: str
    message_ids: tuple[str, ...]
    text: str
    score: float
    label: str = ""


def episode_text(e: Episode) -> str:
    return f"用户：{e.user_text}\n助手：{e.answer_text}"


def _label(it: Item) -> str:
    if it.status == "superseded":
        return f"后来被 item:{it.superseded_by} 替代" if it.superseded_by else "后来被替代"
    if it.status == "resolved":
        return "后来已解决"
    return ""


class _Cand(BaseModel):
    hit: Hit
    concept_ids: tuple[str, ...]


def _authority_label(it: Item, status_of: Callable[[Item], AuthorityStatus] | None) -> str:
    if status_of is None:
        return UNCHECKED
    try:
        return AUTHORITY_LABELS[status_of(it)]
    except Exception:  # broad on purpose: an unread authority is "unchecked", never "in force"
        log.warning("authority status of %s failed", it.item_id, exc_info=True)
        return UNCHECKED


def _candidates(store: MemoryStore, user_id: str, *, mode: Mode, since, until, kinds,
                exclude_sessions_from, as_of, known_concepts,
                authority: AuthorityOf | None = None) -> list[_Cand]:
    wanted = set(AUTO_KINDS if mode == "auto" else
                 ("episode", "decision", "open_question", "preference", "about_me"))
    if kinds is not None:
        wanted &= set(kinds)
    out: list[_Cand] = []
    if "episode" in wanted:
        for e in store.episodes(user_id, since=since, until=until, as_of=as_of,
                                exclude_sessions_from=exclude_sessions_from):
            out.append(_Cand(concept_ids=e.concept_ids, hit=Hit(
                id=f"ep:{e.key}", kind="episode", at=e.at, page_id=e.page_id, provenance=None,
                status=None, session_id=e.session_id, message_ids=(e.message_id,),
                text=episode_text(e), score=0.0)))
    item_kinds = sorted(wanted - {"episode"})
    if not item_kinds:
        return out
    status_of = None
    if authority is not None and {"preference", "about_me"} & set(item_kinds):
        try:
            status_of = authority(user_id)
        except Exception:  # broad on purpose: labelled unchecked below
            log.warning("authority read failed for %s", user_id, exc_info=True)
    for it in store.items(user_id, kinds=item_kinds, as_of=as_of):
        if since is not None and it.valid_from < since:
            continue
        if until is not None and it.valid_from >= until:
            continue
        if mode == "auto":
            if it.status != "active":
                continue
            if (it.kind == "open_question" and it.concept_ids
                    and set(it.concept_ids) <= known_concepts):
                continue
        out.append(_Cand(concept_ids=it.concept_ids, hit=Hit(
            id=f"item:{it.item_id}", kind=it.kind, at=it.valid_from, page_id=it.page_id,
            provenance=it.provenance, status=it.status, session_id=it.session_id,
            message_ids=tuple(e.message_id for e in it.evidence if e.message_id is not None),
            text=it.content, score=0.0,
            label=_authority_label(it, status_of) if it.kind in ("preference", "about_me")
            else _label(it))))
    return out


def _cosines(store: MemoryStore, embedder: Embedder | None, user_id: str,
             query: str) -> dict[str, float]:
    if embedder is None:
        return {}
    try:
        q = embedder.embed_query(query)
    except Exception:
        log.warning("memory query embedding failed for %s", user_id, exc_info=True)
        return {}
    qn = math.sqrt(sum(x * x for x in q)) or 1.0
    t = tag(embedder)
    vecs = {f"ep:{k}": v for k, v in store.episode_vectors(user_id, t).items()}
    vecs.update({f"item:{k}": v for k, v in store.item_vectors(user_id, t).items()})
    out = {}
    for i, v in vecs.items():
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out[i] = sum(a * b for a, b in zip(q, v, strict=True)) / (qn * n)
    return out


def _norm(text: str) -> str:
    return squash(text).lower()


def _phrases(query: str) -> list[str]:
    """Contiguous runs of two or more query words, squashed, at least PHRASE_MIN_CHARS long."""
    toks = [_norm(t) for t in tokenize(query)]
    out = []
    for i in range(len(toks)):
        for j in range(i + 2, len(toks) + 1):
            p = "".join(toks[i:j])
            if len(p) >= PHRASE_MIN_CHARS:
                out.append(p)
    return out


def _strict_lexical(c: _Cand, *, bm25_hit: bool, phrases: Sequence[str],
                    idents: Sequence[str], concept_ids: frozenset[str]) -> bool:
    text = _norm(c.hit.text)
    if bm25_hit and concept_ids & set(c.concept_ids):
        return True
    if any(p in text for p in phrases):
        return True
    return any(i in text for i in idents)


def _with_undone(h: Hit, undone: UndoneOf | None) -> Hit:
    titles = undone(h.session_id, h.message_ids[0]) if undone is not None and h.kind == "episode" else ()
    if not titles:
        return h
    return h.model_copy(update={"text": f"{UNDONE_NOTE.format(titles='；'.join(titles))}\n{h.text}"})


def search(store: MemoryStore, embedder: Embedder | None, user_id: str, query: str, *,
           mode: Mode, since: datetime | None = None, until: datetime | None = None,
           kinds: Sequence[str] | None = None, page_id: str | None = None,
           concept_ids: Sequence[str] = (), exclude_sessions_from: Mapping[str, int] = _NO_EXCLUDE,
           as_of: datetime | None = None, k: int = 10, threshold: float | None = None,
           known_concepts: frozenset[str] = frozenset(),
           now: datetime | None = None, authority: AuthorityOf | None = None,
           undone: UndoneOf | None = None) -> list[Hit]:
    """Up to `k` hits, best first. `since` / `until` are aware datetimes (the caller turns the
    learner's dates into them); `now` ages episodes (default: the newest candidate's time, so
    a replay with `as_of` ranks as it did). `known_concepts`: concepts K holds as KNOWN, read by
    the caller; an open question all of whose concepts are known is left out in auto mode.
    `authority`: labels a history hit's preference / fact (`authority_status`)."""
    if k <= 0 or not query.strip():
        return []
    cands = _candidates(store, user_id, mode=mode, since=since, until=until, kinds=kinds,
                        exclude_sessions_from=exclude_sessions_from, as_of=as_of,
                        known_concepts=known_concepts, authority=authority)
    if not cands:
        return []
    by_id = {c.hit.id: c for c in cands}
    depth = max(3 * k, 20)
    lex = bm25_rank([(i, c.hit.text) for i, c in by_id.items()], query, k=depth)
    cos = {i: s for i, s in _cosines(store, embedder, user_id, query).items() if i in by_id}
    vec = sorted(cos, key=lambda i: (-cos[i], i))[:depth]
    fused = rrf_ids([[i for i, _ in lex], vec], k=len(by_id))

    ref = now or (as_of if as_of is not None else max(c.hit.at for c in cands))
    concepts = frozenset(concept_ids)
    lex_ids = {i for i, _ in lex}
    phrases = _phrases(query) if mode == "auto" else []
    idents = [m.lower() for m in _IDENT.findall(query)] if mode == "auto" else []
    tau = AUTO_TAU if threshold is None else threshold
    scored: list[Hit] = []
    for i, base in fused:
        c = by_id[i]
        close = cos.get(i, -1.0) >= tau
        if mode == "auto" and not (
                close or _strict_lexical(c, bm25_hit=i in lex_ids, phrases=phrases,
                                         idents=idents, concept_ids=concepts)):
            continue
        if mode == "history" and not (close or i in lex_ids):
            continue              # a vector is always someone's nearest: not a hit by itself
        score = base
        if concepts & set(c.concept_ids):
            score *= BOOST_CONCEPT
        if page_id is not None and c.hit.page_id == page_id:
            score *= BOOST_PAGE
        if c.hit.kind == "decision":
            score *= BOOST_DECISION
        if c.hit.kind == "episode":
            age = max((ref - c.hit.at).total_seconds() / 86400.0, 0.0)
            score *= 0.5 ** (age / HALF_LIFE_DAYS)
        scored.append(c.hit.model_copy(update={"score": score}))
    scored.sort(key=lambda h: (-h.score, h.id))
    hidden = frozenset(s.item_id for s in store.suppressions(user_id))    # memory/forget_fact.py
    if hidden:
        scored = [h for h in scored if h.id.removeprefix("item:") not in hidden and h.id not in hidden]
    return [_with_undone(h, undone) for h in scored[:k]]
