"""Disposable backend for the ask_user walkthrough in REAL Obsidian (driver
obsidian-plugin/scripts/real-obsidian/ask-walkthrough.mjs). Test-only, no paid call.

The audit backend (tests/e2e/audit_backend.py: build_services on a fresh Postgres, DBOS launched
by the app lifespan, assistant turns as DBOS workflows, every model a scripted fake) with one more
scripted chat: ASK → the assistant says one line and calls ask_user (two options); the turn ends
on the question. His next message resumes that run: the request then carries the ask_user call's
result (his message, framed by ask_user.REPLIED). An option he clicked → it goes on from his
answer; an unrelated question (VARIANTS["unrelated"]) → it answers that instead. Anything else goes
to the audit backend's own script.

GET /test/question-feedback (header X-Evowiki-User): the user's raw `rating` session entries (the
one record of a question's 👍 / 👎: call id, question, chips, note), the harness's read of them
(`assistant_feedback` signals, `rated_answers`) and the turn cases a round builds from them
(`assistant_cases`; the replay check is skipped here: a fake, nothing is replayed).

  uv run python -m tests.e2e.ask_backend --port 8791 --user ask-user
"""

from typing import Any

from fastapi import Request

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.tools.ask_user import REPLIED
from evowiki.harness.cases import assistant_cases
from evowiki.llm.chat import ChatRequest, ChatTurn, ToolCall
from evowiki.wikiapp.signals import assistant_feedback, rated_answers
from tests.e2e import audit_backend as ab

ASK = "我想先弄懂主成分分析"
QUESTION = "你学过线性代数吗？"
OPTIONS = ["学过", "没学过"]
# his next message in each variant of the walkthrough
VARIANTS = {"option": "没学过", "unrelated": "协方差是什么？"}
GOES_ON = "明白了（{a}）。那我们先从向量和方差讲起。"
OTHER = "协方差看两个量是不是一起变：一个变大时另一个也倾向变大，协方差为正。"
_inner = ab.assistant_turn


def assistant_turn(script, request: ChatRequest) -> ChatTurn:
    said, tools = ab._said(request)
    if said.rsplit("他问：\n", 1)[-1] != ASK:
        return _inner(script, request)
    ab.CHAT.append({"ask_turn": True, "tools": [t.name for t in request.tools], "results": tools})
    if not tools:
        return ChatTurn(text="好，先问你一句。", tool_calls=(ToolCall(
            id="ask1", name="ask_user", args={"question": QUESTION, "options": OPTIONS}),))
    if tools[-1] == REPLIED.format(q=QUESTION, a=VARIANTS["unrelated"]):
        return ChatTurn(text=OTHER)                  # not an answer: the new request, no re-ask
    reply = next((a for a in OPTIONS if tools[-1] == REPLIED.format(q=QUESTION, a=a)), "?")
    return ChatTurn(text=GOES_ON.format(a=reply))


ab.assistant_turn = assistant_turn       # assistant_gateway looks it up at call time
_mount = ab.mount_script_status


def mount(app, script) -> None:
    _mount(app, script)

    @app.get("/test/question-feedback")
    def _feedback(request: Request) -> dict[str, Any]:
        uid = request.headers["X-Evowiki-User"]
        store = PgSessionStore(app.state.services.pool)
        ratings = [{"session_id": i.session_id, "entry_id": e.entry_id, **e.payload}
                   for i in store.user_sessions(uid) for e in store.entries(i.session_id)
                   if e.type == "rating"]
        feedback = assistant_feedback(store, None, uid)
        cases, unusable = assistant_cases(feedback, uid, check=lambda c: None)
        return {"ratings": ratings,
                "signals": [{"run_id": run, **sig.model_dump(mode="json")} for sig, run in feedback],
                "rated_answers": [[at.isoformat(), down] for at, down in rated_answers(store, uid)],
                "cases": [{"case_id": c.case_id, "kind": c.kind, "run_id": c.run_id,
                           "signal_ids": list(c.signal_ids)} for c in cases],
                "unusable": [list(u) for u in unusable]}


ab.mount_script_status = mount           # main() looks it up at call time

if __name__ == "__main__":
    ab.main()
