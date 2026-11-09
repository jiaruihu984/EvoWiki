"""GET /wiki/engine: the 参数 tab's prompts in use and θ_infer,u (read-only, no LLM call)."""

from datetime import UTC, datetime

import pytest

from evowiki.deep.agent import PROMPT_VERSION as DEEP
from evowiki.store.rsi import PgRsiStore
from evowiki.store.usage import update_settings
from evowiki.teach import infer_paths as ip
from evowiki.teach.contracts import (
    InferParams,
    Localization,
    ParamPatchOp,
    Patch,
    PolicyVersion,
)
from evowiki.teach.rsi_store import PatchRecord
from evowiki.wikiapp.wiki_schemas import EngineOut
from tests.service.conftest import H, signup

STEPS = ["拆目标和摸底", "列大纲", "写每一段", "出确认题", "理解你的反馈", "复习卡"]
SELF = "evidence_weight.self"
AT = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def deepseek_mode(monkeypatch):
    """A fixed runtime config: DeepSeek mode, no Anthropic key, UTC dates."""
    monkeypatch.setenv("EVOWIKI_RUNTIME_PROVIDER", "deepseek")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for var in ("EVOWIKI_DEEPSEEK_MODEL", "EVOWIKI_DEEPSEEK_FAST_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EVOWIKI_USAGE_TZ", "UTC")


def engine(client, uid: str = "u1") -> EngineOut:
    r = client.get("/wiki/engine", headers=H(uid))
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"prompts", "params"}
    return EngineOut.model_validate(r.json())


def test_default_user(client):
    signup(client, "u1")
    out = engine(client)
    assert [p.step for p in out.prompts] == STEPS
    assert [(p.ver, p.model) for p in out.prompts] == [
        ("intake@v38 · enter@v5 · page-fit@v3", "DeepSeek · 好 / DeepSeek · 快"),
        (DEEP, "DeepSeek · 快"),     # the page writer never thinks (2026-10-28)
        (DEEP, "DeepSeek · 快"),
        (f"{DEEP} · followup-check@v5", "DeepSeek · 快"),
        ("feedback-understand@v14", "DeepSeek · 好"),
        ("card-rewrite@v9", "DeepSeek · 快")]
    assert all(p.text for p in out.prompts)
    assert [p.path for p in out.params] == list(ip.PATHS)
    assert all(p.default and p.tuned_at is None and p.why is None for p in out.params)
    row = next(p for p in out.params if p.path == SELF)
    assert (row.label, row.value) == ("自我评估权重", "0.6")
    assert next(p for p in out.params if p.path == "forgetting_scale").value == "× 1.0"


def test_user_tier_choice_changes_the_model(client, services):
    signup(client, "u1")
    with services.pool.connection() as conn:
        update_settings(conn, "u1", now=AT, tiers={"review": "good"})
    models = {p.step: p.model for p in engine(client).prompts}
    assert models["复习卡"] == "DeepSeek · 好"
    assert models["出确认题"] == "DeepSeek · 快 / DeepSeek · 好"   # writer, then followup
    assert models["列大纲"] == "DeepSeek · 快"                      # the writer's own plan_page


def test_tuned_param(client, services):
    signup(client, "u1")
    store = PgRsiStore(services.pool)
    loc = Localization(track="A", failure="overconfident", target="infer_param", ref=SELF,
                       signals=("sig1",), reason="自述偏高")
    store.put_patch(PatchRecord(user_id="u1", cutoff=None, patch=Patch(
        patch_id="pa1", track="A", localization=loc,
        body=ParamPatchOp(track="A", path=SELF, value=0.45), why="你的自述偏乐观",
        status="personal_accepted", from_version=1, to_version=2, created_at=AT)))
    store.add_version("u1", PolicyVersion(track="A", version=1, at=AT, infer=InferParams()))
    store.add_version("u1", PolicyVersion(track="A", version=2, at=AT, patch_id="pa1",
                                          infer=ip.set_(InferParams(), SELF, 0.45)))
    rows = {p.path: p for p in engine(client).params}
    s = rows[SELF]
    assert (s.value, s.default, s.tuned_at, s.why) == ("0.45", False, "2026-10-09", "你的自述偏乐观")
    assert all(p.default for p in rows.values() if p.path != SELF)
    raw = client.get("/wiki/engine", headers=H("u1")).json()["params"]
    assert set(raw[0]) == {"path", "label", "description", "value", "default", "tuned_at", "why"}


def test_unknown_user(client):
    assert client.get("/wiki/engine", headers=H("ghost")).status_code == 404
