"""Dotted paths into θ_infer,u (`InferParams`): read, clamp-and-set, bounds, complexity.

The path list is the RSI-A whitelist: the evidence weights, then the five scalars. Bounds come
from the contract (the `Field` ge / le constraints; evidence weights are [0, 2] by its validator,
the two claim trust levels [0, 1]),
so a path can never be set outside what `InferParams` accepts."""

from typing import get_args

from annotated_types import Ge, Le

from evowiki.teach.contracts import CLAIM_KINDS, EvidenceKind, InferParams

_WEIGHT_PREFIX = "evidence_weight."
_WEIGHT_BOUNDS = (0.0, 2.0)
_CLAIM_BOUNDS = (0.0, 1.0)
_SCALARS = ("forgetting_scale", "slip", "guess_scale", "inference_strength", "start_known")
PATHS: tuple[str, ...] = (
    tuple(_WEIGHT_PREFIX + k for k in get_args(EvidenceKind)) + _SCALARS)

PATH_LABEL: dict[str, str] = {
    "evidence_weight.quiz": "测验结果权重",
    "evidence_weight.check": "知识检查权重",
    "evidence_weight.self": "自我评估权重",
    "evidence_weight.feedback": "学习反馈权重",
    "evidence_weight.ask": "提问行为权重",
    "evidence_weight.recall": "复习表现权重",
    "evidence_weight.slip_claim": "「粗心失误」反馈可信度",
    "evidence_weight.learned_claim": "「已掌握」反馈可信度",
    "forgetting_scale": "记忆保持系数",
    "slip": "已掌握时的答错概率",
    "guess_scale": "猜对概率系数",
    "inference_strength": "关联知识推断强度",
    "start_known": "初始掌握概率",
}

PATH_DESCRIPTION: dict[str, str] = {
    "evidence_weight.quiz": "测验结果在掌握评估中的影响大小。数值越大，测验结果的影响越强；越小则越弱。这是权重，不是百分比。",
    "evidence_weight.check": "页面确认题结果在掌握评估中的影响大小。数值越大，确认题结果的影响越强；越小则越弱。这是权重，不是百分比。",
    "evidence_weight.self": "自我评估在掌握判断中的影响大小。数值越大，你对自己水平的评价影响越强；越小则越弱。这是权重，不是百分比。",
    "evidence_weight.feedback": "学习反馈在掌握评估中的影响大小。数值越大，你对学习内容的反馈影响越强；越小则越弱。这是权重，不是百分比。",
    "evidence_weight.ask": "提问信息在掌握评估中的影响大小。数值越大，提问中反映的掌握情况影响越强；越小则越弱。这是权重，不是百分比。",
    "evidence_weight.recall": "复习表现对掌握评估的影响大小。数值越大，复习时的回忆表现影响越强；越小则越弱。这是权重，不是百分比。",
    "evidence_weight.slip_claim": "你表示只是粗心时，对这句话的信任程度，范围为 0 到 1。越大，越会减轻本次答错的负面影响；越小，越会保留答错的影响。",
    "evidence_weight.learned_claim": "你表示已经掌握时，对这句话的信任程度，范围为 0 到 1。越大，越会增加掌握依据；越小则越谨慎，之后仍需答题确认。",
    "forgetting_scale": "记忆保持时间的倍率，1 表示基准。越大，系统估计同一知识能记得更久；越小，则预计更快需要复习。",
    "slip": "已掌握知识时仍可能答错的假设概率，例如 0.1 表示 10%。越大，答错对掌握评估的负面影响越小；越小则相反。",
    "guess_scale": "基础猜中概率的倍率，1 表示基准。越大，系统越会考虑答对可能是猜中，同一次答对对掌握的证明越弱；越小则相反。它不是实际答对率。",
    "inference_strength": "从相关知识推断掌握情况的强度。越大，关联知识的推断影响越强；越小则越弱，0 表示不传播这类推断。",
    "start_known": "缺少学习记录时，对已有基础的初始估计。越大，初始判断越倾向已有基础；越小则越谨慎。之后会随学习记录更新，并非所有知识点都直接以这个数值开始。",
}

_DEFAULT = InferParams()


def _check(path: str) -> None:
    if path not in PATHS:
        raise KeyError(f"unknown θ_infer path {path!r}")


def bounds(path: str) -> tuple[float, float]:
    _check(path)
    if path.startswith(_WEIGHT_PREFIX):
        return _CLAIM_BOUNDS if path[len(_WEIGHT_PREFIX):] in CLAIM_KINDS else _WEIGHT_BOUNDS
    lo = hi = None
    for m in InferParams.model_fields[path].metadata:
        if isinstance(m, Ge):
            lo = float(m.ge)
        elif isinstance(m, Le):
            hi = float(m.le)
    if lo is None or hi is None:      # fail closed: every whitelisted scalar must be bounded
        raise ValueError(f"{path} has no ge/le bounds in the contract")
    return lo, hi


def get(p: InferParams, path: str) -> float:
    _check(path)
    if path.startswith(_WEIGHT_PREFIX):
        return float(p.evidence_weight[path[len(_WEIGHT_PREFIX):]])
    return float(getattr(p, path))


def set_(p: InferParams, path: str, value: float) -> InferParams:
    """A new, validated InferParams with `path` set to `value` clamped to `bounds(path)`."""
    lo, hi = bounds(path)
    v = min(hi, max(lo, float(value)))
    data = p.model_dump()
    if path.startswith(_WEIGHT_PREFIX):
        data["evidence_weight"] = {**data["evidence_weight"], path[len(_WEIGHT_PREFIX):]: v}
    else:
        data[path] = v
    return InferParams(**data)


def complexity(p: InferParams) -> int:
    """How many whitelisted paths differ from the default."""
    return sum(1 for path in PATHS if get(p, path) != get(_DEFAULT, path))
