"""/wiki/rsi/changes and /wiki/rsi/patches/{id}/undo across layers: layer-2 method changes and
external change sources (teach.change_source) share the change list and the undo route."""

import logging
from datetime import timedelta

from evowiki.store.rsi_layer2 import PgLayer2Store
from evowiki.teach.contracts import ChangeOut, MethodChange
from evowiki.teach.layer2 import method_registry
from tests.runtime.helpers import T0
from tests.service.conftest import H, signup


def _changes(client, uid="u1") -> list[ChangeOut]:
    r = client.get("/wiki/rsi/changes", headers=H(uid))
    assert r.status_code == 200, r.text
    return [ChangeOut.model_validate(c) for c in r.json()]


DEFAULT = method_registry.DEFAULT_FIT_MODEL


def _model(*paths: str):
    """The default FitModel without the given paths (a stand-in for an A2 move)."""
    return DEFAULT.model_copy(update={"paths": tuple(p for p in DEFAULT.paths if p not in paths)})


def _method_change(services, uid="u1", cid="mc_1"):
    l2 = PgLayer2Store(services.pool)
    method_registry.commit(l2, uid, "A2", fit_model=_model("slip"),
                           change_id=cid, now=T0 - timedelta(hours=1))
    l2.put_method_change(uid, MethodChange(change_id=cid, kind="A2", status="applied",
                                           summary="估计时不再考虑一项", why="猜得更准",
                                           from_version=1, to_version=2,
                                           created_at=T0 - timedelta(hours=1)))
    return l2


class Source:
    prefix = "g_"

    def __init__(self):
        self.undone = []

    def _row(self, status):
        return ChangeOut(patch_id="g_1", track="G", status=status, summary="把「方差」拆成两页",
                         why="一页讲了两件事", signals=(), at=T0, can_undo=status == "personal_accepted")

    def list(self, user_id):
        return [self._row("personal_accepted")]

    def undo(self, user_id, change_id, *, now):
        if change_id != "g_1":
            raise LookupError(change_id)
        if self.undone:
            raise ValueError("already undone")
        self.undone.append((user_id, change_id, now))
        return self._row("rolled_back")


class Broken:
    prefix = "x_"

    def list(self, user_id):
        raise RuntimeError("graph store down")

    def undo(self, user_id, change_id, *, now):
        raise AssertionError("not called")


def test_method_change_is_listed_and_undone(client, services):
    signup(client, "u1")
    signup(client, "u2")
    l2 = _method_change(services)
    (row,) = _changes(client)
    assert (row.patch_id, row.layer, row.target, row.track) == ("mc_1", 2, "估计方法", "A")
    assert row.status == "personal_accepted" and row.can_undo
    assert row.outcome == "pending" and row.effect == ()
    assert client.post("/wiki/rsi/patches/mc_1/undo", headers=H("u2")).status_code == 404
    r = client.post("/wiki/rsi/patches/mc_1/undo", headers=H("u1"))
    assert r.status_code == 200, r.text
    out = ChangeOut.model_validate(r.json())
    assert out.patch_id == "mc_1" and out.status == "rolled_back" and not out.can_undo
    assert not out.summary.startswith("已撤销")
    v = method_registry.current(l2, "u1", "A2")
    assert v.version == 3 and v.fit_model.paths == DEFAULT.paths
    assert client.post("/wiki/rsi/patches/mc_1/undo", headers=H("u1")).status_code == 409
    assert client.post("/wiki/rsi/patches/nope/undo", headers=H("u1")).status_code == 404


def test_external_source_rows_and_undo(client, services):
    signup(client, "u1")
    src = Source()
    services.change_sources = (src,)
    _method_change(services)
    rows = _changes(client)
    assert [r.patch_id for r in rows] == ["g_1", "mc_1"]           # newest first
    assert rows[0].track == "G"
    r = client.post("/wiki/rsi/patches/g_1/undo", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert ChangeOut.model_validate(r.json()).status == "rolled_back"
    assert src.undone == [("u1", "g_1", T0)]
    assert client.post("/wiki/rsi/patches/g_1/undo", headers=H("u1")).status_code == 409
    assert client.post("/wiki/rsi/patches/g_9/undo", headers=H("u1")).status_code == 404


def test_a_raising_source_does_not_break_the_list(client, services, caplog):
    signup(client, "u1")
    services.change_sources = (Broken(), Source())
    _method_change(services)
    with caplog.at_level(logging.ERROR, logger="evowiki.teach.cycle"):
        rows = _changes(client)
    assert {r.patch_id for r in rows} == {"g_1", "mc_1"}
    assert any("x_" in rec.getMessage() for rec in caplog.records)


def test_only_the_latest_method_change_is_undoable_over_the_route(client, services):
    signup(client, "u1")
    l2 = _method_change(services)                                    # mc_1: A2 v1 → v2
    method_registry.commit(l2, "u1", "A2", fit_model=_model("slip", "guess_scale"),
                           change_id="mc_2", now=T0 - timedelta(minutes=30))
    l2.put_method_change("u1", MethodChange(change_id="mc_2", kind="A2", status="applied",
                                            summary="估计时再少考虑一项", why="猜得更准",
                                            from_version=2, to_version=3,
                                            created_at=T0 - timedelta(minutes=30)))
    assert {r.patch_id: r.can_undo for r in _changes(client)} == {"mc_1": False, "mc_2": True}
    r = client.post("/wiki/rsi/patches/mc_1/undo", headers=H("u1"))
    assert r.status_code == 409 and "这项更改现在不能撤销" in r.text
    assert client.post("/wiki/rsi/patches/mc_2/undo", headers=H("u1")).status_code == 200
    assert {r.patch_id: r.can_undo for r in _changes(client)} == {"mc_1": True, "mc_2": False}
    r = client.post("/wiki/rsi/patches/mc_1/undo", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert method_registry.current(l2, "u1", "A2").fit_model.paths == DEFAULT.paths
