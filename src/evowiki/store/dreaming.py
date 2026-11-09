"""Derived views with optimistic publication and complete input provenance."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from evowiki.store.db import fetch_all, fetch_one

MAX_FAILED = 5


@dataclass(frozen=True)
class Summary:
    user_id: str
    kind: str
    key: str
    version: int
    payload: dict
    watermarks: dict
    fingerprint: str
    dirty: bool
    updated_at: datetime
    sources: tuple[str, ...]

    @property
    def node(self) -> str:
        return f"dream:{self.kind}:{self.key}:{self.version}"


def paused(conn: Any, user_id: str) -> bool:
    return bool(fetch_one(conn, "select 1 from forget_request where user_id=%s "
                         "and state <> 'committed' limit 1", (user_id,)))


def sources_visible(conn: Any, user_id: str, sources: Sequence[str]) -> bool:
    if paused(conn, user_id):
        return False
    # A snapshot may have been read before deletion and consumed after its commit.
    # Neither the shared generation lock nor the pending-request check covers that gap.
    sessions = {source[8:] for source in sources if source.startswith('session:')}
    sessions.update(source.split(':', 1)[1].split('/', 1)[0] for source in sources
                    if source.startswith(('turn:', 'ep:', 'message:')))
    if sessions and fetch_one(conn, 'select 1 from forgotten_session where user_id=%s '
                              'and session_id=any(%s) limit 1', (user_id, sorted(sessions))):
        return False
    if sources and fetch_one(conn, 'select 1 from forget_frontier f join forget_request r '
                             'using(request_id) where r.user_id=%s and r.state=\'committed\' '
                             'and f.node=any(%s) limit 1', (user_id, list(sources))):
        return False
    if fetch_one(conn, 'select 1 from memory_suppression_closure where user_id=%s '
                 'and node=any(%s) limit 1', (user_id, list(sources))):
        return False
    suppressed = fetch_all(conn, 'select item_id,evidence_message_ids from memory_suppression '
                           'where user_id=%s', (user_id,))
    blocked = {message for row in suppressed for message in row['evidence_message_ids']}
    items = {row['item_id'] for row in suppressed}
    return not any((s.startswith('turn:') and s.rsplit('/', 1)[-1] in blocked)
                   or (s.startswith('item:') and s[5:] in items) for s in sources)


def _summary(conn: Any, r: dict) -> Summary:
    rows = fetch_all(conn, "select source_id from dream_source where user_id=%s and kind=%s "
                     "and key=%s and version=%s order by source_id",
                     (r['user_id'], r['kind'], r['key'], r['version']))
    return Summary(**r, sources=tuple(s['source_id'] for s in rows))


def get(conn: Any, user_id: str, kind: str, key: str) -> Summary | None:
    if paused(conn, user_id):
        return None
    r = fetch_one(conn, "select * from dream_summary where user_id=%s and kind=%s and key=%s",
                  (user_id, kind, key))
    return _summary(conn, r) if r else None


def list_summaries(conn: Any, user_id: str, kind: str) -> list[Summary]:
    if paused(conn, user_id):
        return []
    rows = fetch_all(conn, "select d.*,coalesce((select jsonb_agg(s.source_id order by s.source_id) "
                     "from dream_source s where s.user_id=d.user_id and s.kind=d.kind "
                     "and s.key=d.key and s.version=d.version),'[]'::jsonb) as sources "
                     "from dream_summary d where d.user_id=%s and d.kind=%s order by d.key",
                     (user_id, kind))
    return [Summary(**{**r, 'sources': tuple(r['sources'])}) for r in rows]


def publish(conn: Any, user_id: str, kind: str, key: str, *, expected_version: int,
            payload: dict, watermarks: dict, sources: Sequence[str], fingerprint: str,
            now: datetime) -> bool:
    conn.execute("select pg_advisory_xact_lock(hashtext(%s))", ('memory:user:' + user_id,))
    if not sources_visible(conn, user_id, sources):
        return False
    row = fetch_one(conn, "select version from dream_summary where user_id=%s and kind=%s "
                    "and key=%s for update", (user_id, kind, key))
    if (row['version'] if row else 0) != expected_version:
        return False
    version = expected_version + 1
    conn.execute("insert into dream_summary values(%s,%s,%s,%s,%s,%s,%s,false,%s) "
                 "on conflict(user_id,kind,key) do update set version=excluded.version,"
                 "payload=excluded.payload,watermarks=excluded.watermarks,"
                 "fingerprint=excluded.fingerprint,dirty=false,updated_at=excluded.updated_at",
                 (user_id, kind, key, version, Jsonb(payload), Jsonb(watermarks), fingerprint, now))
    node = f'dream:{kind}:{key}:{version}'
    for source in sorted(set(sources)):
        conn.execute("insert into dream_source values(%s,%s,%s,%s,%s) on conflict do nothing",
                     (user_id, kind, key, version, source))
        conn.execute("insert into memory_dependency(user_id,source,target,owner) "
                     "values(%s,%s,%s,%s) on conflict do nothing", (user_id, source, node, node))
    return True


def invalidate_sources(conn: Any, user_id: str, nodes: Sequence[str]) -> int:
    from evowiki.memory.lexical import invalidate
    invalidate(user_id)
    if not nodes:
        return 0
    conn.execute("update dream_pending set revision=revision+1,processed=0 where user_id=%s "
                 "and session_id in (select substring(s.source_id from 9) from dream_source s "
                 "join dream_source changed using(user_id,kind,key,version) "
                 "where s.user_id=%s and s.source_id like 'session:%%' "
                 "and changed.source_id=any(%s))", (user_id, user_id, list(nodes)))
    return conn.execute("update dream_summary set dirty=true,payload='{}',watermarks='{}' "
                        "where user_id=%s and (kind,key) in "
                        "(select kind,key from dream_source where user_id=%s and source_id=any(%s))",
                        (user_id, user_id, list(nodes))).rowcount


def register_consumption(conn: Any, user_id: str, node: str, sources: Sequence[str]) -> None:
    for source in sorted(set(sources)):
        conn.execute('insert into memory_dependency(user_id,source,target,owner) '
                     'values(%s,%s,%s,%s) on conflict do nothing', (user_id, source, node, node))


def pending(conn: Any, user_id: str, *, limit: int = 50) -> list[dict]:
    return fetch_all(conn, "select p.* from dream_pending p where user_id=%s and revision>processed "
                     "and (select count(*) from assistant_entry e where e.session_id=p.session_id "
                     "and e.type='wrapup_failed' and e.payload->>'version'=p.revision::text "
                     "and e.payload->>'reason'='failed') < %s "
                     "order by updated_at,session_id limit %s", (user_id, MAX_FAILED, limit))


def completed(conn: Any, user_id: str, session_id: str, revision: int) -> None:
    conn.execute("update dream_pending set processed=greatest(processed,%s) "
                 "where user_id=%s and session_id=%s", (revision, user_id, session_id))
