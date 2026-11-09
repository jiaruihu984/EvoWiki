"""Bounded, source-checked rolling context; original logs remain the authority."""

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evowiki.assistant.forgotten import forgotten_of
from evowiki.assistant.session import fold
from evowiki.llm.models import LLMRequest
from evowiki.memory.forget import generation_guard
from evowiki.memory.lexical import rank
from evowiki.store import dreaming
from evowiki.store.db import fetch_one

BATCH_TURNS = 40
RAW_CHARS = 60000
TURN_CHARS = 1500
_UNCLEAR = re.compile(r"不(?:懂|理解|明白|会|清楚)|没(?:懂|理解|明白|搞懂)|困惑|理解有困难|don['’]t (?:understand|know|get it)|do not (?:understand|know)|can(?:not|['’]t) understand|not (?:clear|sure)|confused", re.IGNORECASE)
_UNDERSTOOD = re.compile(r"理解了|懂了|明白了|会了|搞懂了|i understand|got it|makes sense", re.IGNORECASE)


class Statement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=600)
    sources: list[str] = Field(min_length=1, max_length=4)
    state: Literal['background', 'unclear', 'understood', 'hypothesis'] = 'background'
    concept: str = Field(default='', max_length=300)
    evidence_quote: str = Field(default='', max_length=600)


class AnchorReply(BaseModel):
    model_config = ConfigDict(extra='forbid')
    statements: list[Statement] = Field(default_factory=list, max_length=24)
    learning: list[Statement] = Field(default_factory=list, max_length=BATCH_TURNS)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def covered_messages(row: dreaming.Summary, session_id: str) -> frozenset[str]:
    prefix = f'turn:{session_id}/'
    return frozenset(node[len(prefix):] for node in row.sources if node.startswith(prefix))


def covered_turns(row: dreaming.Summary, session_id: str) -> int:
    return int(row.payload.get('covered_turns', {}).get(session_id, 0))


def render(row: dreaming.Summary) -> str:
    return '\n'.join(f"- {s['text']} [{', '.join(s['sources'])}]"
                     for s in [*row.payload.get('statements', []),
                               *row.payload.get('learning', [])[:8]])


class AnchorMemory:
    def __init__(self, pool: Any, sessions: Any, store: Any, *, repo: Any = None):
        self.pool, self.sessions, self.store, self.repo = pool, sessions, store, repo

    def pending(self, user_id: str) -> list[dict]:
        with self.pool.connection() as conn:
            return dreaming.pending(conn, user_id)

    def _exists(self, uid: str, key: str) -> bool:
        kind, _, identity = key.partition(':')
        if kind == 'memory_item':
            item = self.store.get_item(uid, identity)
            return item is not None and item.kind in ('decision', 'open_question')
        if self.repo is None:
            return kind == 'page'  # goal ownership requires a repository
        state = self.repo.load(uid).state
        return identity in (state.pages if kind == 'page' else
                            {**state.paths, **state.maps} if kind == 'goal' else {})

    def readable(self, user_id: str, key: str) -> dreaming.Summary | None:
        if not self._exists(user_id, key):
            return None
        with self.pool.connection() as conn:
            row = dreaming.get(conn, user_id, 'anchor', key)
            if row is None or row.dirty or not dreaming.sources_visible(conn, user_id, row.sources):
                return None
        # Authority stays with the item store, including status changes not yet rebuilt.
        if any((item := self.store.get_item(user_id, identity)) is None
               or item.status != 'active' for identity in row.payload.get('item_ids', [])):
            return None
        return row

    def search(self, user_id: str, query: str, *, exclude: str | None = None) -> list:
        with self.pool.connection() as conn:
            keys = [r.key for r in dreaming.list_summaries(conn, user_id, 'anchor')
                    if r.key != exclude]
        rows = [r for key in keys if (r := self.readable(user_id, key)) is not None]
        ranked = rank(user_id, 'anchors', [(r.key, render(r) + '\n' + '\n'.join(
            value['text'] for value in r.payload.get('learning', []))) for r in rows], query, k=5)
        by_key = {r.key: r for r in rows}
        return [by_key[key] for key, _ in ranked]

    def continuity(self, user_id: str, page_id: str | None, question: str = '') -> list:
        """Prior explicit goal discussions on this page, never inferred page membership."""
        page = self.readable(user_id, 'page:' + page_id) if page_id else None
        requested: set[str] = set()
        if self.repo is not None:
            state = self.repo.load(user_id).state
            goals = {**state.paths, **state.maps}
            requested.update('goal:' + identity for identity in goals
                             if re.search(r'(?<![\w-])' + re.escape(identity) + r'(?![\w-])', question))
            named = [identity for identity, goal in goals.items()
                     if (label := getattr(getattr(goal, 'path', goal), 'goal', ''))
                     and len(label) >= 2 and label in question
                     and re.search(r'目标|goal|继续|学习计划|\b(?:continue|learning plan|study plan)\b', question, re.IGNORECASE)]
            if len(named) == 1:
                requested.add('goal:' + named[0])
        keys = requested
        if not keys and page_id:
            from evowiki.store.db import fetch_all
            with self.pool.connection() as conn:
                rows = fetch_all(conn, "select distinct d.key from dream_source d "
                                 "join dream_summary summary on (summary.user_id,summary.kind,summary.key,summary.version)="
                                 "(d.user_id,d.kind,d.key,d.version) "
                                 "join assistant_session s on s.user_id=d.user_id "
                                 "and s.session_id=split_part(substring(d.source_id from 6),'/',1) "
                                 "where d.user_id=%s and d.kind='anchor' and left(d.key,5)='goal:' "
                                 "and left(d.source_id,5)='turn:' and s.page_id=%s", (user_id, page_id))
            keys = {row['key'] for row in rows}
        goals = [row for key in sorted(keys) if (row := self.readable(user_id, key)) is not None]
        goals.sort(key=lambda row: (row.updated_at, row.key), reverse=True)
        return ([page] if page is not None else []) + goals

    def expand(self, user_id: str, key: str, version: int, offset: int = 0) -> dict | None:
        row = self.readable(user_id, key)
        if row is None or row.version != version:
            return None
        nodes = [s for s in row.sources if s.startswith('turn:')]
        offset = max(0, offset)
        chunk = nodes[offset:offset + 20]
        return {'anchor': key, 'version': version, 'sources': chunk,
                'next_cursor': offset + len(chunk) if offset + len(chunk) < len(nodes) else None,
                'has_more': offset + len(chunk) < len(nodes)}

    def _session(self, uid: str, sid: str, items: list, assignments: dict | None = None) -> dict | None:
        try:
            info = self.sessions.info(sid)
        except LookupError:
            return None
        if info.user_id != uid:
            return None
        entries = self.sessions.entries(sid)
        forgotten = forgotten_of(self.store, uid)
        blocked = set(forgotten.messages)
        blocked.update(mid for suppression in self.store.suppressions(uid)
                       for mid in suppression.evidence_message_ids)
        turns = [(index, t) for index, t in enumerate(fold(info, entries).turns)
                 if t.answer and not t.stopped and t.message_id not in blocked]
        tools: dict[str, list] = {}
        for entry in entries:
            if entry.type == 'tool' and entry.payload.get('ok'):
                tools.setdefault(entry.payload.get('message_id'), []).append(entry.payload)
        users = {e.payload['message_id']: e for e in entries if e.type == 'user'}
        goals = {}
        if self.repo is not None:
            state = self.repo.load(uid).state
            goals = {**state.paths, **state.maps}
        records = []
        for index, turn in turns:
            node = f'turn:{sid}/{turn.message_id}'
            # Explicit IDs are identities; titles are never used to invent an anchor.
            goal_ids = {g for g in goals if re.search(r'(?<![\w-])' + re.escape(g) + r'(?![\w-])',
                                                      turn.user_text)}
            goal_ids.update(tool['args']['goal_id'] for tool in tools.get(turn.message_id, [])
                            if tool.get('effect') not in ('read', 'network')
                            and tool.get('args', {}).get('goal_id') in goals)
            for identity, goal in goals.items():
                label = getattr(getattr(goal, 'path', goal), 'goal', '')
                if (isinstance(label, str) and len(label) >= 2 and label in turn.user_text
                        and re.search(r'目标|goal|继续|学习计划|\b(?:continue|learning plan|study plan)\b', turn.user_text, re.IGNORECASE)):
                    goal_ids.add(identity)
            if assignments and node in assignments:
                keys = set(assignments[node])
            elif len(goal_ids) == 1:
                keys = {'goal:' + next(iter(goal_ids))}
            elif info.page_id is not None:
                keys = {'page:' + info.page_id}
            else:
                keys = {'memory_item:' + item.item_id for item in items
                        if (any(ref.session_id == sid and ref.message_id == turn.message_id
                                for ref in item.evidence)
                            or re.search(r'(?<![\w-])' + re.escape(item.item_id) + r'(?![\w-])',
                                         turn.user_text))}
            user = users[turn.message_id]
            records.append({'node': node, 'keys': sorted(keys), 'question': turn.user_text,
                            'answer': forgotten.answer(turn.user_text, turn.answer),
                            'recorded_at': user.at.isoformat(), 'seq': user.seq, 'turn_index': index})
        with self.pool.connection() as conn:
            nodes = [r['node'] for r in records]
            if not dreaming.sources_visible(conn, uid, nodes):
                records = [r for r in records
                           if dreaming.sources_visible(conn, uid, [r['node']])]
        annotations = [e.model_dump(mode='json') for e in entries
                       if e.type in ('rating', 'decision', 'observed')]
        return {'info': info, 'records': records, 'annotations': annotations,
                'version': _hash([e.model_dump(mode='json') for e in entries])}

    def update(self, info: Any, *, gateway: Any, account: Any, now: datetime) -> bool:
        uid, sid = info.user_id, info.session_id
        with self.pool.connection() as conn:
            todo = fetch_one(conn, 'select * from dream_pending where user_id=%s and session_id=%s',
                             (uid, sid))
            if dreaming.paused(conn, uid):
                return False
            existing = dreaming.list_summaries(conn, uid, 'anchor')
        if todo is None or todo['revision'] <= todo['processed']:
            return True
        items = self.store.items(uid, kinds=['decision', 'open_question'])
        assignments: dict[str, set[str]] = {}
        for summary in existing:
            for source in summary.sources:
                if source.startswith('turn:'):
                    assignments.setdefault(source, set()).add(summary.key)
        current = self._session(uid, sid, items, assignments)
        if current is None:
            return False
        keys = {key for record in current['records'] for key in record['keys']}
        keys.update(r.key for r in existing if 'session:' + sid in r.sources)
        done = True
        for key in sorted(keys):
            if not self._exists(uid, key):
                continue
            if not self._update_key(uid, sid, key, items, gateway, account, now, assignments):
                done = False
        with self.pool.connection() as conn:
            row = fetch_one(conn, 'select revision from dream_pending where user_id=%s '
                            'and session_id=%s for update', (uid, sid))
            if row is None or row['revision'] != todo['revision']:
                return False
            if done:
                dreaming.completed(conn, uid, sid, todo['revision'])
        return done

    def _update_key(self, uid: str, sid: str, key: str, items: list, gateway: Any,
                    account: Any, now: datetime, assignments: dict) -> bool:
        with self.pool.connection() as conn:
            old = dreaming.get(conn, uid, 'anchor', key)
        members = {sid}
        if old:
            members.update(n[8:] for n in old.sources if n.startswith('session:'))
        states = {member: data for member in sorted(members)
                  if (data := self._session(uid, member, items, assignments)) is not None}
        records = {member: [r for r in data['records'] if key in r['keys']]
                   for member, data in states.items()}
        all_raw = {r['node']: r for group in records.values() for r in group}
        marks = dict(old.watermarks) if old else {}
        checkpoints = old.payload.get('checkpoints', {}) if old else {}
        active_items = [i for i in items if i.status == 'active' and (
            (key.startswith('page:') and i.scope_type == 'page' and i.scope_id == key[5:])
            or (key.startswith('goal:') and i.scope_type == 'goal' and i.scope_id == key[5:])
            or key == 'memory_item:' + i.item_id)]
        refs = [{'id': i.item_id, 'content': i.content} for i in active_items]
        reset = bool(old and not old.payload)
        for member, count in marks.items():
            group = records.get(member, [])
            checkpoint = checkpoints.get(member, {})
            if (count > len(group) or checkpoint.get('prefix') != _hash(group[:count])
                    or checkpoint.get('annotations') != _hash(states[member]['annotations']
                                                               if member in states else [])):
                reset = True
        if old and old.payload.get('items_fingerprint') != _hash(refs):
            reset = True
        if old:
            with self.pool.connection() as conn:
                if not dreaming.sources_visible(conn, uid, old.sources):
                    reset = True
        if reset:
            marks, checkpoints = {}, {}
        previous = [] if reset or old is None else old.payload.get('statements', [])
        learning = [] if reset or old is None else old.payload.get('learning', [])
        pending = [r for member, group in records.items() for r in group[int(marks.get(member, 0)):]]
        if not pending and old and not reset:
            if not old.dirty:
                return True
            # Dirty notifications may contain no new semantic input. Clear without a model call.
            with self.pool.connection() as conn:
                return dreaming.publish(conn, uid, 'anchor', key, expected_version=old.version,
                                        payload=old.payload, watermarks=marks, sources=old.sources,
                                        fingerprint=old.fingerprint, now=now)
        raw = {}
        kept_previous = []
        # The editable draft is bounded, and every sentence is accompanied by original text.
        for statement in previous:
            cited = statement.get('sources', [])
            if not set(cited) <= all_raw.keys() or len(set(raw) | set(cited)) > BATCH_TURNS // 2:
                continue
            kept_previous.append(statement)
            raw.update({node: all_raw[node] for node in cited})
        batch = pending[:BATCH_TURNS - len(raw)]
        raw.update({record['node']: record for record in batch})
        evidence_raw = {node: {'question': r['question'][:TURN_CHARS // 2],
                               'answer': r['answer'][:TURN_CHARS // 2],
                               'recorded_at': r['recorded_at'],
                               'question_truncated': len(r['question']) > TURN_CHARS // 2,
                               'answer_truncated': len(r['answer']) > TURN_CHARS // 2}
                        for node, r in raw.items()}
        assert sum(len(r['question']) + len(r['answer']) for r in evidence_raw.values()) <= RAW_CHARS
        # Read only annotations attached to this bounded group, never the entire chat again.
        feedback = {}
        for member, data in states.items():
            for entry in data['annotations']:
                value = entry['payload']
                mid = value.get('turn_id') or value.get('message_id')
                if entry['type'] == 'rating' and f'turn:{member}/{mid}' in raw:
                    feedback[member + '/' + mid] = {
                        'value': value.get('value'), 'note': str(value.get('note') or '')[:600],
                        'chips': value.get('chips', [])}

        sources = set(raw) | {'session:' + member for member in states}
        sources.update('item:' + i.item_id for i in active_items)
        if old and not reset:
            sources.update(old.sources)  # earlier generation inputs remain dependencies
        evidence = {'raw': evidence_raw,
                    'items': [{'id': item.item_id, 'content': item.content[:600],
                               'content_truncated': len(item.content) > 600}
                              for item in active_items if any(
                                  f'turn:{ref.session_id}/{ref.message_id}' in raw
                                  for ref in item.evidence)],
                    'feedback': feedback}
        fingerprint = _hash(evidence)
        reply = AnchorReply()
        if raw:
            request = LLMRequest(
                purpose='memory_anchor', prompt_version='anchor@v2', subject=key,
                system=('整理给定锚点的跨会话背景。所有陈述须核对给出的原文并引用完整 turn ID。'
                        '旧概要只是编辑底稿。不要复制权威条目状态或临时要求。推测标 hypothesis。'
                        '普通提问不能作为不理解；学习状态只接受用户明确的自我报告，'
                        '填写 concept 和 evidence_quote，后者必须逐字引用对应用户原文。'
                        '不要把助手的话当用户事实。保留关键变化，已有明确理解不因普通提问撤销。'
                        '学习记录填写独立 learning 列表，不受 24 条背景陈述上限影响。'
                        '带 truncated 标记的是原文片段，不能声称已读完整原文。'),
                prompt=json.dumps({'previous': kept_previous, **evidence}, ensure_ascii=False),
                params={'source_user_id': uid, 'source_nodes': sorted(sources)})
            with generation_guard(self.pool, uid):
                with self.pool.connection() as conn:
                    if not dreaming.sources_visible(conn, uid, sorted(sources)):
                        return False
                reply, call = gateway.call('generator', request, AnchorReply,
                                           account=account if gateway.metered else None)
                with self.pool.connection() as conn:
                    dreaming.register_consumption(conn, uid, f'call:{call.id}', sorted(sources))
        statements = []
        for statement in [*reply.statements, *reply.learning]:
            if not set(statement.sources) <= raw.keys():
                continue
            value = statement.model_dump()
            if statement.state in ('unclear', 'understood'):
                quote = statement.evidence_quote.strip()
                support = next((node for node in statement.sources
                                if quote and quote in evidence_raw[node]['question']), None)
                concept = statement.concept.strip().casefold()
                context = '\n'.join(evidence_raw[node]['question'] + '\n' +
                                    evidence_raw[node]['answer'] for node in statement.sources).casefold()
                if (support is None or not concept or concept not in context
                        or '?' in quote or '？' in quote):
                    continue
                unclear, understood = bool(_UNCLEAR.search(quote)), bool(_UNDERSTOOD.search(quote))
                if (statement.state == 'unclear' and not unclear
                        or statement.state == 'understood' and (unclear or not understood)):
                    continue
                record = raw[support]
                source_sid, mid = support[5:].split('/', 1)
                value.update(session_id=source_sid, message_id=mid,
                             recorded_at=record['recorded_at'])
                # Learning is independent of the bounded prose overview.
                learning.append(value)
            if statement in reply.statements:
                statements.append(value)
        latest = {}
        for value in sorted(learning, key=lambda v: (v['recorded_at'], v['session_id'], v['message_id'])):
            latest[value['concept'].strip().casefold()] = value
        for record in batch:
            member = record['node'][5:].split('/', 1)[0]
            marks[member] = int(marks.get(member, 0)) + 1
        for member, group in records.items():
            count = int(marks.get(member, 0))
            checkpoints[member] = {'prefix': _hash(group[:count]),
                                   'annotations': _hash(states[member]['annotations'])}
        payload = {'statements': statements, 'learning': list(latest.values()),
                   'item_ids': [i.item_id for i in active_items], 'checkpoints': checkpoints,
                   'items_fingerprint': _hash(refs),
                   'covered_turns': {member: max((r['turn_index'] + 1 for r in
                                                  group[:int(marks.get(member, 0))]), default=0)
                                     for member, group in records.items()}}
        with self.pool.connection() as conn:
            # Share publication's user lock with session writes/forget; inspect current input
            # again after the model finishes so a correction cannot publish stale evidence.
            conn.execute('select pg_advisory_xact_lock(hashtext(%s))', ('memory:user:' + uid,))
            conn.execute('select session_id from assistant_session where user_id=%s '
                         'and session_id=any(%s) order by session_id for update',
                         (uid, sorted(states)))
            for member, before in states.items():
                after = self._session(uid, member, self.store.items(uid, kinds=['decision', 'open_question']),
                                      assignments)
                if after is None or after['version'] != before['version']:
                    return False
            current_items = self.store.items(uid, kinds=['decision', 'open_question'])
            if _hash([i.model_dump(mode='json') for i in current_items]) != _hash(
                    [i.model_dump(mode='json') for i in items]):
                return False
            if not dreaming.publish(conn, uid, 'anchor', key,
                                    expected_version=old.version if old else 0,
                                    payload=payload, watermarks=marks, sources=sorted(sources),
                                    fingerprint=fingerprint, now=now):
                return False
        return all(int(marks.get(member, 0)) >= len(group) for member, group in records.items())
