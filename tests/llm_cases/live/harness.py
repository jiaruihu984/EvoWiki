"""Live model-case support with explicit paid-run opt-in and usage accounting.

Configuration defaults to the checkout's .env; EVOWIKI_ENV_FILE overrides it.
DeepSeek comparison mode excludes the Anthropic key unless EVOWIKI_LIVE_MODEL
selects Sonnet. Calls use in-memory logs and meters and write usage summaries to
.live-results. Live cases require EVOWIKI_LIVE=1 and are excluded from CI.
"""

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from evowiki.llm.gateway import Gateway
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.prices import load_prices
from evowiki.llm.usage import Account, Usage
from evowiki.llm.usage_control import UsageControl, check_prices
from evowiki.runtime.llm import (
    DEFAULT_RUNTIME_MODEL,
    RUNTIME_RECORD_MAX_ATTEMPTS,
    provider_config_problem,
    runtime_gateway_config,
    runtime_providers,
)
from evowiki.runtime.usage import runtime_tier_configs
from evowiki.wikiapp.llm import CountingGateway, GatewayJsonLLM
from tests.runtime.llm_helpers import MemoryAttemptLog, MemoryCallLog

ENV_FILE = Path(os.environ.get("EVOWIKI_ENV_FILE") or Path(__file__).resolve().parents[3] / ".env")
RESULTS = Path(__file__).resolve().parents[3] / ".live-results"
LIVE_USER = "live-case"


def dev_env(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"')
    if os.environ.get("EVOWIKI_LIVE_MODEL") == "sonnet":
        return env          # Claude-only comparison run (see live_gateway); keeps the Anthropic key
    env.pop("ANTHROPIC_API_KEY", None)
    env["EVOWIKI_RUNTIME_PROVIDER"] = "deepseek"
    return env


def _sonnet_only(tiers: dict) -> tuple[GatewayConfig, dict]:
    """Compare every role and tier using Sonnet when explicitly selected."""
    claude = ModelRef(provider="anthropic", model=DEFAULT_RUNTIME_MODEL)
    config = GatewayConfig(generator=claude, verifier=claude, allow_single_provider=True)
    return config, {t: config for t in tiers}


@dataclass
class MemoryMeter:
    """UsageMeter without a budget: every paid response is kept."""

    rows: list[tuple[str, Usage, bool]] = field(default_factory=list)

    def check(self, account: Account) -> None:
        return None

    def record(self, account: Account, purpose: str, usage: Usage, *, replayed: bool) -> None:
        self.rows.append((purpose, usage, replayed))

    def chosen_tier(self, account: Account):
        return None


TOKEN_FIELDS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")


def hit_rate(t: dict) -> float | None:
    prompt = t["input_tokens"] + t["cache_read_tokens"] + t["cache_write_tokens"]
    return round(t["cache_read_tokens"] / prompt, 4) if prompt else None


@dataclass
class Live:
    """One live case: `gateway` (metered, counted), `json_llm` for goals/ code, `spend()`."""

    name: str
    gateway: Gateway
    meter: MemoryMeter
    started: float = field(default_factory=time.monotonic)
    attempts: MemoryAttemptLog | None = None
    reported: bool = False

    def counting(self) -> CountingGateway:
        return CountingGateway(self.gateway, LIVE_USER)

    def json_llm(self) -> GatewayJsonLLM:
        return GatewayJsonLLM(self.counting(), subject=self.name)

    def spend(self, purposes: tuple[str, ...] | None = None) -> dict:
        """Paid (not replayed) meter rows: calls, cost and token counts per purpose and in total.
        input_tokens is the uncached remainder (DeepSeek: the cache misses); cache_read_tokens
        the prompt-cache hits; `cache_hit_rate` = cache reads / all prompt tokens (None when no
        prompt token was reported)."""
        rows = [r for r in self.meter.rows if not r[2]
                and (purposes is None or r[0] in purposes)]
        by: dict[str, dict] = {}
        for purpose, u, _ in rows:
            b = by.setdefault(purpose, {"calls": 0, "cost_usd": 0.0, "unpriced": 0,
                                        **dict.fromkeys(TOKEN_FIELDS, 0)})
            b["calls"] += 1
            if u.cost_usd is None:
                b["unpriced"] += 1
            else:
                b["cost_usd"] += u.cost_usd
            for k in TOKEN_FIELDS:
                b[k] += getattr(u, k)
        for b in by.values():
            b["cache_hit_rate"] = hit_rate(b)
        tokens = {k: sum(b[k] for b in by.values()) for k in TOKEN_FIELDS}
        return {"calls": len(rows), "cost_usd": round(sum(b["cost_usd"] for b in by.values()), 6),
                **tokens, "cache_hit_rate": hit_rate(tokens),
                "seconds": round(time.monotonic() - self.started, 1), "by_purpose": by}

    @contextmanager
    def always_reported(self) -> Iterator[None]:
        """A case that raises still writes its report: what it paid is never lost."""
        try:
            yield
        except BaseException as exc:
            if not self.reported:             # an assert after `report` keeps that report
                self.report({"raised": f"{type(exc).__name__}: {exc}"[:500]})
            raise

    def report(self, extra: dict | None = None) -> dict:
        self.reported = True
        out = {"case": self.name, **self.spend(), **(extra or {})}
        RESULTS.mkdir(exist_ok=True)
        (RESULTS / f"{self.name}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                                   encoding="utf-8")
        # every model output of the case (purpose, version, raw text), so a failure can be read
        # afterwards instead of being run again
        log = getattr(self.gateway, "_log", None)
        calls = log.calls() if log is not None and hasattr(log, "calls") else []
        (RESULTS / f"{self.name}.calls.jsonl").write_text(
            "".join(json.dumps({"purpose": c.purpose, "prompt_version": c.prompt_version,
                                "latency": c.latency, "raw_output": c.raw_output},
                               ensure_ascii=False) + "\n" for c in calls), encoding="utf-8")
        # every provider attempt, the rejected and the starved ones too: why a case paid for more
        # calls than it kept
        tried = self.attempts.attempts() if self.attempts is not None else []
        (RESULTS / f"{self.name}.attempts.jsonl").write_text(
            "".join(json.dumps({"purpose": a.purpose, "status": a.status,
                                "error_type": a.error_type, "error_message": a.error_message,
                                "params": a.params, "cost": a.cost, "raw_output": a.raw_output},
                               ensure_ascii=False) + "\n" for a in tried), encoding="utf-8")
        print(f"\n[live] {self.name}: {out['calls']} calls, {out['seconds']} s, "
              f"${out['cost_usd']:.4f}, tokens in {out['input_tokens']} / cache read "
              f"{out['cache_read_tokens']} / out {out['output_tokens']}"
              + ("" if out["cache_hit_rate"] is None
                 else f", cache hit rate {out['cache_hit_rate']:.0%}"))
        return out


def live_gateway(name: str) -> Live:
    """A fresh metered runtime gateway with the fast / good tiers (the wiki flows' calls carry an
    account, so they route by purpose). Fails loudly when the env file or a provider key is
    missing (never silently falls back to fakes)."""
    env = dev_env()
    config = runtime_gateway_config(env)
    tiers = runtime_tier_configs(env)
    if os.environ.get("EVOWIKI_LIVE_MODEL") == "sonnet":
        config, tiers = _sonnet_only(tiers)
    prices = load_prices()
    check_prices(list(tiers.values()), prices)
    providers = runtime_providers(env)
    if (problem := provider_config_problem(config, providers)) is not None:
        raise RuntimeError(f"live case {name}: {problem}")
    meter = MemoryMeter()
    usage = UsageControl(meter, prices, tier_configs=tiers, tiers=meter)
    attempts = MemoryAttemptLog()       # every attempt's raw output, the rejected ones too
    gw = Gateway(config, MemoryCallLog(), mode="record", providers=providers,
                 record_max_attempts=RUNTIME_RECORD_MAX_ATTEMPTS, usage=usage,
                 attempts=attempts)
    return Live(name=name, gateway=gw, meter=meter, attempts=attempts)


@dataclass
class LiveDeep(Live):
    """One live page-writer case: `llm` is the deep tier (`DeepLLM`, what `deep.writer.PageWriter`
    takes) over the same in-memory call log and meter; `gateway` is its primary writer, `spend()` /
    `report()` as for `Live` (the deep.write rows)."""

    llm: object = None            # evowiki.wikiapp.refine_service.DeepLLM


def live_deep(name: str) -> LiveDeep:
    """A fresh metered deep tier built like `service.deps.build_deep` (runtime_deep_config /
    runtime_deep_providers, the writer fallback when both keys exist), DeepSeek-only per `dev_env`
    (so the writer is DeepSeek's good tier, thinking on). Fails loudly when a provider is missing."""
    from evowiki.deep.llm import DeepLLM
    from evowiki.runtime.llm import runtime_deep_providers
    from evowiki.runtime.usage import runtime_deep_config, runtime_deep_fallback_config

    env = dev_env()
    config, fallback = runtime_deep_config(env), runtime_deep_fallback_config(env)
    prices = load_prices()
    check_prices([config, *([fallback] if fallback else [])], prices)
    providers = runtime_deep_providers(env)
    if (problem := provider_config_problem(config, providers)) is not None:
        raise RuntimeError(f"live case {name}: {problem}")
    meter = MemoryMeter()
    llm = DeepLLM(config=config, providers=providers, log=MemoryCallLog(), prices=prices,
                  meter=meter, fallback=fallback)
    gw, _ = llm.gateway(None)
    return LiveDeep(name=name, gateway=gw, meter=meter, llm=llm)
