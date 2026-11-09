"""The fixed evaluator of the conversation summary (assistant.compact, compact@v7): how good one
reply is, for the RSI loop's single-step reruns (observability plan §9) and the fixed cases
(`evaluation.compact_cases`).

Protected: `src/evowiki/evaluation/**` is denied to code patches (forge/policy.DENIED); the judge
prompt below has no ⟦g⟧ marks and is no prompt step (llm/prompt_steps.STEPS), so neither the H
line nor any improver can change it. Only the user changes this file, the thresholds included.
It deliberately does not reuse the product's parsing or checks (assistant/summary.py can be
patched): it reads the raw reply itself, and the evidence it allows comes from the stored request.

Four checks:
- structure (code): the reply is the v7 shape; every learning item cites an allowed id
  (`params["evidence_ids"]` of the stored request); how long the summary is against its input
  (`compression`, at most MAX_COMPRESSION, measured only on an input of COMPRESSION_MIN_INPUT
  characters or more: a short dialogue has little to compress);
- kept the key information (judge): the points the next assistant must know — the fixed list of a
  case, or the judge's own list when none is given — each kept or not (`retention`, at least
  MIN_RETENTION);
- made nothing up (judge): claims of the summary the input does not support, each quoted from
  the summary; a quote that is not in the summary is the judge's error and is not counted;
- tasks right (judge): learning items that are really a task, a system problem or an unanswered
  question (or have no evidence in his own words).

`evaluate(request, raw_output, gateway=...)` is the one entry point; without a gateway only the
code part runs (free). The judge is one call on the gateway's "verifier" role; a judge reply that
does not parse raises (the gateway's LLMParseError): an error, never a silent pass."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evowiki.llm.gateway import Gateway
from evowiki.llm.models import LLMCall, LLMRequest
from evowiki.llm.usage import Account

__all__ = ["COMPRESSION_MIN_INPUT", "EVAL_PURPOSE", "EVAL_VERSION", "MAX_COMPRESSION", "MIN_RETENTION", "CompactEval",
           "JudgeOut", "Structure", "evaluate", "judge_prompt", "structure", "summary_text"]

EVAL_PURPOSE = "eval.compact.judge"
EVAL_VERSION = "compact-eval@v2"
EVIDENCE_PARAM = "evidence_ids"      # the stored request's param (assistant/summary.py writes it)

MAX_COMPRESSION = 0.6     # summary characters / input characters above this: not compressed enough
COMPRESSION_MIN_INPUT = 1000   # input characters below this: too short for the ratio to mean anything
MIN_RETENTION = 0.8       # kept key points / key points below this: lost too much

SYSTEM = "只回复 JSON。"


@dataclass(frozen=True)
class Structure:
    schema_ok: bool
    error: str = ""                                   # why the shape is wrong ("" when ok)
    learning_items: int = 0
    bad_evidence: tuple[str, ...] = ()                # "unclear:<concept> (<id>)" not allowed
    summary_chars: int = 0
    input_chars: int = 0

    @property
    def compression(self) -> float:
        return self.summary_chars / self.input_chars if self.input_chars else 0.0


def _items(obj: Any, key: str) -> list[Any]:
    v = obj.get(key, [])
    if not isinstance(v, list):
        raise TypeError(f"{key} is not a list")
    return v


def _reply(raw: str) -> dict[str, Any]:
    """The reply as plain data; ValueError / TypeError when it is not the v7 shape."""
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"handoff", "learning"}:
        raise ValueError("top level must be exactly handoff + learning")
    h, ln = data["handoff"], data["learning"]
    if not isinstance(h, dict) or not isinstance(ln, dict):
        raise TypeError("handoff / learning must be objects")
    if not isinstance(h.get("summary", ""), str):
        raise TypeError("handoff.summary must be a string")
    for key in ("decisions", "active_requests"):
        if not all(isinstance(x, str) for x in _items(h, key)):
            raise TypeError(f"handoff.{key} must hold strings")
    for key in ("unclear", "understood"):
        for e in _items(ln, key):
            if not (isinstance(e, dict) and isinstance(e.get("concept"), str)
                    and isinstance(e.get("evidence_id"), str)):
                raise TypeError(f"a learning.{key} item needs concept + evidence_id")
    return data


def summary_text(data: dict[str, Any]) -> str:
    """The reply as the judge reads it (and as its length is counted): every field, labelled."""
    h, ln = data["handoff"], data["learning"]
    lines = [f"交接：{h.get('summary', '').strip()}"]
    lines += [f"定下的事：{d}" for d in _items(h, "decisions")]
    lines += [f"这次对话里的要求：{r}" for r in _items(h, "active_requests")]
    lines += [f"他没懂的：{e['concept']}" for e in _items(ln, "unclear")]
    lines += [f"他懂了的：{e['concept']}" for e in _items(ln, "understood")]
    return "\n".join(lines)


def structure(request: LLMRequest, raw_output: str) -> Structure:
    """The code checks of one reply to `request` (the stored compaction request)."""
    try:
        data = _reply(raw_output)
    except (ValueError, TypeError) as exc:
        return Structure(schema_ok=False, error=str(exc)[:200], input_chars=len(request.prompt))
    allowed = set(request.params.get(EVIDENCE_PARAM) or ())
    bad, n = [], 0
    for key in ("unclear", "understood"):
        for e in data["learning"].get(key, []):
            n += 1
            if e["evidence_id"].strip() not in allowed:
                bad.append(f"{key}:{e['concept']} ({e['evidence_id']})")
    body = summary_text(data)
    return Structure(schema_ok=True, learning_items=n, bad_evidence=tuple(bad),
                     summary_chars=len(body), input_chars=len(request.prompt))


class KeyPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point: str = Field(max_length=300)
    kept: bool


class Flagged(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote: str = Field(max_length=300)      # copied from the summary
    why: str = Field(max_length=300)


class JudgeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_points: list[KeyPoint] = Field(max_length=20)
    made_up: list[Flagged] = Field(default_factory=list, max_length=20)
    missed_tasks: list[str] = Field(default_factory=list, max_length=20)
    wrong_tasks: list[Flagged] = Field(default_factory=list, max_length=20)
    misfiled: list[Flagged] = Field(default_factory=list, max_length=20)


def judge_prompt(material: str, summary: str, key_points: Sequence[str] | None) -> str:
    if key_points:
        points = ("要检查的关键信息（逐条判断交接里有没有保留，key_points 按这个顺序、一条不漏地填，"
                  "point 原样照抄）：\n" + "\n".join(f"- {p}" for p in key_points))
    else:
        points = ("先只看原始材料，列出接手的助手必须知道的关键信息（3 到 8 条：讲到哪了、定下的事、"
                  "他提的要求、没做完的事、他明确说没懂或懂了的点），再逐条判断交接里有没有保留。")
    return f"""一位陪读助手把一段较早的对话整理成交接记录，交给之后接着聊的助手。请检查这份交接记录。

【原始材料】（可能带着更早的交接，然后是要整理的对话；方括号里是用户消息的编号）
{material}

【交接记录】
{summary}

{points}
意思保留下来就算保留，不要求原话。

再检查三件事，每一条都从【交接记录】里原样抄一句或一段放进 quote（一字不改），并用一句话说明 why：
1. made_up：原始材料里没有、或和材料矛盾的说法。合理的概括不算。
2. wrong_tasks：写成没做完、其实已经做完、撤销了或根本不是待办的事。
3. misfiled：写成「他没懂的」，其实是待办、系统问题、助手出了题他还没答，或他并没说过没懂、也没答错的点；写成「他懂了的」，但他并没说过懂、也没答对的点。更早的交接里已有的「他没懂的」「他懂了的」条目，原话在更早的对话里，这里看不到，它们沿用下来就算有证据；只有这段对话显示它已经解决或不再成立时才算放错。
另外把原始材料里还没做完、交接却漏掉的事写进 missed_tasks（每条一句）。
没有就留空列表。

只输出 JSON：{{"key_points": [{{"point": "…", "kept": true}}], "made_up": [{{"quote": "…", "why": "…"}}], "missed_tasks": ["…"], "wrong_tasks": [{{"quote": "…", "why": "…"}}], "misfiled": [{{"quote": "…", "why": "…"}}]}}"""


@dataclass(frozen=True)
class CompactEval:
    structure: Structure
    judged: bool = False
    retention: float | None = None                    # kept / key points (judge)
    made_up: tuple[str, ...] = ()                     # quoted claims the input does not support
    task_errors: tuple[str, ...] = ()                 # missed / wrong / misfiled, labelled
    quote_misses: int = 0                             # judge quotes not found in the summary
    judge_call: LLMCall | None = None
    key_points: tuple[tuple[str, bool], ...] = field(default=())

    @property
    def compression_measured(self) -> bool:
        return self.structure.input_chars >= COMPRESSION_MIN_INPUT

    @property
    def compressed(self) -> bool:
        s = self.structure
        return s.schema_ok and (not self.compression_measured or s.compression <= MAX_COMPRESSION)

    @property
    def passed(self) -> bool:
        """Every check holds (the judge's only when it ran)."""
        s = self.structure
        ok = s.schema_ok and not s.bad_evidence and self.compressed
        if self.judged:
            ok = ok and (self.retention or 0.0) >= MIN_RETENTION and not self.made_up \
                and not self.task_errors
        return ok

    def report(self) -> dict[str, Any]:
        s = self.structure
        return {"passed": self.passed, "schema_ok": s.schema_ok, "error": s.error,
                "learning_items": s.learning_items, "bad_evidence": list(s.bad_evidence),
                "compression": round(s.compression, 3), "compressed": self.compressed,
                "compression_measured": self.compression_measured,
                "judged": self.judged, "retention": self.retention,
                "key_points": [list(k) for k in self.key_points],
                "made_up": list(self.made_up), "task_errors": list(self.task_errors),
                "quote_misses": self.quote_misses,
                "judge_call": self.judge_call.id if self.judge_call else None}


def _squash(text: str) -> str:
    return "".join(text.split())


def evaluate(request: LLMRequest, raw_output: str, *, gateway: Gateway | None = None,
             account: Account | None = None, key_points: Sequence[str] | None = None
             ) -> CompactEval:
    """Evaluate one reply to a stored compaction request. `gateway` None: the code checks only.
    `key_points`: the fixed points of a case (else the judge lists its own). A reply whose shape
    is wrong is never judged (it already failed)."""
    s = structure(request, raw_output)
    if gateway is None or not s.schema_ok:
        return CompactEval(structure=s)
    summary = summary_text(_reply(raw_output))
    req = LLMRequest(purpose=EVAL_PURPOSE, prompt_version=EVAL_VERSION, subject=request.subject,
                     system=SYSTEM, prompt=judge_prompt(request.prompt, summary, key_points))
    out, call = gateway.call("verifier", req, JudgeOut,
                             account=account if gateway.metered else None)
    points = [(k.point, k.kept) for k in out.key_points]
    if key_points:                       # the fixed list, in order: a skipped point is not kept
        given = {p: kept for p, kept in points}
        points = [(p, given.get(p, False)) for p in key_points]
    in_summary = _squash(summary)
    misses = 0

    def quoted(items: Sequence[Flagged], label: str) -> list[str]:
        nonlocal misses
        kept = []
        for f in items:
            if f.quote.strip() and _squash(f.quote) in in_summary:
                kept.append(f"{label}{f.quote}：{f.why}" if label else f"{f.quote}：{f.why}")
            else:
                misses += 1
        return kept

    made_up = quoted(out.made_up, "")
    tasks = [*(f"漏了：{t}" for t in out.missed_tasks if t.strip()),
             *quoted(out.wrong_tasks, "不该是待办："), *quoted(out.misfiled, "放错了：")]
    retention = sum(k for _, k in points) / len(points) if points else 1.0
    return CompactEval(structure=s, judged=True, retention=retention, made_up=tuple(made_up),
                       task_errors=tuple(tasks), quote_misses=misses, judge_call=call,
                       key_points=tuple(points))
