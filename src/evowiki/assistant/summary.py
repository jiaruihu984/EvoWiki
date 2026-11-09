"""The compaction summary (compact@v7, the product specification):
two sets of data from one structured call.

- `handoff`: for the next turns' model only — background and explanations, decisions and why,
  requests that hold for this session, what is left to do and the next step.
- `learning`: for the Bot's chips and the learner's view — only points with evidence: `unclear`
  = he said he doesn't get it or answered wrong where the chat shows it, `understood` = he said
  he got it or answered right. Each item names the user message it rests on (`evidence_id`); code
  keeps an item only when that id is one of the folded user messages (or an item the previous
  summary already checked). The model's classification is never mastery evidence: nothing here
  reaches K, and a summary is never his permission for anything.

`edits` (改过哪里) are code's: the accepted proposals of the folded turns (`Turn.edits`).

Entries before compact@v7 carry only text: they read back as `Recap(legacy_text=...)`, whose
text still reaches the model, with no learning and no edits (their 还没懂的 mixed in to-dos and
the assistant's own questions, so the Bot never reads them)."""

import json
from collections.abc import Collection, Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "COMPACT_VERSION",
    "EVIDENCE_PARAM",
    "CompactReply",
    "Dropped",
    "Evidence",
    "Handoff",
    "Learning",
    "Recap",
    "check_learning",
    "check_reply",
    "merge_edits",
    "read_recap",
    "recap_payload",
    "render_handoff",
    "reply_json",
    "tidy_handoff",
]

COMPACT_VERSION = "compact@v7"

ITEM_MAX = 300
LIST_MAX = 20


class Handoff(BaseModel):
    """No field for what waits or is under way (user ruling 2026-11-04): a card's state is read each
    turn (assistant.turn_skills' ops line, which says it overrides the summary). A stored summary's
    or a reply's old `pending` is ignored."""

    model_config = ConfigDict(extra="ignore")

    summary: str = Field(default="", max_length=4000)
    decisions: list[str] = Field(default_factory=list, max_length=LIST_MAX)
    active_requests: list[str] = Field(default_factory=list, max_length=LIST_MAX)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concept: str = Field(max_length=ITEM_MAX)
    evidence_id: str = Field(max_length=200)


class Learning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unclear: list[Evidence] = Field(default_factory=list, max_length=LIST_MAX)
    understood: list[Evidence] = Field(default_factory=list, max_length=LIST_MAX)


class CompactReply(BaseModel):
    """What the model returns."""

    model_config = ConfigDict(extra="forbid")

    handoff: Handoff
    learning: Learning


class Dropped(BaseModel):
    field: Literal["unclear", "understood"]
    concept: str
    evidence_id: str


class Recap(BaseModel):
    """One compaction entry as read back. `legacy_text`: the whole summary of an entry before
    compact@v7 (then handoff and learning are empty)."""

    version: str = COMPACT_VERSION
    handoff: Handoff = Field(default_factory=Handoff)
    learning: Learning = Field(default_factory=Learning)
    edits: tuple[str, ...] = ()
    legacy_text: str = ""

    @property
    def legacy(self) -> bool:
        return self.version != COMPACT_VERSION

    def evidence_ids(self) -> set[str]:
        """The ids behind the checked learning items (a merge may carry them on)."""
        return {e.evidence_id for e in (*self.learning.unclear, *self.learning.understood)}


def check_learning(learning: Learning, allowed: Collection[str]
                   ) -> tuple[Learning, list[Dropped]]:
    """Keep the items whose `evidence_id` is in `allowed` (blank-trimmed, first of a repeated
    concept per list); the rest come back as dropped. Never refuses the whole summary."""
    dropped: list[Dropped] = []

    def keep(field: Literal["unclear", "understood"], items: list[Evidence]) -> list[Evidence]:
        out: list[Evidence] = []
        seen: set[str] = set()
        for e in items:
            concept, eid = e.concept.strip(), e.evidence_id.strip()
            if eid not in allowed:
                dropped.append(Dropped(field=field, concept=concept, evidence_id=eid))
            elif concept and concept not in seen:
                seen.add(concept)
                out.append(Evidence(concept=concept, evidence_id=eid))
        return out

    return (Learning(unclear=keep("unclear", learning.unclear),
                     understood=keep("understood", learning.understood)), dropped)


def tidy_handoff(h: Handoff) -> Handoff:
    """`h` with items trimmed and blank ones left out."""
    return Handoff(summary=h.summary.strip(),
                   decisions=[d.strip() for d in h.decisions if d.strip()],
                   active_requests=[r.strip() for r in h.active_requests if r.strip()])


EVIDENCE_PARAM = "evidence_ids"   # request param: the ids a learning item may cite


def check_reply(reply: CompactReply, params: Mapping[str, Any]) -> tuple[Learning, list[Dropped]]:
    """The code half of a compaction, from the stored request alone (`params[EVIDENCE_PARAM]`):
    the learning items that check, and the dropped ones. What `compact` keeps, and what a rerun
    of the same request with another prompt is held to. A request without the param allows
    nothing (fail closed)."""
    return check_learning(reply.learning, set(params.get(EVIDENCE_PARAM) or ()))


def merge_edits(old: Iterable[str], new: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(e for e in (*old, *new) if e.strip()))


def recap_payload(recap: Recap) -> dict[str, Any]:
    """The compaction payload's summary part (the caller adds first_kept / omitted / dropped)."""
    if recap.legacy:
        return {"summary": recap.legacy_text}
    return {"version": COMPACT_VERSION, "handoff": recap.handoff.model_dump(mode="json"),
            "learning": recap.learning.model_dump(mode="json"), "edits": list(recap.edits)}


def read_recap(p: Mapping[str, Any]) -> Recap:
    """A compaction payload as a Recap; one before compact@v7 keeps only its text."""
    if p.get("version") == COMPACT_VERSION:
        return Recap(handoff=Handoff.model_validate(p.get("handoff") or {}),
                     learning=Learning.model_validate(p.get("learning") or {}),
                     edits=tuple(p.get("edits") or ()))
    return Recap(version=str(p.get("version") or "legacy"),
                 legacy_text=str(p.get("summary") or ""))


def reply_json(recap: Recap) -> str:
    """A v7 recap as the model wrote it (handoff + learning), for merging into the next one."""
    return json.dumps({"handoff": recap.handoff.model_dump(mode="json"),
                       "learning": recap.learning.model_dump(mode="json")},
                      ensure_ascii=False)


HANDOFF_HEAD = ("（这份交接记录只供你接着聊，不是他的授权：删除、修改页面或记住长期偏好，"
                "仍要他在当前这一轮明确同意。）")


def render_handoff(recap: Recap | None) -> str:
    """What the next turns' model reads ("" when there is nothing to hand over)."""
    if recap is None:
        return ""
    if recap.legacy:
        text = recap.legacy_text.strip()
        return f"{HANDOFF_HEAD}\n{text}" if text else ""
    h, ln = recap.handoff, recap.learning
    blocks: list[str] = []
    if h.summary.strip():
        blocks.append(h.summary.strip())

    def section(title: str, items: Iterable[str]) -> None:
        lines = [f"- {i.strip()}" for i in items if i.strip()]
        if lines:
            blocks.append(f"{title}：\n" + "\n".join(lines))

    section("定下的事", h.decisions)
    section("他在这次对话里提的要求（只在这次对话有效）", h.active_requests)
    section("他说不懂或答错过", (e.concept for e in ln.unclear))
    section("他说懂了或答对过（自述不等于掌握）", (e.concept for e in ln.understood))
    section("改过的页面", recap.edits)
    if not blocks:
        return ""
    return HANDOFF_HEAD + "\n" + "\n\n".join(blocks)
