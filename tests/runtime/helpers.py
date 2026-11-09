import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evowiki.content.loaded import LoadedContent
from evowiki.content.models import Lang
from evowiki.runtime.context import RuntimeContext, prepare_runtime
from evowiki.runtime.policy import PolicyBundle
from evowiki.store.content import activate, import_snapshot_file
from evowiki.store.locks import user_writer
from evowiki.store.users import UserRow, create_user, start_session

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
SNAP = Path(__file__).resolve().parents[1] / "fixtures" / "runtime" / "content_snapshot.json"


class FixedClock:
    def __init__(self, t: datetime = T0):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> datetime:
        self.t = self.t + timedelta(**kw)
        return self.t


def load_fixture_content() -> LoadedContent:
    return LoadedContent.from_document(json.loads(SNAP.read_text(encoding="utf-8")))


def seed_runtime(pool, *, policy: PolicyBundle | None = None) -> RuntimeContext:
    with pool.connection() as conn:
        cv = import_snapshot_file(conn, SNAP, T0)
        activate(conn, cv, T0)
    return prepare_runtime(pool, load_fixture_content(), policy or PolicyBundle.default(), T0)


def make_user(pool, user_id: str, *, background: str = "none", condition: str = "S2",
              lang: Lang = Lang.EN, now: datetime = T0, session_id: str | None = None) -> str:
    sid = session_id or f"{user_id}-s1"
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id=user_id, background=background, condition=condition,
                                  lang=lang, created_at=now))
    with user_writer(pool, user_id) as tx:
        start_session(tx, sid, now)
    return sid
