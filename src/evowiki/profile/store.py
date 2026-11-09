"""The canonical profile per user (`learner_profile`, migration 0077), and an in-memory twin."""

from collections.abc import Callable
from datetime import datetime
from threading import Lock
from typing import Protocol

from psycopg.errors import ForeignKeyViolation
from psycopg.types.json import Jsonb

from evowiki.profile.note import Profile
from evowiki.store.users import UnknownUser


class ProfileStore(Protocol):
    def get(self, user_id: str) -> Profile: ...

    def update(self, user_id: str, fn: Callable[[Profile], Profile]) -> Profile: ...
    # read, change and write one user's profile atomically; returns the new one


class MemoryProfileStore:
    def __init__(self) -> None:
        self._rows: dict[str, Profile] = {}
        self._lock = Lock()

    def get(self, user_id: str) -> Profile:
        return self._rows.get(user_id, Profile())

    def update(self, user_id: str, fn: Callable[[Profile], Profile]) -> Profile:
        with self._lock:
            new = self._rows[user_id] = fn(self.get(user_id))
            return new

    def update_in(self, conn, user_id: str, fn: Callable[[Profile], Profile]) -> Profile:
        return self.update(user_id, fn)


class PgProfileStore:
    def __init__(self, pool, clock: Callable[[], datetime]):
        self._pool, self._clock = pool, clock

    def get(self, user_id: str) -> Profile:
        with self._pool.connection() as conn:
            row = conn.execute("select body from learner_profile where user_id = %s",
                               (user_id,)).fetchone()
        return Profile() if row is None else Profile.model_validate(row["body"])

    def update(self, user_id: str, fn: Callable[[Profile], Profile]) -> Profile:
        with self._pool.connection() as conn, conn.transaction():
            return self.update_in(conn, user_id, fn)

    def update_in(self, conn, user_id: str, fn: Callable[[Profile], Profile]) -> Profile:
        """`update` inside the caller's transaction (a ledger row's effect commits with it)."""
        try:
            conn.execute("insert into learner_profile (user_id, body, updated_at) "
                         "values (%s, '{}'::jsonb, %s) on conflict (user_id) do nothing",
                         (user_id, self._clock()))
        except ForeignKeyViolation as exc:
            raise UnknownUser(user_id) from exc
        row = conn.execute("select body from learner_profile where user_id = %s for update",
                           (user_id,)).fetchone()
        new = fn(Profile.model_validate(row["body"]))
        conn.execute("update learner_profile set body = %s, updated_at = %s where user_id = %s",
                     (Jsonb(new.model_dump(mode="json")), self._clock(), user_id))
        return new
