from datetime import UTC, datetime

import pytest

from evowiki.content.models import Lang
from evowiki.forge import undo
from evowiki.forge.api import ForgeDeps, propose
from evowiki.forge.binding import ForgeConverse
from evowiki.forge.models import ForgeMisconfigured, ToolExample, ToolRequest
from evowiki.forge.registry import promote
from evowiki.llm.usage import Account
from evowiki.store.users import UserRow, create_user
from tests.forge.fakes import InProcessSandbox, accounted_fake
from tests.forge.test_propose import EVI, REQ, Ids, Trace, script

T0 = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)


@pytest.fixture
def user(pool):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2",
                                  lang=Lang.EN, created_at=T0))
    return "u1"


def _deps(pool, converse):
    return ForgeDeps(pool=pool, converse=converse, sandbox=InProcessSandbox(), trace=Trace(),
                     now=lambda: T0, new_id=Ids())


def test_unaccounted_converse_is_refused_before_anything(pool, user):
    raw = script()
    with pytest.raises(ForgeMisconfigured):
        propose("tool", REQ, EVI, user_id="u1", deps=_deps(pool, raw))
    assert raw.requests == []                       # no model call
    with pool.connection() as conn:
        n = conn.execute("select count(*) as n from forge_candidate").fetchone()["n"]
    assert n == 0                                   # no candidate row


def test_accounted_converse_passes_account_and_sums_cost(pool, user):
    seen = []
    inner = script()

    class Gw:                                       # the slice of Gateway that ForgeConverse uses
        def converse(self, role, req, *, account=None):
            seen.append(account)
            t, c = inner(req)
            return t, c.model_copy(update={"cost": 0.01})

    fc = ForgeConverse(gateway=Gw(), account=Account(user_id="u1", group="other"))
    res = propose("tool", REQ, EVI, user_id="u1", deps=_deps(pool, fc))
    assert len(seen) == 4 and all(a == Account(user_id="u1", group="other") for a in seen)
    assert res.build.cost_usd == pytest.approx(0.04)


def test_title_is_required_and_short():
    ex = (ToolExample(args={"n": 2}, expect={"n": 4}),)
    for bad in ("", "很", "很" * 17):
        with pytest.raises(ValueError):
            ToolRequest(name="double", title=bad, purpose="p", agent="deep", examples=ex)
    with pytest.raises(ValueError):
        ToolRequest(name="double", purpose="p", agent="deep", examples=ex)


def test_change_row_uses_title(pool, user):
    res = propose("tool", REQ, EVI, user_id="u1",
                  deps=_deps(pool, accounted_fake(script(), cost=0.01)))
    assert res.build.ok
    with pool.connection() as conn:
        promote(conn, res.candidate_id, gate_ref="gate:1", by="improver", why="好",
                gate_ok=lambda ref: True)
        row = undo.changes(conn, "u1")[0]
    assert row["text"] == "新增了工具「翻倍」"
    assert "把 n 翻倍" in row["why"] and row["cost_usd"] == pytest.approx(0.04)
