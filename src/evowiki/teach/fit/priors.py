"""Hierarchical priors over the θ_infer paths.

Each path has a prior on the logit scale of its bounds: z = logit((v - lo) / (hi - lo)) with the
unit value clamped 1e-3 inside [0, 1], normal with mean logit(prior mean) and sd `sd_logit`.
`priors.json` holds the committed priors; a path missing there gets the default prior (the
`InferParams()` value, sd 1.5). Unknown paths or invalid entries fail closed."""

import json
import math
from dataclasses import dataclass
from pathlib import Path

from evowiki.teach import infer_paths as ip
from evowiki.teach.contracts import InferParams

PRIORS_FILE = Path(__file__).with_name("priors.json")
DEFAULT_SD_LOGIT = 1.5
EDGE = 1e-3                     # unit-scale values are clamped this far inside [0, 1]

_DEFAULT = InferParams()


@dataclass(frozen=True)
class PathPrior:
    mean: float          # on the path's natural scale, inside bounds(path)
    sd_logit: float      # sd of logit((v - lo)/(hi - lo)); larger = weaker
    source: str          # "default" | "<dataset>@<derivation version>"


def default_prior(path: str) -> PathPrior:
    return PathPrior(mean=ip.get(_DEFAULT, path), sd_logit=DEFAULT_SD_LOGIT, source="default")


def to_logit(path: str, value: float) -> float:
    """logit of `value`'s position inside bounds(path), clamped EDGE inside."""
    lo, hi = ip.bounds(path)
    u = min(1.0 - EDGE, max(EDGE, (float(value) - lo) / (hi - lo)))
    return math.log(u / (1.0 - u))


def from_logit(path: str, z: float) -> float:
    lo, hi = ip.bounds(path)
    return lo + (hi - lo) / (1.0 + math.exp(-z))


def _check(path: str, p: PathPrior) -> PathPrior:
    lo, hi = ip.bounds(path)        # raises KeyError on an unknown path
    if not (math.isfinite(p.mean) and lo <= p.mean <= hi):
        raise ValueError(f"prior mean of {path} ({p.mean}) is outside [{lo}, {hi}]")
    if not (math.isfinite(p.sd_logit) and p.sd_logit > 0.0):
        raise ValueError(f"prior sd_logit of {path} must be > 0, got {p.sd_logit}")
    if not p.source:
        raise ValueError(f"prior of {path} has no source")
    return p


def load_priors(file: Path | None = None) -> dict[str, PathPrior]:
    """Every θ_infer path → its prior: the file's entry, else the default prior."""
    raw = json.loads((file or PRIORS_FILE).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("priors file must hold an object of path → prior")
    out = {path: default_prior(path) for path in ip.PATHS}
    for path, e in raw.items():
        if path not in out:
            raise ValueError(f"prior for unknown θ_infer path {path!r}")
        try:
            p = PathPrior(mean=float(e["mean"]), sd_logit=float(e["sd_logit"]),
                          source=str(e["source"]))
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError(f"bad prior entry for {path}: {e!r}") from err
        out[path] = _check(path, p)
    return out


def log_prior(path: str, value: float, prior: PathPrior, strength: float) -> float:
    """-0.5 * strength * (z / sd)^2 with z the logit-scale distance from the prior mean."""
    z = to_logit(path, value) - to_logit(path, prior.mean)
    return -0.5 * strength * (z / prior.sd_logit) ** 2
