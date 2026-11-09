"""Price table (plan rev 3.2 §9.4): USD per million tokens, keyed "provider:model".

A model without an entry gets cost None; its tokens are still recorded. Prompt-cache tokens
(read from / written to a cache) are charged at the model's cache rates when the file gives them
(`cache_read_per_mtok`, `cache_write_per_mtok` for 5-minute writes, `cache_write_1h_per_mtok` for
1-hour writes — twice the input rate when missing —, each optional on its own: DeepSeek's context
cache, for one, has a read rate and no write charge), else at its input rate (an upper bound).
`off_peak_factor` (optional): the model is billed at that fraction of every rate in DeepSeek's
off-peak window (`llm.offpeak`); the caller says whether a call was off-peak.
`long_prompt_over_tokens` + `long_prompt_factor` (optional, both or neither): Claude Haiku 5.5's second
rate card, chosen per request by prompt length, cached tokens included. A malformed file fails closed."""

import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

DEFAULT_PRICES_PATH = Path(__file__).with_name("prices.toml")
_REQUIRED = {"input_per_mtok", "output_per_mtok"}
_CACHE = {"cache_read_per_mtok", "cache_write_per_mtok", "cache_write_1h_per_mtok"}
_LONG = {"long_prompt_over_tokens", "long_prompt_factor"}
_FIELDS = _REQUIRED | _CACHE | _LONG | {"off_peak_factor"}


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float | None = None    # None: cache reads at the input rate
    cache_write_per_mtok: float | None = None   # None: cache writes at the input rate
    cache_write_1h_per_mtok: float | None = None   # None: 1-hour writes at twice the input rate
    off_peak_factor: float | None = None   # None: one price at every hour
    long_prompt_over_tokens: int | None = None   # None: one price at every prompt length
    long_prompt_factor: float = 1.0

    def prompt_factor(self, prompt_tokens: int) -> float:
        """What every rate of a request with this long a prompt is multiplied by."""
        over = self.long_prompt_over_tokens
        return self.long_prompt_factor if over is not None and prompt_tokens > over else 1.0


class PriceTable:
    def __init__(self, prices: Mapping[str, Price]):
        self._prices = MappingProxyType(dict(prices))

    def price(self, provider: str, model: str) -> Price | None:
        return self._prices.get(f"{provider}:{model}")

    def overlay(self, other: "PriceTable") -> "PriceTable":
        return PriceTable({**self._prices, **other._prices})

    def cost(self, provider: str, model: str, input_tokens: int, output_tokens: int,
             cache_read_tokens: int = 0, cache_write_tokens: int = 0,
             cache_write_1h_tokens: int = 0, off_peak: bool = False) -> float | None:
        """input_tokens: billed at the full input rate (the uncached remainder).
        cache_write_tokens: every cache write; cache_write_1h_tokens the part of them made with
        the 1-hour TTL (Anthropic: 2x input, 5-minute writes 1.25x). off_peak: the whole call was
        in the off-peak window (a model without `off_peak_factor` ignores it). The prompt length
        that picks the rate card is input + cache reads + cache writes."""
        p = self.price(provider, model)
        if p is None:
            return None
        read = p.input_per_mtok if p.cache_read_per_mtok is None else p.cache_read_per_mtok
        write = p.input_per_mtok if p.cache_write_per_mtok is None else p.cache_write_per_mtok
        write_1h = (2 * p.input_per_mtok if p.cache_write_1h_per_mtok is None
                    else p.cache_write_1h_per_mtok)
        long = min(cache_write_1h_tokens, cache_write_tokens)
        factor = p.off_peak_factor if off_peak and p.off_peak_factor is not None else 1.0
        factor *= p.prompt_factor(input_tokens + cache_read_tokens + cache_write_tokens)
        return factor * (input_tokens * p.input_per_mtok + output_tokens * p.output_per_mtok
                + cache_read_tokens * read + (cache_write_tokens - long) * write
                + long * write_1h) / 1_000_000


def _rate(key: str, name: str, v: object) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ValueError(f"{key}.{name} must be a number, got {v!r}")  # noqa: TRY004
    if not math.isfinite(v) or v < 0:
        raise ValueError(f"{key}.{name} must be a finite non-negative number, got {v!r}")
    return float(v)


def _factor(key: str, v: object) -> float:
    f = _rate(key, "off_peak_factor", v)
    if not 0 < f <= 1:
        raise ValueError(f"{key}.off_peak_factor must be in (0, 1], got {v!r}")
    return f


def _long_prompt(key: str, entry: Mapping[str, object]) -> dict[str, float | int]:
    if not _LONG & set(entry):
        return {}
    if not _LONG <= set(entry):
        raise ValueError(f"{key} must give both of {sorted(_LONG)}")
    over = entry["long_prompt_over_tokens"]
    factor = _rate(key, "long_prompt_factor", entry["long_prompt_factor"])
    if isinstance(over, bool) or not isinstance(over, int) or over < 1:
        raise ValueError(f"{key}.long_prompt_over_tokens must be a positive integer, got {over!r}")
    if factor < 1:
        raise ValueError(f"{key}.long_prompt_factor must be at least 1, got {factor!r}")
    return {"long_prompt_over_tokens": over, "long_prompt_factor": factor}


def load_prices(path: Path = DEFAULT_PRICES_PATH) -> PriceTable:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    if set(data) != {"models"}:
        raise ValueError(f"{path}: the only top-level table must be [models], got {sorted(data)}")
    prices: dict[str, Price] = {}
    for key, entry in data["models"].items():
        provider, sep, model = key.partition(":")
        if not sep or not provider or not model:
            raise ValueError(f"{path}: key {key!r} must be provider:model")
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: {key} must be a table")  # noqa: TRY004
        if set(entry) - _FIELDS:
            raise ValueError(f"{path}: {key} has unknown fields {sorted(set(entry) - _FIELDS)}")
        for name in sorted(_REQUIRED - set(entry)):
            raise ValueError(f"{path}: {key} is missing {name}")
        prices[key] = Price(
            _rate(key, "input_per_mtok", entry["input_per_mtok"]),
            _rate(key, "output_per_mtok", entry["output_per_mtok"]),
            **{n: _rate(key, n, entry[n]) for n in sorted(_CACHE) if n in entry},
            off_peak_factor=_factor(key, entry["off_peak_factor"]) if "off_peak_factor" in entry
            else None,
            **_long_prompt(key, entry))
    return PriceTable(prices)
