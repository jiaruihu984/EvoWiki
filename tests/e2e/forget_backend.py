"""Disposable backend for the 删除对话 (delete a conversation) walkthrough in REAL Obsidian.
Test-only, no paid call.

The audit backend (tests/e2e/audit_backend.py: build_services on a fresh Postgres, DBOS launched
by the app lifespan, every model a scripted fake) with the production delete flow as
`build_services` wires it (`svc.forget` = deps.build_forget: RSI's forget_session and S3's
preference steps; /assistant/* opens once the lifespan's ledger reconcile passed). The forget
ledger is a temp file (EVOWIKI_FORGET_LEDGER), never under the repo.

POST /test/seed (header X-Evowiki-User) seeds, through the real stores (PgSessionStore,
wikiapp.preferences.land_preference, sources.store), for that user:
  A 「为什么方差要平方而不是取绝对值」 (today, latest): his question + an answer, then a second
    turn whose words land one teaching rule 「讲概念时先给一个生活里的例子」 and one profile line
    (想学什么) 「在学线性代数，目标是看懂 PCA」, both active, origin_ref A/<that message>;
  B 「特征值为什么叫「特征」」 (today, earlier) and C 「协方差为负是什么意思」 (yesterday): plain Q/A;
    C's answer recalled A's first turn (`recalled` ep:A/<m>), so C is in A's taint closure;
  materials used in A: 「线性代数讲义.pdf」 (also used in B) and 「截图 2026-10-21.png」 (only A).
  -> {"user", "sessions": {"A", "B", "C"}, "rule_id", "fact_key", "sources": {"handout",
      "screenshot"}}
POST /test/fail-next-forget: the next session delete fails once inside its transaction (the RSI
  step raises; nothing deleted) -> DELETE answers 202, the request stays `failed`; any later run
  succeeds (the lifespan's background retry, every 60 s, or POST /test/retry-forget).
POST /test/retry-forget: ForgetService.retry_open() once -> [{"request_id", "state", "error"}].
GET /test/forget-state (header): {"sessions": [{"session_id", "title"}] still listed,
  "pending": [{"session_id", "state"}], "rule_in_force", "fact_in_force",
  "sources": [{"source_id", "title", "deleted"}], "fail_armed"}.

  uv run python -m tests.e2e.forget_backend --port 8861 --user forget-user
"""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Request

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import SessionInfo
from evowiki.memory.contracts import EvidenceRef, MemoryScope
from evowiki.memory.tombstone import FORGET_LEDGER_ENV
from evowiki.profile.note import WANT
from evowiki.profile.store import PgProfileStore
from evowiki.sources.models import SourceRow
from evowiki.sources.store import add_use, insert_source
from evowiki.store.db import fetch_all, fetch_one
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.preferences import CurrentMessage, land_preference
from evowiki.wikiapp.repo import PgRepo
from tests.e2e import audit_backend as ab
from tests.runtime.helpers import make_user

LEDGER = Path(tempfile.mkdtemp(prefix="evowiki-forget-")) / "forget-ledger.jsonl"
_build = ab.build_services


def build_services(env, **kw):
    return _build({**env, FORGET_LEDGER_ENV: str(LEDGER)}, **kw)


ab.build_services = build_services      # main() looks it up at call time

A_Q = "为什么方差要平方而不是取绝对值？"
A_ANSWER = ("平方让偏差都变成正数，而且处处可导、方便推公式；绝对值也能衡量离散程度（平均绝对偏差），"
            "但在零点不可导，代数上也没有平方好用。")
A_PREF = "以后讲概念时先给一个生活里的例子吧。我在学线性代数，目标是看懂 PCA。"
A_PREF_ANSWER = "好的，记下了：讲概念时先举一个生活里的例子；也记下了你在学线性代数、想看懂 PCA。"
RULE = "讲概念时先给一个生活里的例子"
FACT = "在学线性代数，目标是看懂 PCA"
B_Q = "特征值为什么叫「特征」？"
B_ANSWER = "因为它刻画了矩阵本身的性质：沿特征向量方向，矩阵只做伸缩，伸缩倍数就是特征值。"
C_Q = "协方差为负是什么意思？"
C_ANSWER = ("协方差为负说明两个量倾向反向变化：一个偏大时另一个往往偏小。"
            "和方差一样，它也是用偏差相乘再取平均。")
HANDOUT = "线性代数讲义.pdf"
SCREENSHOT = "截图 2026-10-21.png"

SEEDS: dict[str, dict[str, Any]] = {}


class OnceFailing:
    """The RSI step of the delete, failing once when armed (the delete's transaction rolls back:
    nothing deleted, the request recorded `failed` and retried later)."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.armed = inner, False

    def forget(self, conn, user_id, session_id, *, quotes):
        if self.armed:
            self.armed = False
            raise RuntimeError("test: this delete fails once (POST /test/fail-next-forget)")
        return self.inner.forget(conn, user_id, session_id, quotes=quotes)


def _turn(store: PgSessionStore, new_id, sid: str, q: str, a: str, at: datetime,
          recalled: list[str] | None = None) -> str:
    mid = new_id("msg")
    store.append(sid, new_id("ent"), "user", {"message_id": mid, "text": q}, at)
    payload: dict[str, Any] = {"message_id": new_id("msg"), "text": a}
    if recalled:
        payload["recalled"] = [{"id": r, "kind": "episode", "content_hash": "seed"}
                               for r in recalled]
    store.append(sid, new_id("ent"), "assistant", payload, at + timedelta(seconds=20))
    return mid


def seed(svc: Any, uid: str) -> dict[str, Any]:
    pool, new_id = svc.pool, svc.new_id
    with pool.connection() as conn:
        if fetch_one(conn, "select 1 from app_user where user_id = %s", (uid,)) is None:
            make_user(pool, uid)
    now = datetime.now(UTC)
    store = PgSessionStore(pool)
    sids = {k: new_id("ses") for k in "ABC"}
    starts = {"C": now - timedelta(days=1), "B": now - timedelta(hours=1),
              "A": now - timedelta(minutes=10)}
    for k in "CBA":
        store.create(SessionInfo(session_id=sids[k], user_id=uid, page_id=None,
                                 note_concept_id=None, created_at=starts[k]))
    a1 = _turn(store, new_id, sids["A"], A_Q, A_ANSWER, starts["A"])
    a2 = _turn(store, new_id, sids["A"], A_PREF, A_PREF_ANSWER, starts["A"] + timedelta(minutes=2))
    _turn(store, new_id, sids["B"], B_Q, B_ANSWER, starts["B"])
    # C's answer recalled A's first turn: C is in A's taint closure
    _turn(store, new_id, sids["C"], C_Q, C_ANSWER, starts["C"],
          recalled=[f"ep:{sids['A']}/{a1}"])

    # his words in A's second message land a rule and a profile line (the remember_* tools' path)
    repo, profile = PgRepo(pool), PgProfileStore(pool, svc.clock)
    current = CurrentMessage(message_id=a2, words=(A_PREF,))
    landed = {}
    for kind, quote, text, section in (("teaching", RULE, RULE, None),
                                       ("about_me", "我在学线性代数，目标是看懂 PCA", FACT, WANT)):
        got = land_preference(
            repo, repo.load(uid), kind=kind, text=text, provenance="explicit_current",
            evidence=[EvidenceRef(session_id=sids["A"], message_id=a2, role="user", quote=quote)],
            scope=MemoryScope(scope_type="global"), section=section, session_id=sids["A"],
            now=now, new_id=new_id, current=current, profile=profile)
        if got.status != "active":
            raise RuntimeError(f"seed: {kind} did not land: {got}")
        landed[kind] = got.item_id

    # materials used in A: the handout also in B, the screenshot only in A
    src = {"handout": new_id("src"), "screenshot": new_id("src")}
    with pool.connection() as conn, conn.transaction():
        for key, title, kind, mime, data in (
                ("handout", HANDOUT, "pdf", "application/pdf", b"%PDF-1.4 seed"),
                ("screenshot", SCREENSHOT, "image", "image/png", b"\x89PNG seed")):
            insert_source(conn, SourceRow(
                source_id=src[key], user_id=uid, kind=kind, title=title,
                sha256=f"seed-{src[key]}", bytes=len(data), pages=1, status="ready", error=None,
                unreadable_pages=(), toc_mode=None, progress={}, origin=None,
                created_at=starts["C"], updated_at=starts["C"], deleted_at=None), data, mime)
            add_use(conn, uid, src[key], "ask", session_id=sids["A"], now=starts["A"])
        add_use(conn, uid, src["handout"], "ask", session_id=sids["B"], now=starts["B"])
    out = {"user": uid, "sessions": sids, "rule_id": landed["teaching"],
           "fact_key": landed["about_me"], "sources": src}
    SEEDS[uid] = out
    return out


def forget_state(svc: Any, uid: str, failing: OnceFailing) -> dict[str, Any]:
    pool = svc.pool
    listed = svc.assistant.list_sessions(uid)
    s = SEEDS.get(uid, {})
    rules = {i.item_id for i in tspec.effective_spec(PgRepo(pool).load(uid).state).items}
    prof = PgProfileStore(pool, svc.clock).get(uid)
    with pool.connection() as conn:
        sources = fetch_all(conn, "select source_id, title, deleted_at is not null as deleted "
                                  "from source where user_id = %s order by created_at, title",
                            (uid,))
    return {"sessions": [{"session_id": r.session_id, "title": r.title} for r in listed.sessions],
            "pending": [{"session_id": r["session_id"], "state": r["state"]}
                        for r in svc.forget.pending(uid)],
            "rule_in_force": s.get("rule_id") in rules,
            "fact_in_force": FACT in prof.owned.get(WANT, ()),
            "sources": sources, "fail_armed": failing.armed}


_mount = ab.mount_script_status


def mount(app, script) -> None:
    _mount(app, script)
    svc = app.state.services
    assert svc.forget is not None and svc.forget.available, "build_services did not wire forget"
    failing = OnceFailing(svc.forget._harness)
    svc.forget._harness = failing

    @app.post("/test/seed")
    def _seed(request: Request) -> dict[str, Any]:
        return seed(svc, request.headers["X-Evowiki-User"])

    @app.post("/test/fail-next-forget")
    def _fail() -> dict[str, bool]:
        failing.armed = True
        return {"armed": True}

    @app.post("/test/retry-forget")
    def _retry() -> list[dict[str, Any]]:
        return [{"request_id": o.request_id, "state": o.state, "error": o.error}
                for o in svc.forget.retry_open()]

    @app.get("/test/forget-state")
    def _state(request: Request) -> dict[str, Any]:
        return forget_state(svc, request.headers["X-Evowiki-User"], failing)


ab.mount_script_status = mount           # main() looks it up at call time

if __name__ == "__main__":
    ab.main()
