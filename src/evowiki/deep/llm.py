"""The page writer's model tier (single writer): the deep tier's gateways (primary + fallback, the
pack's step tiers), the per-page job meter and the forged-tool opener. Shared by the writer
(`deep.writer`) and the refine service (`wikiapp.refine_service`); it touches no wiki state."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from evowiki.agent.trace import NO_TRACE, TraceSink
from evowiki.deep.agent import VISUAL_TIER, ForgeHook, ForgeOpener, RefineInput
from evowiki.harness.pack import HarnessPack
from evowiki.harness.source import HarnessSource
from evowiki.llm.budget import JobBudget, JobMeter, PageBudgetExceeded
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import AttemptStore, CallStore, RequestLog
from evowiki.llm.models import GatewayConfig
from evowiki.llm.prices import PriceTable
from evowiki.llm.usage import UsageMeter
from evowiki.llm.usage_control import UsageControl, with_reasoning

if TYPE_CHECKING:
    from evowiki.forge.run import ForgeRuntime

GROUP = "page"
__all__ = ["GROUP", "DeepLLM", "JobBudget", "JobMeter", "PageBudgetExceeded", "forge_opener"]


@dataclass(frozen=True)
class DeepLLM:
    """What a job's gateway is built from: the deep tier's config (generator writes, verifier
    reviews — another provider when one is configured), its providers, the shared call / attempt
    logs, the price table and the user meter; where the writer's traces and the full inputs of its
    JSON calls go."""

    config: GatewayConfig
    providers: Mapping[str, Any]
    log: CallStore
    prices: PriceTable
    meter: UsageMeter
    attempts: AttemptStore | None = None
    clock: Callable[[], datetime] | None = None
    # the writer after `config` when its provider is unavailable (single writer: DeepSeek's good
    # tier behind Claude); None = no fallback
    fallback: GatewayConfig | None = None
    # the user's prompt guidance (Prompt 设置) and harness values, read through the pack source; None = the
    # default wording
    harness: HarnessSource | None = None
    trace: TraceSink = NO_TRACE
    requests: RequestLog | None = None
    # a harness replay's own salt (`Gateway.salted`): its calls never reuse recorded ones; None = a
    # normal run
    salt: str | None = None
    # the "fast" / "good" tier configs the pack's H10 step tiers and H9 / H11 hook steps may pick
    # (phase 3a); None = only the writer chain ("deep"), and a pack picking another tier fails
    # closed
    tiers: Mapping[str, GatewayConfig] | None = None
    # the user's model (the composer picker, D27): user id -> the model their calls use, and each
    # model's writer config and fast / good configs. With them a user's writer is their model's
    # (`config` otherwise), the fallback only when it is another provider, and the pack's step
    # tiers that model's levels. None = `config` / `tiers` for everyone.
    pick: Callable[[str], str | None] | None = None
    writers: Mapping[str, GatewayConfig] | None = None
    model_tiers: Mapping[str, Mapping[str, GatewayConfig]] | None = None
    # the learner profile per (user id, purpose) (`profile.service.ProfileService.context`), appended
    # by every gateway this builds; None = no profile
    profile: Callable[[str, str], str] | None = None
    # the visualization step's own model (`deep.visual`, EVOWIKI_VISUAL_MODEL); None = the writer's
    visual: GatewayConfig | None = None
    reasoning: Callable[[str], str | None] | None = None

    def _picked(self, user_id: str | None) -> str | None:
        if user_id is None or self.pick is None:
            return None
        return self.pick(user_id)

    def writer_config(self, user_id: str | None = None) -> GatewayConfig:
        """The writer for `user_id`: their model's writer, else `config`."""
        m = self._picked(user_id)
        if m is None:
            return self.config
        if m not in (self.writers or {}):
            raise ValueError("所选模型已不可用，请重新选择模型")
        config = self.writers[m]
        effort = self.reasoning(user_id) if self.reasoning is not None and user_id is not None else None
        return with_reasoning(config, effort) if effort is not None else config

    def _gateway(self, config: GatewayConfig, meter: JobMeter) -> Gateway:
        kw: dict[str, Any] = {"clock": self.clock} if self.clock is not None else {}
        gw = Gateway(config, self.log, mode="record", providers=self.providers,
                     attempts=self.attempts, record_max_attempts=1,
                     usage=UsageControl(meter, self.prices, clock=self.clock), harness=self.harness,
                     requests=self.requests, profile=self.profile, **kw)
        return gw if self.salt is None else gw.salted(self.salt)

    def gateway(self, limit_usd: float | None) -> tuple[Gateway, JobMeter]:
        meter = JobMeter(self.meter, limit_usd)
        return self._gateway(self.config, meter), meter

    def with_harness(self, source: HarnessSource) -> "DeepLLM":
        """The same writers reading their packs from `source`: every gateway this builds (primary
        and fallback) gets it, as `Gateway.with_harness` would (shared logs, usage and providers)."""
        return replace(self, harness=source)

    def gateways(self, limit_usd: float | None, user_id: str | None = None, *,
                 budget: JobBudget | None = None) -> tuple[list[Gateway], JobMeter]:
        """The writers in order (primary = the user's model's writer, then the fallback when there
        is one on another provider), metered by one JobMeter (the page's spend and its own limit
        cover both). `budget`: the job's own (the page job's, shared with its planning calls);
        None = a new one with `limit_usd`."""
        meter = JobMeter(self.meter, limit_usd, budget=budget)
        primary = self.writer_config(user_id)
        out = [self._gateway(primary, meter)]
        if (self._picked(user_id) is None and self.fallback is not None
                and self.fallback.generator.provider != primary.generator.provider):
            out.append(self._gateway(self.fallback, meter))
        return out, meter

    def tier_gateway(self, tier: str, meter: JobMeter, user_id: str | None = None) -> Gateway:
        if tier == VISUAL_TIER and self.visual is not None:
            return self._gateway(self.visual, meter)
        """A gateway on the tier's config (the user's model's level when it has one), metered by
        `meter` (the writers' own JobMeter). KeyError for a tier this deep tier has no config for."""
        if self.tiers is None or tier not in self.tiers:
            raise KeyError(tier)
        m = self._picked(user_id)
        own = (self.model_tiers or {}).get(m, {}) if m is not None else {}
        if m is not None and not own:
            raise ValueError("所选模型已不可用，请重新选择模型")
        config = own.get(tier, self.tiers[tier])
        effort = self.reasoning(user_id) if self.reasoning is not None and user_id is not None else None
        return self._gateway(with_reasoning(config, effort) if effort is not None else config, meter)

    def roster(self, limit_usd: float | None, user_id: str | None = None, *,
               budget: JobBudget | None = None) -> tuple[dict[str, list[Gateway]], JobMeter]:
        """{"deep": [primary, fallback?], "fast": [..], "good": [..]} sharing one JobMeter;
        tiers None -> only "deep". `user_id`: whose model they are; `budget` as `gateways`."""
        gws, meter = self.gateways(limit_usd, user_id, budget=budget)
        out: dict[str, list[Gateway]] = {"deep": gws}
        for tier in self.tiers or {}:
            if tier != "deep":
                out[tier] = [self.tier_gateway(tier, meter, user_id)]
        if self.visual is not None:
            out[VISUAL_TIER] = [self.tier_gateway(VISUAL_TIER, meter, user_id)]
        return out, meter

    @property
    def model(self) -> str:
        g = self.config.generator
        return f"{g.provider}:{g.model}"

    def model_for(self, user_id: str | None) -> str:
        """`model` for the user's own writer (their pick)."""
        g = self.writer_config(user_id).generator
        return f"{g.provider}:{g.model}"


class ForgeCandidateUnavailable(RuntimeError):
    """A harness replay's forged-tool candidate is not in the run the forge opened (its tool
    switch is off, the pin failed, or the candidate did not load): the replay must not run
    without it (`DeepService.replay_section` turns this into ReplayFailed("forge_unavailable"))."""


FORGE_UNAVAILABLE = "这次重放用不上这个新工具（工具开关关着，或者没能准备好），没有重放"


def forge_opener(runtime: "ForgeRuntime | None", inp: RefineInput,
                 on_cost: Callable[[float | None], None] | None = None, *,
                 candidate: str | None = None,
                 on_missing: Callable[[], None] | None = None) -> ForgeOpener | None:
    """The page writer's forged tools (coding-agent RSI phase 2, plan T11): `refine` calls the
    opener once with its run id and pack; the forge pins the user's opted-in tools for the page
    (ranked by the page's title and goal). None without a runtime: no pin step, no forged tools.
    `on_cost`: what each request_tool build cost (the job meter's `charge`, so the page's
    `cost_usd` includes it). `candidate`: a harness replay's forged-tool candidate (run-scoped
    overlay, never the registry); None passes nothing. With a candidate the opened run must reach
    it, else `on_missing()` and ForgeCandidateUnavailable (before the writer's first call: the
    refine fails)."""
    if runtime is None:
        return None
    extra = {} if candidate is None else {"candidate": candidate}

    def open_(run_id: str, pack: HarnessPack) -> ForgeHook | None:
        run = runtime.for_run(user_id=inp.user_id, agent="deep", run_id=run_id, group=GROUP,
                              pack=pack, query=f"{inp.title} {inp.goal}",
                              on_cost=on_cost, **extra)
        if candidate is not None and (run is None or not any(
                t.candidate_id == candidate for t in run.reachable.values())):
            if on_missing is not None:
                on_missing()
            raise ForgeCandidateUnavailable(f"forged-tool candidate {candidate} not in this run")
        return run.tools if run is not None else None
    return open_
