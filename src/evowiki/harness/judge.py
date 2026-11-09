"""The harness judge (design 2026-10-21 §4.1, §5.5): routed by the writer's provider family, blind,
both orders, with a quote check.

Routing (`judge_for`): the judge only has to come from another family than the writer of the texts
it judges, and it is the other family's cheap (fast) tier. Writer Anthropic (the deep tier's
Sonnet) → DeepSeek's fast model with thinking off; writer DeepSeek (the fallback, any of its
thinking modes) → the Anthropic fast tier (Haiku). Any other writer, or the needed judge not
configured (no key) → `JudgeUnavailable` with a plain reason; the round skips and shows it.
A same-family judge is never used.

Comparison (`compare`): the judge sees the learner's signals and the two texts as 「甲」/「乙」,
twice: (a, b) and (b, a). Each call names the better text (or "same") and copies one sentence of
it. A side wins only when both orders name it; a flip, or "same" in either order, is a tie. An
order whose quote is not in the text it names, or is also in the other text (a shared sentence is
no proof), counts as "same", so the comparison is a tie (both compared after collapsing runs of
whitespace and stripping; an empty quote never matches). Then the length rule (spec §5.5, as in
`evaluation.page`): when the candidate `a` won and is longer than LENGTH_RATIO × the reference
`b` (characters, stripped), it is a tie unless the learner asked for more (`asked_for_more`). A
longer reference that wins stays a reference win.
Content only: the judge never sees a replay's 【模拟】 lines (`content_of`), and two replays that did
different things (`ReplayResult.actions`) are not its to decide: what it says is kept, the comparison
is a tie with `actions_differ`, and whether the task got done is code's (`gate.layers`). It picked the
answer that wrote a quiz in the chat over one that called open_review, the very thing the learner
complained about (fault injection 2026-11-05).
A judge reply that does not parse raises (the gateway's LLMParseError / ProviderOutputError): an
error, never a silent tie. The caller decides what an error means for its round."""

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from evowiki.deep.llm import DeepLLM
from evowiki.harness.anchors import LENGTH_RATIO
from evowiki.harness.judge_prompt import (
    ASSISTANT_SYSTEM,
    JUDGE_PURPOSE,
    JUDGE_VERSION,
    PROMPT_VERSION_ASSISTANT,
    SYSTEM,
    assistant_judge_prompt,
    judge_prompt,
)
from evowiki.harness.replay import Case, ReplayResult
from evowiki.llm.gateway import Gateway, Provider
from evowiki.llm.models import GatewayConfig, LLMCall, LLMRequest, ModelRef
from evowiki.llm.providers import ENV_KEYS, ProviderTimeoutError
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import UsageControl
from evowiki.runtime.llm import DEEPSEEK_NOTHINK, runtime_providers
from evowiki.runtime.usage import runtime_tier_configs
from evowiki.teach.contracts import Signal
from evowiki.teach.localize import asked_for_more as _asked_for_more
from evowiki.teach.localize import quotes

Winner = Literal["a", "b", "tie"]
_Side = Literal["a", "b", "same"]
Kind = Literal["page", "assistant"]

FLIP_NOTE = "两次判断相反，算持平"
QUOTE_NOTE = "引用的句子不在它选的那段里，或另一段也有，算持平"
LONGER_NOTE = "更长但不一定更好，算持平"
ACTIONS_NOTE = "两份做的动作不同（{a} / {b}），任务完成与否由真实回归判，裁判只比内容，算持平"
_SIMULATED_LINE = re.compile(r"^【模拟(?:动作)?】.*(?:\n|$)", re.MULTILINE)


def content_of(text: str) -> str:
    """What the judge reads of a replay: its text without the lines saying a tool was called."""
    return _SIMULATED_LINE.sub("", text).strip()


def case_inputs(case: Case, signals: Sequence[Signal]) -> tuple[str, bool, Kind]:
    """What `compare` is told for `case`: (signals_text, asked_for_more, kind), from the case's own
    signals among `signals`. A deep case: the page prompt, its signals' quotes
    (`localize.quotes`) and whether they ask for more. An assistant case (harness 3b follow-up 3):
    the assistant prompt and the rejection texts, one per line; a rejection never asks for more
    (the length rule holds)."""
    sigs = [s for s in signals if s.signal_id in case.signal_ids]
    if case.kind == "assistant":
        return "\n".join(s.text.strip() for s in sigs if s.text.strip()), False, "assistant"
    qs = quotes(sigs, len(sigs)) if sigs else ()
    return "\n".join(qs), _asked_for_more(qs), "page"


class JudgeUnavailable(RuntimeError):
    """No compliant judge for this writer: the round skips with `reason` (plain Chinese)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def family(provider: str) -> str:
    """The provider family: every mode of one vendor ("deepseek", "deepseek-nothink"; "anthropic",
    "anthropic-think") is one family."""
    return next((f for f in ("deepseek", "anthropic") if provider.startswith(f)), provider)


@dataclass(frozen=True)
class JudgeConfigs:
    """The judges that can be used: None = not configured (its API key is missing)."""

    deepseek: ModelRef | None       # DeepSeek's fast model, thinking off; judges Anthropic writers
    anthropic: ModelRef | None      # the Anthropic fast tier (Haiku); judges DeepSeek writers


# Both families' fast models come from one config: the runtime fast tier as it is built with two
# providers (Anthropic mode: Haiku generates, DeepSeek's fast model verifies), whatever mode the
# app itself runs in. The DeepSeek judge runs on DEEPSEEK_NOTHINK, the provider the DeepSeek-mode
# fast tier uses (thinking off).
_TWO_FAMILIES = {"EVOWIKI_RUNTIME_PROVIDER": "anthropic"}
_ALL_PROVIDERS = {"EVOWIKI_RUNTIME_PROVIDER": "deepseek"}    # builds DEEPSEEK_NOTHINK too


def judge_configs(env: Mapping[str, str] = os.environ) -> JudgeConfigs:
    """The judges from the app's runtime fast tier (`runtime_tier_configs`, models and their env
    overrides as it reads them), each only when its family's API key is set. Only key presence
    is read."""
    fast = runtime_tier_configs({**env, **_TWO_FAMILIES})["fast"]
    ds = (ModelRef(provider=DEEPSEEK_NOTHINK, model=fast.verifier.model)
          if env.get(ENV_KEYS["deepseek"]) else None)
    an = fast.generator if env.get(ENV_KEYS["anthropic"]) else None
    return JudgeConfigs(deepseek=ds, anthropic=an)


def judge_providers(env: Mapping[str, str] = os.environ) -> dict[str, Provider]:
    """The providers a judge gateway needs (`runtime_providers` with every provider built:
    Anthropic at the runtime effort, DEEPSEEK_NOTHINK with thinking off and the fast tier's
    timeout). Providers whose key is missing are left out, as runtime_providers does."""
    return runtime_providers({**env, **_ALL_PROVIDERS})


def judge_for(writer_provider: str, configs: JudgeConfigs) -> ModelRef:
    """The judge for texts written by `writer_provider` (module doc)."""
    fam = family(writer_provider)
    if fam == "anthropic":
        ref, name = configs.deepseek, "DeepSeek"
    elif fam == "deepseek":
        ref, name = configs.anthropic, "Anthropic"
    else:
        raise JudgeUnavailable(f"写作模型来自 {writer_provider}，没有对应的评审模型")
    if ref is None:
        raise JudgeUnavailable(f"没有可用的 {name} 评审模型（缺少 {name} 的 API key），这一轮跳过")
    if family(ref.provider) == fam:
        raise JudgeUnavailable("评审模型和写作模型来自同一家，这一轮跳过")
    return ref


def judge_gateway(llm: DeepLLM, judge: ModelRef, *, providers: Mapping[str, Provider],
                  record_max_attempts: int = 1) -> Gateway:
    """A gateway whose every role is `judge`, over `providers` (`judge_providers()`) and the deep
    tier's logs, prices and user meter (like `DeepLLM`'s own gateways). Its usage control has no
    tiers, so an account never routes the call to another model. The judge's provider missing →
    JudgeUnavailable. The improver's own model is built the same way (`round._Round.improver_gw`),
    with the gateway's usual retries."""
    if judge.provider not in providers:
        raise JudgeUnavailable(f"评审模型 {judge.provider} 没有配置，这一轮跳过")
    kw = {"clock": llm.clock} if llm.clock is not None else {}
    return Gateway(GatewayConfig(generator=judge, verifier=judge, allow_single_provider=True),
                   llm.log, mode="record", providers=providers, attempts=llm.attempts,
                   record_max_attempts=record_max_attempts, usage=UsageControl(llm.meter, llm.prices),
                   requests=llm.requests, **kw)


class JudgeOut(BaseModel):
    better: Literal["甲", "乙", "same"]
    quote: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Comparison:
    """One blind both-order comparison. `a_id` / `b_id`: the replay run ids of the texts.
    `quote`: the sentence the judge copied from the winner (empty on most ties). `flipped`: the two
    orders named opposite texts. `call_ids`: the judge's llm_call ids, (a first, b first).
    `sides`: what it named in each order before any rule ("a" / "b" / "same"), which a canary
    reads: on identical or reordered texts the quote rule alone ties everything."""

    cmp_id: str
    case_id: str
    a_id: str
    b_id: str
    winner: Winner
    reason: str
    quote: str
    flipped: bool
    call_ids: tuple[str, str]
    sides: tuple[_Side, _Side] = ("same", "same")
    actions_differ: bool = False


def _norm(s: str) -> str:
    return " ".join(s.split())


def _quoted(quote: str, text: str, other: str) -> bool:
    """`quote` is in `text` and not in `other` (both normalised; empty never matches)."""
    q = _norm(quote)
    return bool(q) and q in _norm(text) and q not in _norm(other)


_FIRST: dict[str, _Side] = {"甲": "a", "乙": "b"}     # the (a, b) order
_SECOND: dict[str, _Side] = {"甲": "b", "乙": "a"}    # the (b, a) order


def _ask(case: Case, first: str, second: str, order: str, signals_text: str, gw: Gateway,
         account: Account, kind: Kind = "page") -> tuple[JudgeOut, LLMCall]:
    if kind == "assistant":
        version, system = PROMPT_VERSION_ASSISTANT, ASSISTANT_SYSTEM
        prompt = assistant_judge_prompt(first, second, signals=signals_text)
    else:
        version, system = JUDGE_VERSION, SYSTEM
        prompt = judge_prompt(first, second, signals=signals_text, anchor=case.anchor)
    req = LLMRequest(purpose=JUDGE_PURPOSE, prompt_version=version,
                     subject=f"{case.user_id}/{case.case_id}/{order}", system=system, prompt=prompt)
    try:
        return gw.call("verifier", req, JudgeOut, account=account)
    except ProviderTimeoutError:      # one call in a round's hundred must not void the round's spend
        return gw.call("verifier", req, JudgeOut, account=account)


def compare(case: Case, a: ReplayResult, b: ReplayResult, *, signals_text: str,
            asked_for_more: bool, gw: Gateway, account: Account,
            kind: Kind = "page") -> Comparison:
    """Judge `a` against `b` on `case` (module doc). Callers pass the candidate (or composed) text
    as `a` and the current pack's reference as `b`: a loss for the candidate is winner "b".

    `gw`: a gateway whose verifier is the judge for the texts' writer (`judge_gateway(llm,
    judge_for(...))`); `account`: whose usage the two calls are charged to. Both texts must be of
    `case` and have the same `writer_provider` (one judge has to be compliant for both), else
    ValueError. A judge from the writer's family raises JudgeUnavailable, before any call when the
    gateway's verifier shows it, otherwise once the first call reports its provider (before the
    second call). The same request served again from the call log (same texts, case and order)
    gives the same verdict and the same `cmp_id`: callers dedupe comparisons by `cmp_id`, and
    re-judging an identical pair never draws a new judge sample.

    `kind`: "page" (default) judges two versions of a page section with the page prompt;
    "assistant" judges two assistant answers with the assistant prompt. Everything else (both
    orders, flip = tie, the quote check, the length rule) is the same for both."""
    if a.case_id != case.case_id or b.case_id != case.case_id:
        raise ValueError(f"texts of case {a.case_id!r} / {b.case_id!r}, "
                         f"compared on case {case.case_id!r}")
    if a.writer_provider != b.writer_provider:
        raise ValueError(f"texts of different writers ({a.writer_provider} / {b.writer_provider}) "
                         "cannot share one judge")
    writer = family(a.writer_provider)
    if family(gw.config.verifier.provider) == writer:
        raise JudgeUnavailable("评审模型和写作模型来自同一家，这一轮跳过")
    ta, tb = content_of(a.text), content_of(b.text)
    j1, c1 = _ask(case, ta, tb, "ab", signals_text, gw, account, kind)
    if family(c1.provider) == writer:
        raise JudgeUnavailable("评审模型和写作模型来自同一家，这一轮跳过")
    j2, c2 = _ask(case, tb, ta, "ba", signals_text, gw, account, kind)
    if family(c2.provider) == writer:
        raise JudgeUnavailable("评审模型和写作模型来自同一家，这一轮跳过")
    s1, s2 = _FIRST.get(j1.better, "same"), _SECOND.get(j2.better, "same")
    text = {"a": ta, "b": tb}
    other = {"a": tb, "b": ta}
    unquoted = any(s != "same" and not _quoted(j.quote, text[s], other[s])
                   for s, j in ((s1, j1), (s2, j2)))
    flipped = {s1, s2} == {"a", "b"}
    winner: Winner = "tie"
    if s1 == s2 and s1 != "same" and not unquoted:
        winner = s1
    notes = []
    if flipped:
        notes.append(FLIP_NOTE)
    if unquoted:
        notes.append(QUOTE_NOTE)
    if (winner == "a" and not asked_for_more
            and len(ta) > LENGTH_RATIO * len(tb)):
        winner = "tie"
        notes.append(LONGER_NOTE)
    differ = set(a.actions) != set(b.actions)
    if differ:
        winner = "tie"
        notes.append(ACTIONS_NOTE.format(a="、".join(a.actions) or "没有动作", b="、".join(b.actions) or "没有动作"))
    quote = j1.quote.strip() if winner != "tie" else ""      # a win: both orders quoted it
    reason = "；".join(notes + [f"甲乙顺序：{j1.reason[:200]}", f"乙甲顺序：{j2.reason[:200]}"])
    key = f"{case.case_id}\n{a.run_id}\n{b.run_id}\n{c1.id}\n{c2.id}"
    return Comparison(cmp_id="cmp:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20],
                      case_id=case.case_id, a_id=a.run_id, b_id=b.run_id, winner=winner,
                      reason=reason, quote=quote, flipped=flipped, call_ids=(c1.id, c2.id),
                      sides=(s1, s2), actions_differ=differ)
