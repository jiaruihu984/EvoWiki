"""0085_spec_item_provenance on a test database with old-shape teaching specs (as the dev DB held
them: `source` and a free-text `origin`, no `authority`): every old item is rewritten in the new
shape — default rules (dNN, source default, or rsi after an RSI edit) RSI's to tune with the base
constraints protected, everything else the user's own; item counts and texts unchanged; the
cached states of those users dropped; a NOTICE per user; a second run changes nothing; the log
loads again (SpecItem refuses the old shape)."""

from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from evowiki.content.models import Lang
from evowiki.store.migrate import MIGRATIONS_DIR
from evowiki.store.users import UserRow, create_user
from evowiki.teach.default_spec import DEFAULT_ITEMS
from evowiki.wikiapp.repo import PgRepo

T0 = datetime(2026, 10, 28, tzinfo=UTC)
SQL = (MIGRATIONS_DIR / "0085_spec_item_provenance.sql").read_text(encoding="utf-8")


def _old(item_id: str, text: str, source: str, origin: str | None = None, **kw) -> dict:
    return {"item_id": item_id, "text": text, "scope": "any", "domain": None, "source": source,
            "origin": origin, "helpful": 0.0, "harmful": 0.0, **kw}


DEFAULTS = [_old(i.item_id, i.text, "default") for i in DEFAULT_ITEMS]
CHAT = _old("si_1", "页面里多配几个能动手的交互组件", "rsi", "gp_1")
MINE = _old("si_2", "公式后面跟一句人话", "user", helpful=1.5)
EDITED_BY_RSI = _old("d04", "新词先用大白话说清", "rsi", "harness:r1")
EDITED_BY_USER = _old("d05", "一次只讲一个新东西", "user")


def _saved(version: int, items: list[dict], action: str, origin: str | None = None) -> dict:
    return {"type": "teaching_spec_saved", "action": action, "origin": origin, "evidence": {},
            "spec": {"version": version, "items": items, "exemplars": [],
                     "params": {"recap_max_per_page": 4, "analogy_level": 1, "fading_level": 1,
                                "interactive_bias": 1}}}


EVENTS = [   # (user, seq, payload)
    ("me", 1, _saved(1, DEFAULTS, "migrated")),
    ("me", 2, _saved(2, [*DEFAULTS, CHAT], "guideline", "gp_1")),
    ("u2", 1, _saved(1, DEFAULTS, "migrated")),
    ("u2", 2, _saved(2, [*DEFAULTS[:3], EDITED_BY_RSI, EDITED_BY_USER, *DEFAULTS[5:], MINE], "rsi",
                     "harness:r1")),
]


def _run(conn) -> list[str]:
    notes: list[str] = []

    def note(d) -> None:
        notes.append(d.message_primary or "")

    conn.add_notice_handler(note)
    conn.execute(SQL)
    conn.commit()
    conn.remove_notice_handler(note)
    return notes


def test_0085_rewrites_old_items_defaults_rsi_the_rest_the_users(admin_pool, pool):
    # the migration runs as the migrating superuser (0082: the app role owns no table)
    with admin_pool.connection() as conn:
        for u in ("me", "u2"):
            create_user(conn, UserRow(user_id=u, background="none", condition="S2", lang=Lang.EN,
                                      created_at=T0))
        for i, (u, seq, payload) in enumerate(EVENTS):
            conn.execute("insert into wiki_event (user_id, seq, event_id, occurred_at, type, "
                         "payload) values (%s, %s, %s, %s, 'teaching_spec_saved', %s)",
                         (u, seq, f"wev_{i}", T0, Jsonb(payload)))
        conn.execute("insert into wiki_snapshot (user_id, wiki_seq, reducer, state) "
                     "values ('me', 2, 'old', '{}')")
        conn.commit()

        notes = _run(conn)
        assert notes == [
            "0085 me: 2 event(s): 44 default rule item(s) (RSI's to tune), 1 the user's own",
            "0085 u2: 2 event(s): 43 default rule item(s) (RSI's to tune), 2 the user's own"]
        assert conn.execute("select count(*) as n from wiki_snapshot").fetchone()["n"] == 0
        assert conn.execute("select count(*) as n from wiki_event where payload::text like "
                            "'%\"source\"%'").fetchone()["n"] == 0
        before = {r["event_id"]: r["payload"] for r in conn.execute(
            "select event_id, payload from wiki_event").fetchall()}
        assert _run(conn) == []                                   # idempotent
        assert {r["event_id"]: r["payload"] for r in conn.execute(
            "select event_id, payload from wiki_event").fetchall()} == before

    me = PgRepo(pool).load("me").state.spec               # the log loads again
    assert me is not None and len(me.items) == 23              # same count as before
    by = {i.item_id: i for i in me.items}
    assert [i.text for i in me.items[:22]] == [i.text for i in DEFAULT_ITEMS]
    assert all((by[i.item_id].origin, by[i.item_id].authority, by[i.item_id].rev_by)
               == ("default", "rsi", "system") for i in DEFAULT_ITEMS)
    assert {i for i, x in by.items() if x.protected} == {"d12", "d16", "d18", "d20"}
    chat = by["si_1"]
    assert (chat.origin, chat.authority, chat.origin_ref, chat.rev) == ("user_ui", "user", "gp_1", 1)

    u2 = {i.item_id: i for i in PgRepo(pool).load("u2").state.spec.items}
    assert (u2["d04"].origin, u2["d04"].authority, u2["d04"].rev_by, u2["d04"].origin_ref) == (
        "default", "rsi", "rsi", "harness:r1")
    assert (u2["d05"].origin, u2["d05"].authority) == ("user_ui", "user")
    assert (u2["si_2"].origin, u2["si_2"].authority, u2["si_2"].helpful) == ("user_ui", "user", 1.5)
