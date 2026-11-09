"""0079_drop_map_known: a stored goal map from before the 「整块已会只按 K 算」 ruling (a node with state
"known" and `known_why` on every node, a GoalMapSaved with action "known") loads after the rewrite:
the known node is collapsed, `known_why` is gone, the action is "diagnosed"; the cached states are
dropped."""

import json
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from evowiki.content.models import Lang
from evowiki.goals.regions import GoalMap, MapNode
from evowiki.store.migrate import MIGRATIONS_DIR
from evowiki.store.users import UserRow, create_user
from evowiki.wikiapp.events import GoalMapSaved
from evowiki.wikiapp.repo import PgRepo, _from_row

T0 = datetime(2026, 10, 28, tzinfo=UTC)
SQL = (MIGRATIONS_DIR / "0079_drop_map_known.sql").read_text(encoding="utf-8")


def _old_payload() -> dict:
    m = GoalMap(map_id="map-x", goal="学概率", created_at=T0, nodes=(
        MapNode(id="c1", kind="region", name="概率与生成函数"),
        MapNode(id="c2", kind="region", name="随机变量", state="expanded"),
        MapNode(id="c3", kind="concept", name="期望", parent="c2", state="expanded")))
    payload = GoalMapSaved(record=m, action="planned").model_dump(mode="json")
    payload["action"] = "known"
    for n in payload["record"]["nodes"]:
        n["known_why"] = ""
    payload["record"]["nodes"][0].update(state="known", known_why="诊断：会的概率 95%（小测）")
    return payload


def test_0079_rewrites_old_maps_so_they_load(pool, admin_pool):
    old = _old_payload()
    with pytest.raises(ValidationError):
        _from_row("wev_1", T0, old)
    with admin_pool.connection() as conn:           # migration SQL runs as the owner
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))
        conn.execute("insert into wiki_event (user_id, seq, event_id, occurred_at, type, payload) "
                     "values ('u1', 1, 'wev_1', %s, 'goal_map_saved', %s)", (T0, Jsonb(old)))
        conn.execute("insert into wiki_snapshot (user_id, wiki_seq, reducer, state) "
                     "values ('u1', 1, 'old', '{}')")
        conn.commit()
        with pytest.raises(psycopg.Error):          # still append-only outside the migration
            conn.execute("update wiki_event set payload = '{}' where event_id = 'wev_1'")
        conn.rollback()
        conn.execute(SQL)
        conn.commit()
        payload = conn.execute("select payload from wiki_event where event_id = 'wev_1'"
                               ).fetchone()["payload"]
        assert conn.execute("select count(*) as n from wiki_snapshot").fetchone()["n"] == 0
        with pytest.raises(psycopg.Error):          # the trigger is back on
            conn.execute("update wiki_event set payload = '{}' where event_id = 'wev_1'")
        conn.rollback()
    assert payload["action"] == "diagnosed"
    assert [(n["id"], n["state"]) for n in payload["record"]["nodes"]] == [
        ("c1", "collapsed"), ("c2", "expanded"), ("c3", "expanded")]
    assert "known_why" not in json.dumps(payload)
    m = PgRepo(pool).load("u1").state.maps["map-x"]
    assert [(n.id, n.state, n.name) for n in m.nodes] == [
        ("c1", "collapsed", "概率与生成函数"), ("c2", "expanded", "随机变量"),
        ("c3", "expanded", "期望")]
