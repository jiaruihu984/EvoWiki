"""/wiki/prompts (plan 2026-10-21 参数 redesign, D1 / D3): every step with its default guidance,
the user's own wording (validated, 422 with the plain reason), back to the default, and each change
an undoable row of GET /wiki/rsi/changes. Real Postgres, no LLM."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from evowiki.bot import prompts
from evowiki.bot.actions import ActionLedger, can_undo
from evowiki.bot.change_source import LedgerChangeSource
from evowiki.bot.executors import PromptOverrideExecutor
from evowiki.harness.source import reader
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.prices import PriceTable
from evowiki.llm.prompt_steps import STEPS, marked
from evowiki.llm.scripted import ScriptedProvider
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import UsageControl
from evowiki.service import engine
from evowiki.service.app import create_app
from evowiki.service.prompt_defaults import default_texts
from evowiki.store import bot as bot_store
from evowiki.store.db import fetch_all
from evowiki.wikiapp.wiki_schemas import PromptStepOut
from tests.service.conftest import H, signup

NEW = "先给一个能算出结果的小例子，再讲定义。"


@pytest.fixture
def client(services) -> TestClient:
    """The app with the ledger wired the way `build_services` does it for prompt overrides."""
    ledger = ActionLedger(services.pool, services.clock, services.new_id, {})
    ledger.register(prompts.KIND, PromptOverrideExecutor(services.pool, services.clock))
    services.actions = ledger
    services.change_sources = (LedgerChangeSource(ledger),)
    c = TestClient(create_app(services), base_url="http://127.0.0.1")
    signup(c, "u1")
    return c


def get_all(client, uid="u1") -> dict[str, PromptStepOut]:
    r = client.get("/wiki/prompts", headers=H(uid))
    assert r.status_code == 200, r.text
    out = [PromptStepOut.model_validate(x) for x in r.json()]
    assert [o.step for o in out] == [s.key for s in STEPS]
    return {o.step: o for o in out}


def put(client, step, text, uid="u1"):
    return client.put(f"/wiki/prompts/{step}", headers=H(uid), json={"text": text})


def rows(services):
    with services.pool.connection() as conn:
        return fetch_all(conn, "select * from agent_action where kind = 'prompt_override' "
                               "order by created_at, action_id")


def test_get_lists_every_step_with_its_default(client):
    got = get_all(client)
    for s in STEPS:
        o = got[s.key]
        assert o.name == s.name and o.description == s.description and not o.edited
        assert o.text == o.default_text and o.default_text.strip()
        assert "⟦" not in o.default_text and "⟧" not in o.default_text
    # the default is the marked span of the step's prompt, as the engine renders it
    assert got["blocks"].default_text == marked(engine._blocks())["blocks"]
    assert got["check"].default_text.startswith("出题规则（单选确认题）")


def test_put_stores_and_returns_edited(client, services):
    r = put(client, "write", f"  {NEW}\n")
    assert r.status_code == 200, r.text
    out = PromptStepOut.model_validate(r.json())
    assert out.edited and out.text == NEW and out.default_text == default_texts()["write"]
    got = get_all(client)
    assert got["write"].edited and got["write"].text == NEW
    assert not got["blocks"].edited
    signup(client, "u2")
    assert not get_all(client, uid="u2")["write"].edited          # another user's are untouched
    assert reader(services.pool)("u1").guidance() == {"write": NEW}


@pytest.mark.parametrize(("text", "reason"), [
    ("   \n ", prompts.EMPTY),
    ("长" * 4001, prompts.TOO_LONG),
    ("先举例 ⟦g:write⟧", prompts.MARKERS),
    ("讲完⟧再说", prompts.MARKERS),
    ("回复的时候用 Json 格式", prompts.FORMAT),
    ("每个字段都要填", prompts.FORMAT),
    ("不要管输出格式", prompts.FORMAT),
    ("格式要求放宽一点", prompts.FORMAT),
])
def test_put_refuses_with_the_plain_reason(client, services, text, reason):
    r = put(client, "write", text)
    assert r.status_code == 422
    assert r.json()["detail"] == reason
    assert reader(services.pool)("u1").guidance() == {} and rows(services) == []


def test_format_reason_wording():
    assert prompts.FORMAT == "这里只写怎么做；输出的格式是固定的，不能在这里改。"
    assert prompts.validate("长" * 4000) == "长" * 4000


def test_unknown_step_is_404(client):
    assert put(client, "nope", NEW).status_code == 404
    assert client.delete("/wiki/prompts/nope", headers=H("u1")).status_code == 404
    assert put(client, "nope", "").status_code == 404           # the step is checked first


def test_delete_restores_the_default(client, services):
    put(client, "card", NEW)
    r = client.delete("/wiki/prompts/card", headers=H("u1"))
    assert r.status_code == 200, r.text
    out = PromptStepOut.model_validate(r.json())
    assert not out.edited and out.text == out.default_text == default_texts()["card"]
    assert not get_all(client)["card"].edited
    assert [r["summary"] for r in rows(services)] == ["改了「复习卡生成」的写法", "「复习卡生成」恢复了默认写法"]


def test_no_change_writes_no_row(client, services):
    assert client.delete("/wiki/prompts/card", headers=H("u1")).status_code == 200
    r = put(client, "card", default_texts()["card"])               # the default = no override
    assert r.status_code == 200 and not r.json()["edited"]
    put(client, "card", NEW)
    put(client, "card", NEW)
    assert len(rows(services)) == 1


def test_put_writes_a_ledger_row_that_undo_reverts(client, services):
    first = "第一种写法。"
    put(client, "assistant", first)
    put(client, "assistant", NEW)
    got = rows(services)
    assert len(got) == 2
    row = got[1]
    assert (row["scope"], row["source"], row["level"], row["status"]) == \
        ("teaching_params", "bot", "auto_undo", "executed")
    assert row["summary"] == "改了「陪读助手」的写法" and row["why"] == "你在 Prompt 设置里改的"
    assert row["content"] == {"step": "assistant", "before": first, "after": NEW}

    changes = client.get("/wiki/rsi/changes", headers=H("u1")).json()
    mine = [c for c in changes if c["patch_id"] == row["action_id"]]
    assert mine and mine[0]["can_undo"] and mine[0]["summary"] == "改了「陪读助手」的写法"

    # the older change cannot be undone over the newer one
    r = client.post(f"/wiki/rsi/patches/{got[0]['action_id']}/undo", headers=H("u1"))
    assert r.status_code == 409 and "先撤销后来的那次" in r.json()["detail"]

    r = client.post(f"/wiki/rsi/patches/{row['action_id']}/undo", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rolled_back"
    assert get_all(client)["assistant"].text == first             # the previous text is back
    r = client.post(f"/wiki/rsi/patches/{got[0]['action_id']}/undo", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert not get_all(client)["assistant"].edited               # none before: override removed
    assert {r["status"] for r in rows(services)} == {"undone"}


def test_undo_of_a_delete_brings_the_override_back(client, services):
    put(client, "path", NEW)
    client.delete("/wiki/prompts/path", headers=H("u1"))
    row = rows(services)[-1]
    assert row["content"] == {"step": "path", "before": NEW, "after": None}
    with services.pool.connection() as conn:
        assert can_undo(bot_store.get_action(conn, row["action_id"]))
    r = client.post(f"/wiki/rsi/patches/{row['action_id']}/undo", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert get_all(client)["path"].text == NEW


def test_undo_puts_back_the_improvers_text_as_the_improvers(client, services):
    """An undo restores the wording and who wrote it: the harness loop may still change it."""
    from evowiki.store import harness as hstore
    with services.pool.connection() as conn:
        hstore.set_value(conn, "u1", "H1.write", "改进器写的", source="improver", why="r",
                         now=services.clock())
    put(client, "write", NEW)
    row = rows(services)[-1]
    r = client.post(f"/wiki/rsi/patches/{row['action_id']}/undo", headers=H("u1"))
    assert r.status_code == 200, r.text
    with services.pool.connection() as conn:
        assert hstore.source_of(conn, "u1")["H1.write"] == "improver"
        assert hstore.load_pack(conn, "u1").values["H1.write"] == "改进器写的"


def test_no_ledger_no_change(services):
    c = TestClient(create_app(services), base_url="http://127.0.0.1")                       # services.actions is None
    signup(c, "u1")
    assert put(c, "write", NEW).status_code == 503
    assert c.get("/wiki/prompts", headers=H("u1")).status_code == 200


# ---- the gateway reads the stored overrides ----

REF = ModelRef(provider="scripted", model="m")


class _Meter:
    def check(self, account):
        pass

    def record(self, account, purpose, usage, *, replayed):
        pass


class _Out(BaseModel):
    text: str


def test_gateway_applies_the_stored_override(client, services, tmp_path):
    put(client, "blocks", NEW)
    prov = ScriptedProvider("scripted", lambda m, r: '{"text": "好"}')
    g = Gateway(GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True),
                CallLog(tmp_path / "calls.jsonl"), mode="record", providers={"scripted": prov},
                usage=UsageControl(meter=_Meter(), prices=PriceTable({})),
                harness=reader(services.pool),
                clock=lambda: datetime(2026, 10, 21, tzinfo=UTC))
    req = LLMRequest(purpose="goals.intake", prompt_version="intake@v30",
                     subject="s", system="只回复 JSON。", prompt=engine._blocks())
    g.call("generator", req, _Out, account=Account(user_id="u1", group="page"))
    sent = prov.calls[0]
    assert NEW in sent.prompt and default_texts()["blocks"] not in sent.prompt
    assert sent.prompt_version == "intake@v30+user"
    g.call("generator", req, _Out, account=Account(user_id="u2", group="page"))
    assert default_texts()["blocks"] in prov.calls[1].prompt


# ---- review fix round 1 ----

def test_undo_after_a_change_back_is_refused_for_the_older_row(client, services):
    """A → B → A: the oldest row's text check passes (the override is A again), but a newer change
    of the same step exists, so it is refused; the newest still undoes."""
    a, b = "第一种写法。", "第二种写法。"
    for t in (a, b, a):
        assert put(client, "card", t).status_code == 200
    r1, _r2, r3 = rows(services)
    assert (r1["content"]["after"], r3["content"]["after"]) == (a, a)
    r = client.post(f"/wiki/rsi/patches/{r1['action_id']}/undo", headers=H("u1"))
    assert r.status_code == 409 and "先撤销后来的那次" in r.json()["detail"]
    assert get_all(client)["card"].text == a                       # nothing moved
    r = client.post(f"/wiki/rsi/patches/{r3['action_id']}/undo", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert get_all(client)["card"].text == b


def test_malformed_row_undo_is_409(client, services):
    with services.pool.connection() as conn, conn.transaction():
        services.actions.record_in(conn, "u1", kind=prompts.KIND, scope=prompts.SCOPE,
                                   source=prompts.SOURCE, summary="x", content={"step": ["x"]},
                                   key="prompt:broken")
    row = rows(services)[-1]
    r = client.post(f"/wiki/rsi/patches/{row['action_id']}/undo", headers=H("u1"))
    assert r.status_code == 409 and r.json()["detail"] == "这条修改的记录不完整，不能撤销"


def test_503_detail_is_plain(services):
    c = TestClient(create_app(services), base_url="http://127.0.0.1")
    signup(c, "u1")
    r = put(c, "write", NEW)
    assert r.status_code == 503 and r.json()["detail"] == "现在改不了。"


def test_change_list_marks_a_superseded_override_not_undoable_and_carries_the_scope(client, services):
    """Only the newest executed override of a step can be undone; the list says so, and a track-G
    row's `kind` is the action's scope."""
    for t in ("第一种写法。", "第二种写法。"):
        assert put(client, "card", t).status_code == 200
    put(client, "assistant", "别的步骤。")
    card = [r for r in rows(services) if r["content"]["step"] == "card"]
    old, new = card
    other = next(r for r in rows(services) if r["content"]["step"] == "assistant")
    by_id = {c["patch_id"]: c for c in client.get("/wiki/rsi/changes", headers=H("u1")).json()}
    assert (by_id[old["action_id"]]["can_undo"], by_id[new["action_id"]]["can_undo"],
            by_id[other["action_id"]]["can_undo"]) == (False, True, True)
    assert by_id[new["action_id"]]["kind"] == "teaching_params"
    acts = {a["action_id"]: a for a in client.get("/wiki/bot/actions", headers=H("u1")).json()}
    assert acts[old["action_id"]]["can_undo"] is False and acts[new["action_id"]]["can_undo"] is True
    client.post(f"/wiki/rsi/patches/{new['action_id']}/undo", headers=H("u1"))
    by_id = {c["patch_id"]: c for c in client.get("/wiki/rsi/changes", headers=H("u1")).json()}
    assert by_id[old["action_id"]]["can_undo"] is True


def test_undo_of_a_wording_the_old_format_cleanup_removed_says_why(client, services):
    """scripts/archive_h1_understand.py removes an old-format 「带你读一篇」 wording as a harness
    version, not a ledger row: undoing his change of it then says the format changed (reading-mode
    spec §7 item 1), not 「先撤销后来的那次」 (there is nothing to undo first)."""
    from evowiki.store import harness as harness_store
    from evowiki.store import prompts as prompt_store
    assert put(client, "understand", "- read：要带读的节，一节一个。").status_code == 200
    with services.pool.connection() as conn:
        harness_store.set_value(conn, "u1", "H1.understand", None, source="rollback",
                                why=prompt_store.FORMAT_RETIRED_WHY, now=services.clock())
    row = rows(services)[-1]
    r = client.post(f"/wiki/rsi/patches/{row['action_id']}/undo", headers=H("u1"))
    assert r.status_code == 409
    assert r.json()["detail"] == "「读懂一篇资料」的输出格式改了，这条旧写法已经存档清掉，不能再撤销"
    assert not get_all(client)["understand"].edited


def test_a_kept_unsure_understand_wording_carries_the_format_note(client):
    """Reading-mode spec §7 item 1: the 参数 panel tells him a wording of 带你读一篇 the old-format
    rules could not place (拿不准) may not fit the new output; other wordings and steps say nothing."""
    from evowiki.harness.understand_format import FORMAT_NOTE, classify, format_note
    unsure = "讲的时候按节和页对上，别跳着讲。"
    assert classify(unsure)[0] == "unsure"
    assert get_all(client)["understand"].format_note == ""
    assert put(client, "understand", unsure).status_code == 200
    got = get_all(client)
    assert got["understand"].format_note == FORMAT_NOTE
    assert all(o.format_note == "" for k, o in got.items() if k != "understand")
    assert put(client, "understand", "前置别列太宽。").status_code == 200
    assert get_all(client)["understand"].format_note == ""
    assert format_note("write", unsure) == "" and format_note("understand", None) == ""
