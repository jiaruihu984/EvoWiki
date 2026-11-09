from datetime import UTC, datetime

from evowiki.content.models import Lang
from evowiki.forge import index
from evowiki.forge.registry import ForgedTool
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.store import forge as store
from evowiki.store.users import UserRow, create_user

T0 = datetime(2026, 10, 22, tzinfo=UTC)


def _tool(name, cid, desc):
    return ForgedTool(name=name, candidate_id=cid, files={},
                      schema={"description": desc, "type": "object",
                              "properties": {"x": {"type": "string", "description": "输入"}}})


def _setup(pool):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))
        for cid, name in (("fc_a", "balance_check"), ("fc_b", "frac_equiv")):
            store.insert_candidate(conn, store.CandidateRow(
                id=cid, user_id="u1", kind="tool", name=name, parent_id=None, spec={},
                evidence={}, files={}, build=None, status="built", run_id=None, cost_usd=None,
                created_at=T0, updated_at=T0))
    return {
        "balance_check": _tool("balance_check", "fc_a", "配平检查：判断方程式两边原子数是否相等"),
        "frac_equiv": _tool("frac_equiv", "fc_b", "分数等价：比较两个分数是否相等"),
    }


def _count(conn):
    row = conn.execute("select count(*) as n from forge_tool_vec").fetchone()
    return row["n"] if isinstance(row, dict) else row[0]


def test_search_ranks_by_description(pool):
    tools = _setup(pool)
    emb = HashingEmbedder(dim=256)
    with pool.connection() as conn:
        got = index.search(conn, emb, "判断方程式两边原子数", tools, 5)
        best = index.nearest(conn, emb, "判断方程式两边原子数", tools)
    assert [n for n, _ in got] == ["balance_check", "frac_equiv"]
    assert got[0][1] > got[1][1]
    assert best[0] == "balance_check"


def test_ensure_is_idempotent_and_tags_are_separate(pool):
    tools = _setup(pool)
    emb = HashingEmbedder(dim=256)
    pairs = [(t, t.name) for t in tools.values()]
    with pool.connection() as conn:
        index.ensure(conn, emb, pairs)
        index.ensure(conn, emb, pairs)
        assert _count(conn) == 2
        index.ensure(conn, HashingEmbedder(dim=64), pairs)   # another model_version: re-embedded
        assert _count(conn) == 4
        assert set(store.vecs(conn, ["fc_a"], model_version=emb.model_version)) == {"fc_a"}


def test_empty_query_is_name_order(pool):
    tools = _setup(pool)
    with pool.connection() as conn:
        got = index.search(conn, HashingEmbedder(dim=256), "  ", tools, 5)
        assert index.search(conn, HashingEmbedder(dim=256), "x", {}, 3) == []
    assert got == [("balance_check", 0.0), ("frac_equiv", 0.0)]
