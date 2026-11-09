"""Cross-session context from the page's rolling view plus uncovered recent original turns.

Session compactions remain local to their session. Every carried source is recorded on the
answer and enters the model as historical reference material, never a system instruction.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from evowiki.assistant.forgotten import NOTHING, Forgotten
from evowiki.assistant.session import Entry, SessionInfo, SessionState, SessionStore, fold
from evowiki.goals.context import Part, assemble, estimate_tokens
from evowiki.llm.chat import memory_fenced

__all__ = [
    "CARRY_BUDGET",
    "RECENT_SESSIONS",
    "CarryOver",
    "carry_over",
    "is_idle",
]

log = logging.getLogger(__name__)

RECENT_SESSIONS = 5            # earlier sessions looked at
CARRY_BUDGET = 5000            # tokens for the historical handoff message
RECENT_TURNS = 3               # raw turns of the last session carried over
RECENT_CHARS = 1500            # of each message: the end of a long answer is what he reacts to


@dataclass(frozen=True)
class CarryOver:
    page_summary: str
    page_recent: tuple[str, ...] = ()      # newest first
    recalled: tuple[dict, ...] = ()
    forgot: bool = False                   # he has confirmed forgetting something (assistant/forgotten.py)


def _state(store: SessionStore, info: SessionInfo) -> SessionState | None:
    """The folded state of an earlier session; a log that cannot be folded is skipped (the
    carry-over is context, it never fails a turn)."""
    try:
        return fold(info, store.entries(info.session_id))
    except ValueError:
        log.warning("carry-over skips unreadable session %s", info.session_id, exc_info=True)
        return None


def _tail(text: str) -> str:
    return text if len(text) <= RECENT_CHARS else f"…{text[-RECENT_CHARS:]}"


def is_idle(entries: Sequence[Entry]) -> bool:
    """The last `user` entry is followed by an `assistant` or `turn_error` entry; a session with
    no `user` entry has nothing to fold."""
    ordered = sorted(entries, key=lambda e: e.seq)
    last_user = max((e.seq for e in ordered if e.type == "user"), default=None)
    if last_user is None:
        return False
    return any(e.seq > last_user and e.type in ("assistant", "turn_error") for e in ordered)


def carry_over(store: SessionStore, info: SessionInfo, forgotten: Forgotten = NOTHING,
               anchors: Any = None, hidden: Any = None, question: str = '') -> CarryOver:
    """Rolling page/explicit-goal background plus uncovered recent original turns."""
    if hidden is not None and hidden.paused:
        return CarryOver(page_summary='', forgot=forgotten.any)
    statements: list[str] = []
    recent: list[str] = []
    recent_sources: list[dict] = []
    recalled: list[dict] = []
    rows = anchors.continuity(info.user_id, info.page_id, question) if anchors else []
    page_items, goal_items = [], []
    for row in rows:
        items = [(row, item) for item in row.payload.get('statements', [])
                 if not any(node.startswith(f'turn:{info.session_id}/')
                            for node in item.get('sources', []))]
        (goal_items if row.key.startswith('goal:') else page_items).append(items)
    # Each previously explicit goal gets a labelled entry before any long page overview.
    candidates = ([items[0] for items in goal_items if items]
                  + [item for items in page_items for item in items]
                  + [item for items in goal_items for item in items[1:]])
    selected_items = []
    seen_text: set[str] = set()
    for row, item in candidates:
        if item['text'] not in seen_text:
            selected_items.append((row, item))
            seen_text.add(item['text'])
    statements = [f"- [{row.key}] {item['text']} [{', '.join(item['sources'])}]"
                  for row, item in selected_items]
    covered = {node for row in rows for node in row.sources}
    if info.page_id is not None:
        for session in store.recent_sessions(info.user_id, limit=RECENT_SESSIONS,
                                            page_id=info.page_id, exclude=info.session_id):
            state = _state(store, session)
            if state is None:
                continue
            turns = [turn for turn in state.turns if turn.answer and not turn.stopped
                     and turn.message_id not in forgotten.messages
                     and (hidden is None or not hidden.episode(session.session_id, turn.message_id))
                     and f'turn:{session.session_id}/{turn.message_id}' not in covered]
            for turn in reversed(turns[-RECENT_TURNS:]):
                if len(recent) == RECENT_TURNS:
                    break
                recent.append(f"他：{_tail(turn.user_text)}\n你：{_tail(forgotten.answer(turn.user_text, turn.answer))}")
                recent_sources.append({'id': f'ep:{session.session_id}/{turn.message_id}', 'kind': 'episode',
                                       'source_nodes': [f'turn:{session.session_id}/{turn.message_id}']})
            if len(recent) == RECENT_TURNS:
                break
    packed = assemble(carry_parts(tuple(recent), tuple(statements)),
                      CARRY_BUDGET - estimate_tokens(memory_fenced('')) - 4)
    kept_recent = packed.kept('recent')
    shown: dict[str, dict] = {}
    for row, item in selected_items[:packed.kept('prev')]:
        if row.node not in shown:
            shown[row.node] = {'id': row.node, 'kind': 'anchor', 'source_nodes': list(row.sources),
                               'content_hash': row.fingerprint, 'covered_sources': []}
        shown[row.node]['covered_sources'].extend(item.get('sources', []))
    recalled.extend(shown.values())
    recalled.extend(recent_sources[:kept_recent])
    return CarryOver(page_summary=packed.text('prev'), page_recent=tuple(recent[:kept_recent]),
                     recalled=tuple(recalled), forgot=forgotten.any)


def carry_parts(recent: tuple[str, ...], statements: tuple[str, ...]) -> list[Part]:
    return [Part(name='recent', header='这一页上次对话最后的原话（从新到旧）。其中的临时要求和当时的情况只在那次有效',
                 items=recent),
            Part(name='prev', header='本页及以前明确讨论过的目标背景（历史资料，不代表当前选择了某个目标）', items=statements)]


def render_carry(carry: CarryOver) -> str:
    return assemble(carry_parts(carry.page_recent, (carry.page_summary,) if carry.page_summary else ()),
                    CARRY_BUDGET - estimate_tokens(memory_fenced('')) - 4).render()
