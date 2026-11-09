"""What Gateway needs to meter a call (plan rev 3.2 §9.4): the meter, the price table and the
fast/good tier routing. Pure: no database, no provider SDK."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Literal, Protocol, get_args

from evowiki.llm.models import GatewayConfig
from evowiki.llm.offpeak import off_peak_through
from evowiki.llm.prices import PriceTable
from evowiki.llm.usage import Account, PurposeGroup, Usage, UsageMeter

Tier = Literal["fast", "good"]
TIERS: tuple[Tier, ...] = get_args(Tier)
GROUPS: tuple[PurposeGroup, ...] = get_args(PurposeGroup)
DEFAULT_TIERS: Mapping[PurposeGroup, Tier] = MappingProxyType({
    "page": "good", "assistant": "good", "curator": "fast", "diagnostic": "fast",
    "review": "fast", "other": "good", "proactive": "good",
    # the harness round's improver (rsi.h.improve); its judge has its own gateway (harness.judge)
    "rsi": "good"})
# Per-purpose defaults (wiki tiers fix R1); they beat the group default, and a user's explicit
# per-group choice beats them. Unlisted purposes fall back to the group default.
DEFAULT_PURPOSE_TIERS: Mapping[str, Tier] = MappingProxyType({
    "sources.toc": "fast", "sources.summary": "fast", "sources.glossary": "fast",
    "sources.exam": "fast", "sources.exam_attach": "fast"})


# Planning and splitting always think (user rule 2026-10-23, after the 黑壳虾 intake ran every
# outline / split call on DeepSeek without thinking: 规划和拆分必须用思考模型): the goal's blocks and
# their splits, the gap judgement, the page plan, the page outline and the wiki's grouping /
# split (the curator names the graph's groups). These are "good" whatever the group
# choice or a pin says; every other purpose
# routes as below.
THINKING_PURPOSES: frozenset[str] = frozenset({
    "goals.intake", "goals.enter", "goals.reading.unit",
    "goals.path", "graph.group_naming",
})


def check_prices(configs: Iterable[GatewayConfig], prices: PriceTable) -> None:
    """Startup guard (plan rev 3.2 §9.4): every model any tier can route to (generator and
    verifier of each config) must have a price, or spending limits could not hold for it.
    Raises ValueError naming each missing provider:model, sorted, deduplicated."""
    missing = sorted({f"{ref.provider}:{ref.model}"
                      for config in configs for ref in (config.generator, config.verifier)
                      if prices.price(ref.provider, ref.model) is None})
    if missing:
        raise ValueError(f"no price for {', '.join(missing)} in the price table; add them to the "
                         "price file (spending limits cannot hold for unpriced models)")


# The user's model (the assistant composer's picker) is one per user, stored under this key of
# usage_settings.models: every call made for the user (chat, the tasks a chat starts, the Bot's own
# work) routes to it (D27, 2026-10-27: 「选的模型就是模型」); each group keeps its own tier.
PICK_KEY = "assistant"


def with_reasoning(config: GatewayConfig, effort: str) -> GatewayConfig:
    from evowiki.llm.catalog import reasoning_levels

    refs = {}
    for role in ("generator", "verifier"):
        ref = config.ref(role)
        if effort not in reasoning_levels(ref.provider, ref.model):
            raise ValueError("所选模型不支持这个思考强度，请重新选择")
        refs[role] = ref.model_copy(update={"reasoning_effort": effort})
    return config.model_copy(update=refs)


class TierSource(Protocol):
    def chosen_tier(self, account: Account) -> Tier | None:
        """The user's explicit tier for account.group, None when they never chose one."""
        ...


class ModelSource(Protocol):
    def chosen_model(self, account: Account) -> str | None:
        """The user's picked model (one per user, whatever account.group), None when never picked."""
        ...


class DefaultTiers:
    def chosen_tier(self, account: Account) -> Tier | None:
        return None

    def tier_for(self, account: Account) -> Tier:
        return DEFAULT_TIERS[account.group]


def tier_for(account: Account, purpose: str | None, chosen: Tier | None) -> Tier:
    """Precedence: a planning / splitting purpose (THINKING_PURPOSES) is always "good"; then the
    user's explicit group choice > account.tier_pin > purpose default > group default."""
    if purpose in THINKING_PURPOSES:
        return "good"
    if chosen is not None:
        return chosen
    if account.tier_pin is not None:
        return account.tier_pin
    if purpose is not None and purpose in DEFAULT_PURPOSE_TIERS:
        return DEFAULT_PURPOSE_TIERS[purpose]
    return DEFAULT_TIERS[account.group]


@dataclass(frozen=True)
class UsageControl:
    """tier_configs None: every tier uses the gateway's own config (tiers change nothing).
    clock: when a provider call is sent and answered, for models with an off-peak rate; None =
    every call at the peak rate (an upper bound).
    model_configs: model id -> its tier configs: every account whose user picked a model listed
    here routes to that model's config for the account's tier (planning purposes: the thinking
    tier); a pick that is not listed (its key is gone) routes as if none were made. `tiers` is
    then also asked `chosen_model` (a ModelSource)."""

    meter: UsageMeter
    prices: PriceTable
    tier_configs: Mapping[Tier, GatewayConfig] | None = None
    tiers: TierSource = field(default_factory=DefaultTiers)
    clock: Callable[[], datetime] | None = None
    model_configs: Mapping[str, Mapping[Tier, GatewayConfig]] | None = None
    # the model of a user who never picked one (must be in model_configs to apply): the account's
    # tier then picks among that model's levels too, as the picker shows
    default_model: str | None = None

    def __post_init__(self) -> None:
        if self.tier_configs is not None and set(self.tier_configs) != set(TIERS):
            raise ValueError(f"tier_configs must map exactly {TIERS}, "
                             f"got {sorted(self.tier_configs)}")
        for model, configs in (self.model_configs or {}).items():
            if set(configs) != set(TIERS):
                raise ValueError(f"model_configs[{model!r}] must map exactly {TIERS}, "
                                 f"got {sorted(configs)}")

    def config_for(self, account: Account, default: GatewayConfig,
                   purpose: str | None = None) -> GatewayConfig:
        picked = self.picked_model(account)
        if picked is not None:
            assert self.model_configs is not None
            config = self.model_configs[picked][tier_for(account, purpose, self.tiers.chosen_tier(account))]
            read = getattr(self.tiers, "chosen_reasoning", None)
            effort = read(account) if read is not None else None
            if purpose in THINKING_PURPOSES and (effort or config.generator.reasoning_effort) == "none":
                from evowiki.llm.catalog import reasoning_levels

                levels = reasoning_levels(config.generator.provider, config.generator.model)
                effort = next((level for level in levels if level not in ("none", "default")), "default")
            return with_reasoning(config, effort) if effort is not None else config
        if self.tier_configs is None:
            return default
        return self.tier_configs[tier_for(account, purpose, self.tiers.chosen_tier(account))]

    def picked_model(self, account: Account) -> str | None:
        """The model the account's user picked, when it routes here (listed in model_configs);
        else the default model when listed; else None."""
        if self.model_configs is None:
            return None
        chosen = getattr(self.tiers, "chosen_model", None)
        model = chosen(account) if chosen is not None else None
        if model is not None and model not in self.model_configs:
            raise ValueError("所选模型已不可用，请重新选择模型")
        if model in self.model_configs:
            return model
        if self.default_model is not None:
            if self.default_model not in self.model_configs:
                raise ValueError("默认模型未配置完整或缺少价格，请选择可用模型")
            return self.default_model
        return None

    def usage_of(self, provider: str, model: str, model_version: str | None,
                 input_tokens: int | None, output_tokens: int | None,
                 cache_read_tokens: int | None = 0,
                 cache_write_tokens: int | None = 0,
                 cache_write_1h_tokens: int | None = 0, *,
                 started_at: datetime | None = None,
                 ended_at: datetime | None = None) -> Usage:
        """Price by the model that actually served (model_version), else the configured model.
        Unreported input/output counts are recorded as 0 with cost None: visible, never
        guessed. Unreported cache counts are 0 (the provider read or wrote nothing it said).
        cache_write_1h_tokens: the part of the writes made with the 1-hour TTL (priced higher);
        Usage.cache_write_tokens stays the total. started_at / ended_at: when the call was sent
        and answered; the off-peak rate applies only when both are given and the whole call is
        in the off-peak window (spec 2026-10-21-offpeak-writes §1)."""
        read, write = cache_read_tokens or 0, cache_write_tokens or 0
        long = cache_write_1h_tokens or 0
        off = (started_at is not None and ended_at is not None
               and off_peak_through(started_at, ended_at))
        cost = None
        if input_tokens is not None and output_tokens is not None:
            if model_version:
                cost = self.prices.cost(provider, model_version, input_tokens, output_tokens,
                                        read, write, long, off_peak=off)
            if cost is None:
                cost = self.prices.cost(provider, model, input_tokens, output_tokens,
                                        read, write, long, off_peak=off)
        return Usage(provider=provider, model=model, input_tokens=input_tokens or 0,
                     output_tokens=output_tokens or 0, cache_read_tokens=read,
                     cache_write_tokens=write, cost_usd=cost)

    def now(self) -> datetime | None:
        return None if self.clock is None else self.clock()
