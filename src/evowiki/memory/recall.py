"""Automatic recall (spec §3.1): the turn's question searched in auto mode, rendered as ONE
separate `origin="memory"` context message placed right before the question. It is never part of
the learner's message or of the session log's user entry; what was recalled is logged on the
answer as ids and content hashes (`recalled`, `retrieval_version`).

What a material says never comes back from here as if it had been read (`recalled_answer`): an
earlier answer that cites pages comes back as its question and those pages. A model that has the
words answers from them without reading (live 2026-11-04: 「讲义第 13 页给出了」 in a turn that read
nothing, the page unreadable by then). Mature memory layers keep a pointer back to the canonical
record for the same reason: derived context is rebuilt from the source, never the other way round.

Size: at most RECALL_MAX_ITEMS lines within RECALL_BUDGET tokens, module constants (the harness
pack and H5 never touch them)."""

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from evowiki.assistant.session import SessionInfo, SessionState
from evowiki.goals.context import Part, assemble, estimate_tokens
from evowiki.llm.chat import MEMORY_FENCE_HEAD, ChatMessage, memory_fenced
from evowiki.memory.index import FEEDBACK_HEAD
from evowiki.memory.search import Hit, search

if TYPE_CHECKING:
    from evowiki.memory.ports import MemoryPorts

RECALL_MAX_ITEMS = 5
RECALL_BUDGET = 600
RECALL_LINE_MAX = 240               # characters of one recalled line's text
RETRIEVAL_VERSION = "memory-recall@v1"
FENCE_HEAD = MEMORY_FENCE_HEAD      # the providers fence an origin="memory" message with it
KIND_LABEL = {"decision": "当时的决定", "open_question": "当时提出的问题", "episode": "以前的对话"}
FEEDBACK_LABEL = "以前的反馈"         # an episode carrying his 👍 / 👎 (memory/index.py)
# what the memory's own head can't say for a material (it is shared with what the simulator imports)
RECALLED_NOTE = "这些是以前的对话，不是这一轮读到的。要说资料某页写了什么，这一轮先用 read_source 读；没读就说没读。"
_ANSWER = "助手："
_PAGE_REF = re.compile(r"p\.\s*\d+(?:\s*[–-]\s*\d+)?|第\s*\d+(?:\s*[–-]\s*\d+)?\s*页")


@dataclass(frozen=True)
class Recall:
    message: ChatMessage | None
    recalled: tuple[dict[str, Any], ...] = ()


NO_RECALL = Recall(None, ())


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def recalled_answer(h: Hit) -> str:
    """An episode's text with an answer that cites pages cut to the pages it cited (module doc);
    any other text as it is. The feedback line stays."""
    head, sep, answer = h.text.partition(_ANSWER)
    if h.kind != "episode" or not sep:
        return h.text
    pages = list(dict.fromkeys(m.group(0) for m in _PAGE_REF.finditer(answer)))
    if not pages:
        return h.text
    feedback = [x for x in answer.splitlines() if x.strip().startswith(FEEDBACK_HEAD)]
    said = f"（当时的回答引用了 {'、'.join(pages)}，内容没有放在这里）"
    return "\n".join([f"{head}{_ANSWER}{said}", *feedback])


def _line(h: Hit, tz: ZoneInfo, page_titles: Mapping[str, str]) -> str:
    day = h.at.astimezone(tz).date().isoformat()
    where = page_titles.get(h.page_id, "") if h.page_id else ""
    lines = [x.strip() for x in recalled_answer(h).splitlines() if x.strip()]
    feedback = [x for x in lines if x.startswith(FEEDBACK_HEAD)]
    kind = FEEDBACK_LABEL if h.kind == "episode" and feedback else KIND_LABEL.get(h.kind, h.kind)
    head = " ".join(x for x in (day, kind, where) if x)
    # the feedback first: a long answer is clipped, the feedback never is
    text = " / ".join([*feedback, *(x for x in lines if x not in feedback)])
    if len(text) > RECALL_LINE_MAX:
        text = text[:RECALL_LINE_MAX] + "…"
    return f"- [{head}] {text} ({h.id})"


def recall_block(ports: "MemoryPorts", *, info: SessionInfo, query: str, page_id: str | None,
                 concept_ids: Sequence[str], state: SessionState,
                 known_concepts: frozenset[str], now: datetime,
                 page_titles: Mapping[str, str] = {},
                 live_from: int | None = None,
                 excluded_sources: frozenset[str] = frozenset()) -> Recall:
    """The recall of one turn; NO_RECALL when nothing passes the auto gate. The current
    session's turns from `live_from` (default `state.first_kept`) on are already in the context
    verbatim and are not searched; its earlier turns are. The budget counts the fence the
    providers add."""
    live = state.first_kept if live_from is None else live_from
    excluded = set(excluded_sources)
    excluded.update(f'turn:{info.session_id}/{turn.message_id}' for turn in state.turns[live:])
    anchor_lines: list[str] = []
    anchor_records: list[dict] = []
    seen_text: set[str] = set()
    if ports.anchors is not None:
        rows = ports.anchors.search(info.user_id, query,
                                    exclude='page:' + page_id if page_id else None)
        for row in rows:
            this_row_sources: set[str] = set()
            for index, statement in enumerate(row.payload.get('statements', [])):
                sources = set(statement.get('sources', []))
                text = statement.get('text', '').strip()
                if not text or not sources or sources & excluded or text in seen_text:
                    continue
                snippet = text[:RECALL_LINE_MAX] + ('…' if len(text) > RECALL_LINE_MAX else '')
                anchor_lines.append(f'- [{row.key} v{row.version}] {snippet} ({row.node})')
                anchor_records.append({'id': row.node, 'kind': 'anchor', 'fragment': index,
                                       'content_hash': content_hash(snippet),
                                       'source_nodes': list(row.sources)})
                this_row_sources.update(sources)
                seen_text.add(text)
            excluded.update(this_row_sources)
    hits = search(ports.store, ports.embedder, info.user_id, query, mode="auto",
                  page_id=page_id, concept_ids=concept_ids,
                  exclude_sessions_from={info.session_id: (
                      state.first_kept if live_from is None else live_from)},
                  k=RECALL_MAX_ITEMS + len(excluded), known_concepts=known_concepts, now=now, undone=ports.undone)
    hits = [hit for hit in hits if hit.id not in excluded
            and not {f'turn:{hit.session_id}/{mid}' for mid in hit.message_ids} & excluded]
    hits = hits[:RECALL_MAX_ITEMS]
    if not hits and not anchor_lines:
        return NO_RECALL
    tz = ZoneInfo(ports.timezone(info.user_id))
    lines = tuple(_line(hit, tz, page_titles) for hit in hits)
    pack = assemble([Part(name='anchors', items=tuple(anchor_lines[:RECALL_MAX_ITEMS])),
                     Part(name='recall', items=lines)],
                    RECALL_BUDGET - estimate_tokens(memory_fenced(RECALLED_NOTE)))
    anchor_n = pack.kept('anchors')
    hit_n = min(pack.kept('recall'), RECALL_MAX_ITEMS - anchor_n)
    if not anchor_n and not hit_n:
        return NO_RECALL
    shown = [*anchor_lines[:anchor_n], *lines[:hit_n]]
    return Recall(ChatMessage(role='user', text=f"{RECALLED_NOTE}\n" + '\n'.join(shown), origin='memory'),
                  (*anchor_records[:anchor_n],
                   *({'id': hit.id, 'kind': hit.kind, 'content_hash': content_hash(hit.text)}
                     for hit in hits[:hit_n])))
