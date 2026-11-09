"""「每页最多花」 covers the whole page job (D26): one budget made by `write_page`, handed to the
writer, whose plan_page and write turns all count against it; no limit = unchanged."""
import pytest

from evowiki.deep.contracts import RefineSettings
from evowiki.llm.gateway import Gateway
from evowiki.llm.prices import Price, PriceTable
from evowiki.llm.scripted import ScriptedProvider
from evowiki.llm.usage import NullMeter
from evowiki.llm.usage_control import UsageControl
from evowiki.wikiapp.events import RefineSettingsSet, WikiEvent
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import Commit
from tests.runtime.llm_helpers import GW_CONFIG
from tests.wikiapp.fake_writer import ScriptWriter, writing
from tests.wikiapp.script import PROVIDERS, T0
from tests.wikiapp.test_goal import NOW
from tests.wikiapp.test_organize import env, page1, run, script_page  # noqa: F401

REFS = (GW_CONFIG.generator, GW_CONFIG.verifier)


def _prices(input_per_mtok: float) -> PriceTable:
    return PriceTable({f"{r.provider}:{r.model}": Price(input_per_mtok, 1.0) for r in REFS})


class _Tokens:
    """A scripted provider whose replies report 100 / 100 tokens, so they are priced."""

    def __init__(self, inner):
        self.inner = inner

    def complete(self, model, request, json_schema):
        resp = self.inner.complete(model, request, json_schema)
        return resp.model_copy(update={"input_tokens": 100, "output_tokens": 100, "cost": None})


def metered(s, prices: PriceTable) -> CountingGateway:
    providers = {p: _Tokens(ScriptedProvider(p, s._responder(p))) for p in PROVIDERS}
    gw = Gateway(GW_CONFIG, s.log, mode="record", clock=lambda: T0, providers=providers,
                 record_max_attempts=1, usage=UsageControl(NullMeter(), prices))
    return CountingGateway(gw, "u1")


class BudgetWriter(ScriptWriter):
    """Notes what the page budget had spent when the writer started, and charges its own write
    to that budget (what the real writer's JobMeter does)."""

    def __init__(self, script, cost: float):
        super().__init__(script, cost_usd=cost)
        self.budget = None
        self.at_start = None

    def write(self, inp, *, budget=None, **kw):
        self.budget, self.at_start = budget, budget.spent
        budget.add(self.cost_usd)
        return super().write(inp, budget=budget, **kw)


def set_limit(repo, limit):
    snap = repo.load("u1")
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id="wev_limit", ts=NOW, body=RefineSettingsSet(
        settings=RefineSettings(per_page_limit_usd=limit)))]))


@pytest.mark.parametrize("limit", [None, 1.0])
def test_the_page_budget_is_the_writers_and_covers_the_whole_job(env, limit):  # noqa: F811
    """Stage 4: no runtime call before the writer (it plans in its own loop), so the writer starts
    on an untouched budget and the page's cost is what it spent."""
    repo, s, _ = env
    pid = page1(env)
    set_limit(repo, limit)
    script_page(s)
    w = BudgetWriter(s, cost=0.01)
    kit = writing(env)
    kit = type(kit)(writer=w, specs=kit.specs, assets=kit.assets)
    items, gw = run(env, pid, gw=metered(s, _prices(1.0)), writing=kit)
    assert items[-1][0] == "done" and gw.calls == 0
    done = items[-1][1]
    assert w.budget.limit_usd == limit and w.at_start == 0.0
    assert w.budget.spent == pytest.approx(done.cost_usd) == pytest.approx(0.01)
