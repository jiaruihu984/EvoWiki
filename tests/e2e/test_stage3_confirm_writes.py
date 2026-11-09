"""What an approved draft writes, by card click and by 「好」 in chat alike.

A split (and a merge) only creates its pages; each is written when the learner opens it (the
learner's ruling 2026-10-29): no page_run row on approve, one when the page is opened. write_page
is an explicit "write this page now", so its approve still starts the write. (S3 live-run
follow-up: ConfirmTool approves inside the turn's DBOS step, where DBOS starts no workflow, so a
write request failed and the row only said `write_error`; the approve now runs off the step,
`durable.outside_dbos`, `actions.approve`.) Real DBOS, the stage-3 e2e backend's app with its
scripted assistant (no paid call)."""

from typing import Any

from fastapi.testclient import TestClient

from tests.e2e import stage3_backend as sb

H = {"X-Evowiki-User": sb.USER}


def _say(client: TestClient, sid: str, text: str) -> None:
    r = client.post(f"/assistant/sessions/{sid}/messages", json={"text": text}, headers=H)
    assert r.status_code == 200, r.text
    assert "event: done" in r.text, r.text[-600:]


def _rows(pool, sql: str, *args: Any) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        return list(conn.execute(sql, args).fetchall())


def _proposed(client, pool, text: str, kind: str) -> tuple[str, str]:
    sid = client.post("/assistant/sessions", json={}, headers=H).json()["session_id"]
    _say(client, sid, text)
    [row] = _rows(pool, "select action_id, status from agent_action where user_id = %s "
                        "and kind = %s and status = 'proposed'", sb.USER, kind)
    return sid, row["action_id"]


def _confirmed(client, pool, text: str, kind: str) -> dict[str, Any]:
    sid, action_id = _proposed(client, pool, text, kind)
    _say(client, sid, sb.OK)
    return _done(pool, action_id)


def _done(pool, action_id: str) -> dict[str, Any]:
    [done] = _rows(pool, "select status, result from agent_action where action_id = %s", action_id)
    return done


def _runs(pool) -> set[str]:
    return {r["page_id"] for r in _rows(pool, "select page_id from page_run where user_id = %s",
                                        sb.USER)}


def _opened(client, page_id: str) -> None:
    r = client.post(f"/wiki/pages/{page_id}/organize", headers=H)
    assert r.status_code == 200 and "event: done" in r.text, r.text[-500:]


def test_a_split_confirmed_in_chat_writes_nothing_until_a_page_is_opened(db_url, pool):
    sb.prepare(pool)
    app, _svc, _script = sb.build(db_url, pool)
    with TestClient(app) as client:
        before = _runs(pool)
        done = _confirmed(client, pool, sb.SPLIT, "page_split")
        result = done["result"] or {}
        assert done["status"] == "executed", done
        pages = result["new_pages"]
        assert len(pages) == 2 and not result.get("run_ids"), result
        assert "write_error" not in result and _runs(pool) == before
        _opened(client, pages[1])
        assert pages[1] in _runs(pool) and pages[0] not in _runs(pool)


def test_a_split_approved_on_its_card_writes_nothing_until_a_page_is_opened(db_url, pool):
    sb.prepare(pool)
    app, _svc, _script = sb.build(db_url, pool)
    with TestClient(app) as client:
        before = _runs(pool)
        _sid, action_id = _proposed(client, pool, sb.SPLIT, "page_split")
        r = client.post(f"/wiki/bot/actions/{action_id}/approve", headers=H)
        assert r.status_code == 200, r.text
        done = _done(pool, action_id)
        result = done["result"] or {}
        assert done["status"] == "executed", done
        pages = result["new_pages"]
        assert len(pages) == 2 and not result.get("run_ids"), result
        assert _runs(pool) == before
        _opened(client, pages[0])
        assert pages[0] in _runs(pool) and pages[1] not in _runs(pool)


def test_write_page_confirmed_in_chat_still_starts_its_write(db_url, pool):
    sb.prepare(pool)
    app, _svc, _script = sb.build(db_url, pool)
    with TestClient(app) as client:
        done = _confirmed(client, pool, sb.WRITE, "page_new")
        result = done["result"] or {}
        assert done["status"] == "executed", done
        assert "write_error" not in result, result
        pages = result["new_pages"]
        assert pages and result["run_ids"], result
        assert set(pages) <= _runs(pool), (pages, _runs(pool))
