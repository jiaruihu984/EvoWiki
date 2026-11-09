"""Disposable backend for the 👎-into-memory walkthrough in REAL Obsidian (driver
obsidian-plugin/scripts/real-obsidian/memory-feedback-walkthrough.mjs). Test-only, no paid call.

The ask_user backend (tests/e2e/ask_backend.py: build_services on a fresh Postgres, DBOS, every
model scripted) with one more scripted chat: RELATED (in a new conversation, about the same
topic as ask_backend.ASK) → a plain answer. Every assistant request's `origin="memory"` messages
(the recall block the model gets) are kept.

GET /test/recall: [{"said", "memory": [text...]}] per assistant request.
GET /test/episodes (header X-Evowiki-User): the user's memory_episode rows (session, message,
answer_text) and how many memory_item rows he has.

  uv run python -m tests.e2e.memory_feedback_backend --port 8857 --user memfb-user
"""

from typing import Any

from fastapi import Request

from evowiki.llm.chat import ChatRequest, ChatTurn
from tests.e2e import ask_backend as qb
from tests.e2e import audit_backend as ab

RELATED = "主成分分析到底在做什么"
ANSWER = "主成分分析找方差最大的方向，把数据投到这些方向上。"
RECALL: list[dict[str, Any]] = []
_inner = ab.assistant_turn          # ask_backend's (it replaced audit_backend's)


def assistant_turn(script, request: ChatRequest) -> ChatTurn:
    said, _ = ab._said(request)
    RECALL.append({"said": said.rsplit("他问：\n", 1)[-1],
                   "memory": [m.text for m in request.messages
                              if getattr(m, "origin", None) == "memory"]})
    if said.rsplit("他问：\n", 1)[-1] == RELATED:
        return ChatTurn(text=ANSWER)
    return _inner(script, request)


ab.assistant_turn = assistant_turn
_mount = ab.mount_script_status


def mount(app, script) -> None:
    _mount(app, script)

    @app.get("/test/recall")
    def _recall() -> list[dict[str, Any]]:
        return RECALL

    @app.get("/test/episodes")
    def _episodes(request: Request) -> dict[str, Any]:
        uid = request.headers["X-Evowiki-User"]
        with app.state.services.pool.connection() as conn:
            eps = conn.execute("select session_id, message_id, answer_text, "
                               "embedding is not null as has_vector from memory_episode "
                               "where user_id = %s order by at", (uid,)).fetchall()
            items = conn.execute("select count(*) as n from memory_item where user_id = %s",
                                 (uid,)).fetchone()["n"]
        return {"episodes": eps, "items": items}


ab.mount_script_status = mount

if __name__ == "__main__":
    assert qb.ASK                   # the ask script is installed by importing it
    ab.main()
