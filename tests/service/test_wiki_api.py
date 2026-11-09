
import pytest

from evowiki.belief.aspects import Observe
from evowiki.goals.contracts import Observation
from evowiki.goals.diagnostic import IDK
from evowiki.llm.gateway import Gateway
from evowiki.wikiapp.llm import require
from evowiki.wikiapp.repo import Commit, PgRepo
from evowiki.wikiapp.views import goal_draft_out
from evowiki.wikiapp.wiki_schemas import (
    ORGANIZE_PAYLOADS,
    CheckAnswerOut,
    DeclareOut,
    DoneOut,
    ErrorEvent,
    FeedbackOut,
    GoalDraftOut,
    PageOut,
    SectionOut,
)
from tests.runtime.helpers import T0
from tests.runtime.llm_helpers import GW_CONFIG, MemoryCallLog
from tests.service.conftest import H, parse_sse, signup
from tests.wikiapp.fake_writer import PLAN_PAGE as ORGANIZE
from tests.wikiapp.fake_writer import WRITE
from tests.wikiapp.flat_goal import flat_goal, flat_page
from tests.wikiapp.goal_script import auto_probe, first_page
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import BACKGROUND, GOAL, INTAKE_1, script_diag
from tests.wikiapp.test_organize import BODY2, CHECK_KEY, OUTLINE, script_page
from tests.wikiapp.test_page import LINE


@pytest.fixture
def wiki(services, client):
    script = Script()
    services.gateway = script.gateway()
    return client, script


def start(wiki):
    """The z-score block's draft and its first round (tests.wikiapp.flat_goal), answered through
    the routes."""
    client, s = wiki
    signup(client, "u1")
    s.push("goals.intake", INTAKE_1)
    items = script_diag(s)
    svc = client.app.state.services
    res = flat_goal(PgRepo(svc.pool), require(svc.gateway, "u1"), "u1", GOAL, BACKGROUND,
                    now=svc.clock(), new_id=svc.new_id)
    return client, goal_draft_out(res, user_id="u1"), items


def test_a_draft_comes_without_keys(wiki):
    _, out, _items = start(wiki)
    # the open round is the KST pick only: its question is asked in the chat (ask_check)
    assert out.draft_id is not None and out.page_id is None and out.questions == []
    assert [d.node_id for d in out.decisions] == ["c1", "c2"]
    assert out.notes.from_background == 1 and wiki[1].calls() == 1      # one intake call (S2)


def test_a_draft_json_has_no_answer_key(wiki):
    _, out, _ = start(wiki)
    dumped = out.model_dump_json()
    assert out.model_dump(mode="json")["questions"] == [] and '"answer"' not in dumped


def test_unknown_user_is_404(wiki):
    client, _ = wiki
    r = client.post("/wiki/goals/dr_x/answers", headers=H("ghost"), json={"skip": True})
    assert r.status_code == 404


def test_answers_flow_and_errors(wiki):
    client, out, _items = start(wiki)
    url = f"/wiki/goals/{out.draft_id}/answers"
    assert client.post(url, json={"skip": True}).status_code == 422          # no user header
    assert client.post(url, headers=H("u1"), json={}).status_code == 422
    assert client.post(url, headers=H("u1"),
                       json={"answers": [0], "skip": True}).status_code == 422
    # the questions are asked and answered in the chat (stage 3 T11): answers here are refused
    assert client.post(url, headers=H("u1"), json={"answers": [0]}).status_code == 422
    assert client.post("/wiki/goals/dr_nope/answers", headers=H("u1"),
                       json={"skip": True}).status_code == 404
    r = client.post(url, headers=H("u1"), json={"skip": True})
    assert r.status_code == 201, r.text
    assert GoalDraftOut.model_validate(r.json()).path_id is not None
    again = client.post(url, headers=H("u1"), json={"skip": True})
    assert again.status_code == 409


def test_finishing_a_round_needs_the_llm_and_fails_closed_without_it(wiki, services):
    # the next round and the page plan are the model's call: no gateway -> 503, the draft stays
    client, out, _ = start(wiki)
    services.gateway = None
    r = client.post(f"/wiki/goals/{out.draft_id}/answers", headers=H("u1"), json={"skip": True})
    assert r.status_code == 503 and r.json() == {"detail": "请切换模型重试", "code": "LLMUnavailable", "params": {}}
    assert out.draft_id not in PgRepo(services.pool).load("u1").state.closed_drafts


def new_page(wiki) -> str:
    """The z-score goal's one page (tests.wikiapp.flat_goal.flat_page: its draft skipped)."""
    client, out, _ = start(wiki)
    svc = client.app.state.services
    return flat_page(PgRepo(svc.pool), require(svc.gateway, "u1"), "u1", out.draft_id,
                     now=svc.clock(), new_id=svc.new_id)


def organize(client, pid, uid="u1"):
    return client.post(f"/wiki/pages/{pid}/organize", headers=H(uid))


def test_organize_streams_outline_sections_done(wiki):
    client, s = wiki
    pid = new_page(wiki)
    script_page(s)
    r = organize(client, pid)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"          # FastAPI's SSE headers
    assert r.headers["x-accel-buffering"] == "no"
    events = parse_sse(r.text)
    assert [n for n, _ in events] == (
        ["planning", "outline", "writer"] + ["section_delta", "section_done"] * 3
        + ["done"])
    parsed = [ORGANIZE_PAYLOADS[n].model_validate(d) for n, d in events]
    assert parsed[1].page_id == pid and [x.id for x in parsed[1].sections] == [
        "s1", "s2", "s3", "s4"]
    assert events[1][1]["sections"][1]["cells"][0] == {
        "cell": "c1.procedure", "concept_id": "c1", "concept_name": "z 分数",
        "aspect": "procedure", "band": "shaky", "declared": None}
    assert events[5][1]["body"] == BODY2 and events[5][1]["heading"] == "z 分数怎么读"
    assert events[-1][1] == {"page_id": pid, "llm_calls": 3, "cost_usd": 0.0,
                             "model": "anthropic:claude-sonnet-5-5",
                             "fallback": False, "prefetched": False}

    script_page(s)   # reorganize: the writer outlines again, every section written again
    events = parse_sse(organize(client, pid).text)
    assert [d["cached"] for n, d in events if n == "section_done"] == [False, False, False]
    assert events[-1][1]["llm_calls"] == 3         # the writer's 3 sections


def test_organize_errors_before_stream(wiki, services):
    client, s = wiki
    pid = new_page(wiki)
    calls = s.calls()
    r = organize(client, "pg_nope")
    assert r.status_code == 404
    assert organize(client, pid, uid="ghost").status_code == 404
    services.gateway = None
    r = organize(client, pid)
    assert r.status_code == 503 and r.json() == {"detail": "请切换模型重试", "code": "LLMUnavailable", "params": {}}
    assert s.calls() == calls


def test_organize_llm_failure_is_error_event(wiki):
    client, s = wiki
    pid = new_page(wiki)
    s.push(ORGANIZE, OUTLINE)
    s.push(WRITE, "not json")
    r = organize(client, pid)
    assert r.status_code == 200
    events = parse_sse(r.text)
    assert [n for n, _ in events] == ["planning", "outline", "writer", "error"]
    err = ErrorEvent.model_validate(events[-1][1])
    assert (err.code, err.retryable) == ("llm_failed", True)


def organized_page(wiki, outline=OUTLINE) -> str:
    client, s = wiki
    pid = new_page(wiki)
    script_page(s, outline)
    assert parse_sse(organize(client, pid).text)[-1][0] == "done"
    return pid


def missed_c2(services) -> None:
    """A wrong answer on c2.term somewhere else: K falls, s3 conflicts (a 「不会」 is no conflict, it is
    the chat's)."""
    repo = PgRepo(services.pool)
    snap = repo.load("u1")
    repo.commit(snap, Commit(aspect=[Observe(event_id="miss_c2", ts=T0, observation=Observation(
        cell="c2.term", polarity="-", strength=1.0, kind="check", evidence_ref="check:x:y"))]))


def answer_the_check(client, pid):
    """The check of OUTLINE / LINE answered: the route's K-change pass sees K as it stands."""
    return client.post(f"/wiki/pages/{pid}/checks/s4", headers=H("u1"), json={"choice": CHECK_KEY})


def test_get_page_unorganized_then_organized(wiki):
    client, s = wiki
    pid = new_page(wiki)
    r = client.post(f"/wiki/pages/{pid}/open", headers=H("u1"))
    assert r.status_code == 200, r.text
    out = PageOut.model_validate(r.json())
    assert out.organized is False and out.sections == []
    script_page(s)
    organize(client, pid)
    calls = s.calls()
    r = client.post(f"/wiki/pages/{pid}/open", headers=H("u1"))
    out = PageOut.model_validate(r.json())
    assert out.organized and [x.id for x in out.sections] == ["s1", "s2", "s3", "s4"]
    assert out.diff is None and out.llm_calls == 0 and s.calls() == calls
    assert r.json()["sections"][1]["changed"] == "rewritten"


def test_get_page_works_without_gateway(wiki, services):
    client, _ = wiki
    pid = organized_page(wiki)
    services.gateway = None
    r = client.post(f"/wiki/pages/{pid}/open", headers=H("u1"))
    assert r.status_code == 200 and r.json()["organized"] is True


def test_get_page_errors(wiki):
    client, _ = wiki
    pid = organized_page(wiki)
    assert client.post("/wiki/pages/pg_nope/open", headers=H("u1")).status_code == 404
    assert client.post(f"/wiki/pages/{pid}/open", headers=H("ghost")).status_code == 404


def test_fold_pin_routes(wiki, services):
    client, _ = wiki
    pid = organized_page(wiki)
    services.gateway = None                       # 0-call endpoints
    url = f"/wiki/pages/{pid}/sections"
    r = client.post(f"{url}/s3/fold", headers=H("u1"), json={"folded": True})
    assert r.status_code == 200, r.text
    sec = SectionOut.model_validate(r.json())
    assert sec.id == "s3" and sec.folded and sec.fold_locked
    r = client.post(f"{url}/s3/pin", headers=H("u1"), json={"pinned": True})
    assert r.status_code == 200 and r.json()["pinned"] is True
    assert client.post(f"{url}/s1/fold", headers=H("u1"),
                       json={"folded": True}).status_code == 422          # organizer
    assert client.post(f"{url}/s9/fold", headers=H("u1"),
                       json={"folded": True}).status_code == 404
    assert client.post(f"{url}/s9/pin", headers=H("u1"),
                       json={"pinned": True}).status_code == 404
    assert client.post(f"{url}/s3/fold", headers=H("u1"), json={}).status_code == 422
    page = client.post(f"/wiki/pages/{pid}/open", headers=H("u1")).json()
    s3 = next(x for x in page["sections"] if x["id"] == "s3")
    assert s3["folded"] and s3["pinned"]


def test_done_route(wiki, services):
    client, _ = wiki
    pid = organized_page(wiki)
    services.gateway = None
    r = client.post(f"/wiki/pages/{pid}/done", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert DoneOut.model_validate(r.json()) == DoneOut()
    assert client.post(f"/wiki/pages/{pid}/open", headers=H("u1")).json()["done"] is True
    assert client.post("/wiki/pages/pg_nope/done", headers=H("u1")).status_code == 404


def test_get_page_with_unreachable_provider_is_200(wiki, services):
    client, _ = wiki
    pid = organized_page(wiki, LINE)
    missed_c2(services)                           # K moves into a conflict
    services.gateway = Gateway(GW_CONFIG, MemoryCallLog(), mode="record", providers={})
    r = client.post(f"/wiki/pages/{pid}/open", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert r.json()["organized"] is True and len(r.json()["unresolved_conflicts"]) == 1
    assert r.json()["budget_exceeded"] is False         # down is not over budget


# ---- Task 9: feedback, checks, ask, declarations ----

def fb(client, pid, sid, kind):
    return client.post(f"/wiki/pages/{pid}/sections/{sid}/feedback", headers=H("u1"),
                       json={"kind": kind})


def test_feedback_know_is_json_and_works_without_gateway(wiki, services):
    client, _ = wiki
    pid = organized_page(wiki)
    services.gateway = None
    r = fb(client, pid, "s2", "know")
    assert r.status_code == 200, r.text
    out = FeedbackOut.model_validate(r.json())
    assert out.section.id == "s2" and out.section.folded and out.section.fold_locked
    assert out.change.folded == ["s2"] and out.change.llm_calls == 0
    assert fb(client, pid, "s2", "nope").status_code == 422


def test_feedback_errors(wiki):
    client, _ = wiki
    pid = organized_page(wiki)
    assert fb(client, pid, "s4", "know").status_code == 422           # check section
    assert fb(client, pid, "s9", "know").status_code == 404
    assert fb(client, pid, "s3", "deeper").status_code == 422         # only 不会 stays a route


def test_check_route(wiki, services):
    client, _ = wiki
    pid = organized_page(wiki)
    services.gateway = None
    url = f"/wiki/pages/{pid}/checks/s4"
    assert client.post(url, headers=H("u1"), json={"choice": 7}).status_code == 422
    assert client.post(url, headers=H("u1"), json={"choice": -2}).status_code == 422
    r = client.post(url, headers=H("u1"), json={"choice": IDK})
    assert r.status_code == 200, r.text
    out = CheckAnswerOut.model_validate(r.json())
    assert out.correct is None and out.correct_index == CHECK_KEY and out.change.llm_calls == 0
    assert client.post(url, headers=H("u1"), json={"choice": 0}).status_code == 409
    assert client.post(f"/wiki/pages/{pid}/checks/s2", headers=H("u1"),
                       json={"choice": 0}).status_code == 422
    page = client.post(f"/wiki/pages/{pid}/open", headers=H("u1")).json()
    chk = next(x for x in page["sections"] if x["id"] == "s4")["check"]
    assert chk["answered"] == IDK and chk["correct_index"] == CHECK_KEY


def test_declare_route(wiki, services):
    client, _ = wiki
    pid = organized_page(wiki)
    client.post(f"/wiki/pages/{pid}/open", headers=H("u1"))                # current page
    services.gateway = None
    r = client.post("/wiki/k/cells/c2.term/declare", headers=H("u1"), json={"value": "know"})
    assert r.status_code == 200, r.text
    out = DeclareOut.model_validate(r.json())
    assert out.cell.band == "known" and out.cell.declared == "know"
    assert (out.cell.concept_id, out.cell.aspect) == ("c2", "term")
    assert out.change.folded == ["s3"] and out.change.llm_calls == 0
    r = client.post("/wiki/k/cells/c2.term/declare", headers=H("u1"), json={"value": None})
    assert r.status_code == 200 and r.json()["cell"]["declared"] is None
    assert client.post("/wiki/k/cells/c2.procedure/declare", headers=H("u1"),
                       json={"value": "know"}).status_code == 404
    assert client.post("/wiki/k/cells/c2.term/declare", headers=H("u1"),
                       json={"value": "maybe"}).status_code == 422


class _AccountSpy:
    def __init__(self):
        self.seen: list[tuple[str, str, str]] = []

    def check(self, account):
        return None

    def record(self, account, purpose, usage, *, replayed):
        self.seen.append((account.user_id, account.group, purpose))


def test_wiki_routes_pass_the_users_account(wiki, services):
    from evowiki.llm.prices import PriceTable
    from evowiki.llm.scripted import ScriptedProvider
    from evowiki.llm.usage_control import UsageControl
    from tests.wikiapp.script import PROVIDERS

    client, s = wiki
    spy = _AccountSpy()
    services.gateway = Gateway(GW_CONFIG, s.log, mode="record", clock=lambda: T0,
                               providers={p: ScriptedProvider(p, s._responder(p))
                                          for p in PROVIDERS},
                               record_max_attempts=1,
                               usage=UsageControl(spy, PriceTable({})))
    pid = new_page(wiki)
    script_page(s)
    assert organize(client, pid).status_code == 200
    assert {u for u, _, _ in spy.seen} == {"u1"}
    groups = {p: g for _, g, p in spy.seen}
    assert groups["goals.enter"] == "diagnostic" and "goals.probe" not in groups
    assert ORGANIZE not in groups             # the page writer (deep tier) outlines in its loop


class _OverBudget(_AccountSpy):
    def check(self, account):
        from evowiki.llm.usage import BudgetExceededError
        raise BudgetExceededError(account, "day", 1.0)


def _budget_gateway(s):
    from evowiki.llm.prices import PriceTable
    from evowiki.llm.scripted import ScriptedProvider
    from evowiki.llm.usage_control import UsageControl
    from tests.wikiapp.script import PROVIDERS

    return Gateway(GW_CONFIG, s.log, mode="record", clock=lambda: T0,
                   providers={p: ScriptedProvider(p, s._responder(p)) for p in PROVIDERS},
                   record_max_attempts=1, usage=UsageControl(_OverBudget(), PriceTable({})))


def test_budget_exhausted_json_route_is_429(wiki, services):
    client, out, _ = start(wiki)
    s = wiki[1]
    calls = s.calls()
    services.gateway = _budget_gateway(s)        # finishing the draft plans the path: a call
    r = client.post(f"/wiki/goals/{out.draft_id}/answers", headers=H("u1"), json={"skip": True})
    assert r.status_code == 429 and r.json() == {
        "detail": "今天的花费到了上限 $1.00，这次没有完成。可以在设置里调高。",
        "code": "budget_exceeded", "params": {"period": "day", "limit": "1.00"}}
    assert s.calls() == calls


class _OverBudgetOnCall(_AccountSpy):
    """Lets the first n-1 metered calls through; the n-th check raises BudgetExceededError."""

    def __init__(self, n: int):
        super().__init__()
        self.n = n
        self.checks = 0

    def check(self, account):
        from evowiki.llm.usage import BudgetExceededError
        self.checks += 1
        if self.checks >= self.n:
            raise BudgetExceededError(account, "day", 1.0)


def test_budget_exhausted_organize_stream_is_budget_error(wiki, services):
    """The page's spend is the writer's (no runtime call before it): its budget error ends the
    stream; the outline it planned stays (commit as you go, T3), unfinished."""
    client, s = wiki
    pid = new_page(wiki)
    s.push(ORGANIZE, OUTLINE)
    s.push(WRITE, "BUDGET")
    r = organize(client, pid)
    assert r.status_code == 200
    events = parse_sse(r.text)
    assert [n for n, _ in events] == ["planning", "outline", "writer", "error"]
    err = ErrorEvent.model_validate(events[-1][1])
    assert (err.code, err.retryable) == ("budget_exceeded", False)
    assert PageOut.model_validate(client.post(f"/wiki/pages/{pid}/open", headers=H("u1")).json())
    rec = PgRepo(services.pool).load("u1").state.pages[pid]
    assert rec.page.texts == {} and not rec.has_body


def test_budget_exhausted_get_page_still_updates_it(wiki, services):
    client, s = wiki
    pid = organized_page(wiki, LINE)
    missed_c2(services)                           # K moves into a conflict
    services.gateway = _budget_gateway(s)
    r = client.post(f"/wiki/pages/{pid}/open", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert r.json()["organized"] is True and len(r.json()["unresolved_conflicts"]) == 1
    assert r.json()["budget_exceeded"] is False         # the update makes no model call
    assert r.json()["conflicts"] == [{"kind": "line_weak", "section_id": "s3", "cell": "c2.term"}]


def test_open_within_budget_is_not_budget_exceeded(wiki, services):
    client, _s = wiki
    pid = organized_page(wiki, LINE)
    r = client.post(f"/wiki/pages/{pid}/open", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert r.json()["budget_exceeded"] is False


def test_budget_exhausted_k_change_route_still_updates_the_page(wiki, services):
    client, s = wiki
    pid = organized_page(wiki, LINE)
    client.post(f"/wiki/pages/{pid}/open", headers=H("u1"))                # current page
    missed_c2(services)
    services.gateway = _budget_gateway(s)
    r = answer_the_check(client, pid)
    assert r.status_code == 200, r.text
    out = CheckAnswerOut.model_validate(r.json())
    # no model call and no write: the conflict is only recorded (an answer never rewrites a page)
    assert out.change.rewritten == [] and len(out.change.unresolved_conflicts) == 1
    assert out.change.budget_exceeded is False
    assert [c.model_dump() for c in out.change.conflicts] == [
        {"kind": "line_weak", "section_id": "s3", "cell": "c2.term"}]


# ---- every SSE stream ends with `done` or `error` ----

class ErrScript(Script):
    """Script where a purpose may raise instead of answering: `fail[purpose]` is the exception
    (raised on every call), or a list of exceptions consumed one per call. A purpose the scripted
    page writer takes directly (its plan_page outline) fails the same way."""

    def __init__(self):
        super().__init__()
        self.fail: dict = {}

    def take(self, purpose, prompt="take"):
        f = self.fail.get(purpose)
        if isinstance(f, list):
            f = f.pop(0) if f else None
        if f is not None:
            raise f
        return super().take(purpose, prompt)

    def _responder(self, provider):
        inner = super()._responder(provider)

        def answer(model, request):
            f = self.fail.get(request.purpose)
            if isinstance(f, list):
                f = f.pop(0) if f else None
            if f is not None:
                raise f
            return inner(model, request)
        return answer


@pytest.fixture
def ewiki(services, client):
    script = ErrScript()
    services.gateway = script.gateway()
    return client, script


def test_organize_unexpected_exception_ends_with_internal_error(ewiki):
    client, s = ewiki
    pid = new_page(ewiki)
    s.fail[ORGANIZE] = RuntimeError("boom " + "x" * 600)
    events = parse_sse(organize(client, pid).text)
    assert events[-1][0] == "error"
    err = ErrorEvent.model_validate(events[-1][1])
    assert (err.code, err.retryable) == ("internal", True)
    assert err.detail.startswith("RuntimeError: boom") and len(err.detail) <= 500


def test_organize_writer_transient_errors_end_with_llm_unavailable(ewiki):
    client, s = ewiki
    pid = new_page(ewiki)
    s.push(ORGANIZE, OUTLINE)
    s.push(WRITE, "UNAVAILABLE")            # the writer (and its fallback) unreachable
    events = parse_sse(organize(client, pid).text)
    assert [n for n, _ in events] == ["planning", "outline", "writer", "error"]
    err = ErrorEvent.model_validate(events[-1][1])
    assert (err.code, err.retryable) == ("llm_unavailable", True)
    page = PageOut.model_validate(client.post(f"/wiki/pages/{pid}/open", headers=H("u1")).json())
    # the planned outline is committed (T3), no section text yet; the page is not done
    assert page.organized and all(x.body == "" for x in page.sections) and not page.done
    assert client.post(f"/wiki/pages/{pid}/done", headers=H("u1")).status_code == 409


def test_sse_guard_adds_error_when_stream_stops_without_done():
    from evowiki.service.routes_wiki import _events
    from evowiki.wikiapp.wiki_schemas import OrganizeDoneEvent
    out = list(_events(iter([])))
    assert len(out) == 1 and out[0].event == "error" and "without done" in out[0].raw_data
    done = OrganizeDoneEvent(page_id="p", llm_calls=0)
    ok = list(_events(iter([("done", done)])))
    assert len(ok) == 1 and ok[0].event == "done"


def test_error_mapping_transient_is_llm_unavailable_retryable():
    from evowiki.llm.providers import ProviderTransientError
    from evowiki.wikiapp.organize import _error
    e = _error(ProviderTransientError("APITimeoutError"))
    assert (e.code, e.retryable) == ("llm_unavailable", True)


def test_organize_rejected_provider_does_not_retry_and_ends_llm_unavailable(ewiki):
    from evowiki.llm.providers import ProviderRejectedError
    client, s = ewiki
    pid = new_page(ewiki)
    s.fail[ORGANIZE] = ProviderRejectedError(
        "deepseek rejected the request for deep.write/p: HTTP 402 Insufficient Balance",
        status_code=402)
    events = parse_sse(organize(client, pid).text)
    assert [n for n, _ in events] == ["planning", "error"]            # no outline, nothing written
    err = ErrorEvent.model_validate(events[-1][1])
    assert (err.code, err.retryable) == ("llm_unavailable", False)
    assert "HTTP 402" in err.detail and "Insufficient Balance" in err.detail
    page = PageOut.model_validate(client.post(f"/wiki/pages/{pid}/open", headers=H("u1")).json())
    assert page.organized is False


def test_error_mapping_rejected_is_llm_unavailable_not_retryable():
    from evowiki.llm.providers import ProviderRejectedError
    from evowiki.service.sse import error_payload
    from evowiki.wikiapp.organize import _error
    exc = ProviderRejectedError("HTTP 402", status_code=402)
    e = _error(exc)
    assert (e.code, e.retryable) == ("llm_unavailable", False)
    p = error_payload(exc)
    assert (p["code"], p["retryable"]) == ("llm_unavailable", False)
    assert error_payload(RuntimeError("x"))["code"] == "internal"


def test_a_provider_rejected_402_on_a_json_route_is_503(ewiki):
    from evowiki.llm.providers import ProviderRejectedError
    client, out, _ = start(ewiki)
    s = ewiki[1]
    s.fail["goals.path"] = ProviderRejectedError(
        "deepseek rejected the request: HTTP 402 Insufficient Balance", status_code=402)
    r = client.post(f"/wiki/goals/{out.draft_id}/answers", headers=H("u1"), json={"skip": True})
    assert r.status_code == 503 and r.json() == {"detail": "请切换模型重试", "code": "LLMUnavailable", "params": {}}


def test_a_goals_first_page_through_the_routes(wiki):
    """tests.wikiapp.goal_script.first_page (the live scripts' goal step): S1's blocks confirmed,
    the opening round skipped, the first block split one level, its first point opened (its round
    skipped) and its path's first page started."""
    client, s = wiki
    signup(client, "u1")
    auto_probe(s)
    s.push("goals.intake", {"title": "z 分数", "blocks": [
        {"name": "z 分数", "summary": "离均值几个标准差", "covers": ["计算", "解读"], "target": True},
        {"name": "标准差", "covers": ["离散程度"]}]})
    s.push("goals.enter", {"parts": [{"name": "z 的计算", "atomic": True},
                                     {"name": "z 的解读", "atomic": True}]})
    pid, rounds = first_page(client, "u1", GOAL)
    assert pid.startswith("pg_") and rounds == []
    assert s.calls("goals.intake") == 1 and s.calls("goals.enter") == 1
    page = PgRepo(client.app.state.services.pool).load("u1").state.pages[pid]
    assert page.title == "z 的计算"                     # the first point of the first block
