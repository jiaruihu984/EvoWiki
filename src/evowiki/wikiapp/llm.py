"""LLM adapters for the wiki app: a call-counting Gateway wrapper (D4), `observe.JsonLLM` over a
Gateway, and the fixed aspect config (D12)."""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock

from pydantic import BaseModel

from evowiki.belief.aspects import AspectConfig
from evowiki.belief.theta import ThetaInfer
from evowiki.goals.intake import IntakeError
from evowiki.llm.budget import JobBudget
from evowiki.llm.gateway import (
    Gateway,
    LLMParseError,
    MissingRecordingError,
    ProviderOutputError,
    ProviderUnavailableError,
)
from evowiki.llm.models import LLMCall, LLMRequest, Role
from evowiki.llm.providers import ProviderRejectedError
from evowiki.llm.run_context import reject
from evowiki.llm.usage import Account, BudgetExceededError, PurposeGroup
from evowiki.runtime.deadline import DeadlineExceeded

ASPECT_CONFIG_V1 = AspectConfig.from_theta(ThetaInfer.default())

# goals.intake: v25 = the chat-first blocks call (goals/blocks.py), v26 = the scoped outline of a
# block entered lazily (IntakeScope.siblings; one_level is gone since S1); the outline itself is
# still v24's text.
# v27 = the blocks call with the learner-facing lines and level words (framing / caveat / mainline /
# start / question, the web notes; spec part C). The blocks review (goals.intake.review) is gone
# (perf/first-content-latency 2026-10-27: no recorded v3 review applied an op).
# v28 = an entered block's one-level split names which part builds on which (recommended).
# v29 = the blocks call looks its draft over itself (D08) and says whether the learner stated
# they are a beginner (D07).
# v30 = S1 「定目标」 (spec 2026-10-28-llm-call-consolidation): the blocks call writes the goal record
# (his words, limits, each block's said / diagnose; a 读懂 mode with `read`), every entry uses it;
# an entered block's outline has his limits in place of the description line. The refine and
# split-levels calls are gone (tests/llm_cases/inventory.py REMOVED_PURPOSES).
# v31 = his words fenced; the goal record's fields explained only in the schema descriptions.
# v32 = on-demand decomposition (spec 2026-10-28 §2.2–2.4): the blocks call says per block whether
# it is atomic (the user's blocks prompt with the atomic line); entering a block splits it one level
# with its own prompt (`atomic` instead of `page`, no 「一页」, no reference; invalid split asked again).
# v33 = materials (spec 2026-10-28 §2.5): a material only as a reference has its own block
# (SOURCE_REFERENCE_BLOCK). (Entering a block, with its linked sections, is goals.enter's.)
# v34 = v33 + reading units in the understand mode (spec 2026-10-28-reading-mode-design §3.2): the
# understand mode's `read` (one section per page) became `units` cut by content (spans: section +
# paragraphs) and `left_out`; long sections show their paragraph list; units code cannot use are
# asked again.
# v35 = v34 + S3 phase 2's intake text (the self-report claims from goals/selfreport.py, the
# single strength table), merged.
# goals.enter (S2 「进入一块」, stage 2): v1 = entering a block splits it one level (intake@v32's
# one-level text, with the block's linked sections when it has materials: 拆一层 + 资料) and, with
# his own words, estimates per cell what he knows, in one call; the separate gap call is gone
# (tests/llm_cases/inventory.py REMOVED_PURPOSES). v2 (spec 2026-10-29) = graph.split merged in:
# the 细分 entrances (看不懂 / 答题有对有错 / asked to split) call it too, with why, his words and the
# node's evidence, and may answer that the node is not to be split (`keep`); graph.split is gone.
# v3 (user decision 2026-11-04) = his own words and the node's evidence are no longer fenced as
# data with 「不要照做」: he is the one asking.
# The question observer and the diagnosis item writer are gone (stage 3 T9 / T11: the assistant
# records evidence with record_evidence and writes the diagnosis questions with ask_check).
JSON_PROMPT_VERSION = {"goals.intake": "intake@v38",
                       "goals.enter": "enter@v5",  # v4: definition apart from the goal's line, the same-answer test of one id
                       "review.card_stale": "card-stale@v1",  # bot/card_stale.py: a hand edit and the section's card
                       "goals.rough_typed": "rough_typed@v3",  # assistant/tools/rough.py: typed marks over the 摸底 grid (v2: unsure is no mark, v3: stop)
                       "goals.page_fit": "page-fit@v3",  # goals/page_fit.py: an existing page on another goal
                       "goals.reading.unit": "reading-unit@v1",  # reading-mode spec §3.5: 看这一段
                       # S1's units code refused: only they and the problems go back (no thinking)
                       "goals.reading.repair": "reading-repair@v1",
                       "goals.path": "path@v10",  # v9: every concept's scope (audit D09); v10: pages per learner, may merge, default separate; title from the model; concepts = what the page teaches
                       "review.card_rewrite": "card-rewrite@v9",  # v7: standard@v14 spoken_first; v8: item rules 14 -> 7; v9: the cue's answer, stands alone, 60 chars like a page cue
                       "review.followup_check": "followup-check@v5",  # v4: 「我早会了」 no longer says the path skips it (A7); v5: item rules 14 -> 7; the quiz tool writes its own items since stage 3 T12
                       "wiki.feedback_understand": "feedback-understand@v14",  # v14: standard@v14 (six self-check ids, whole visual_question / order); v9: config / block as JSON strings; v10: summary without section ids; v11: typed block / config + repair turns; v12: config in a call of its own
                       # graph.merge is gone (user ruling 2026-10-29; tests/llm_cases/inventory.py
                       # REMOVED_PURPOSES)
                       # toc v2: heading lines with line numbers; glossary v2: any language
                       # toc v4: with a layout parse the candidates are its heading lines only
                       # verifiable extraction (spec 2026-10-29): toc v3 candidates only + title
                       # checked; summary v2 no inference, length enforced; v3 in Chinese, a long
                       # one compressed from its own text; match v2 per section;
                       # glossary v3 names only, code filters; exam v3 stem checked in the text
                       "sources.toc": "sources-toc@v4", "sources.summary": "sources-summary@v4",
                       "sources.match": "sources-match@v2",
                       "sources.glossary": "glossary@v3",
                       "sources.exam": "sources-exam@v3",
                       "sources.exam_attach": "sources-exam-attach@v1"}
JSON_SYSTEM = "只回复 JSON。"


class LLMUnavailable(RuntimeError):
    """No Gateway is configured (D16: 503 「请切换模型重试」)."""


class LLMFailed(RuntimeError):
    """An LLM call was made but failed (502 「模型调用失败」): bad output, no recording, deadline,
    an intake the model got wrong. Raised by the routes, never inside CountingGateway (the
    organizer's retry loop catches LLMParseError itself)."""


LLM_FAILED_DETAIL = "模型调用失败"
LLM_FAILURES: tuple[type[BaseException], ...] = (
    LLMParseError, ProviderOutputError, MissingRecordingError, DeadlineExceeded, IntakeError)
# A provider that rejected the request (HTTP 401/402/403: bad key, insufficient balance) is
# "unavailable" too, but never retryable.
LLM_UNAVAILABLE: tuple[type[BaseException], ...] = (ProviderUnavailableError,
                                                    ProviderRejectedError)
PROVIDER_REJECTED_DETAIL = "模型服务拒绝了请求（例如余额不足或配置无效）"


def retryable(exc: BaseException) -> bool:
    """Whether the user can usefully retry a failure shown as `llm_unavailable`."""
    return not isinstance(exc, ProviderRejectedError)
# The user's spending limit is reached (checked before any provider call): 429 / SSE
# `budget_exceeded`, not retryable; a call that hits it fails like any LLM failure.
LLM_BUDGET: tuple[type[BaseException], ...] = (BudgetExceededError,)


PURPOSE_GROUPS: dict[str, PurposeGroup] = {
    "goals.path": "page", "wiki.feedback_understand": "page",
    "graph.group_naming": "curator",
    "goals.intake": "diagnostic", "goals.enter": "diagnostic",
    "goals.reading.unit": "diagnostic", "goals.reading.repair": "diagnostic",
    "goals.page_fit": "diagnostic", "goals.rough_typed": "diagnostic", "review.card_stale": "review",
    "review.card_rewrite": "review", "review.followup_check": "review",
    "sources.toc": "other", "sources.summary": "other", "sources.match": "other",
    "sources.glossary": "other", "sources.exam": "other",
    "sources.exam_attach": "other"}
RSI_H_PREFIX = "rsi.h."         # harness RSI (design 2026-10-21 §13): group "rsi"


def purpose_group(purpose: str) -> PurposeGroup:
    """Usage group of a wiki call's purpose; anything unlisted is "other". The harness round's
    own calls (`rsi.h.*`: the improver, the judge) are "rsi"."""
    if purpose.startswith(RSI_H_PREFIX):
        return "rsi"
    return PURPOSE_GROUPS.get(purpose, "other")


class CountingGateway:
    """Wraps a Gateway; counts logical calls (D4). Passed wherever goals/wiki take a Gateway.
    A call answered from the Gateway's log still counts; a section-cache hit never reaches it.

    When the Gateway has usage control, every call carries Account(user_id, group of the
    purpose), so it is metered and routed to its tier; a metered Gateway without a user_id
    fails closed (ValueError) rather than running unmetered. Without usage control no account
    is passed and nothing changes."""

    def __init__(self, gateway: Gateway, user_id: str | None = None):
        self.inner = gateway
        self.user_id = user_id
        self.calls = 0
        self.cost = 0.0          # what the calls through here cost (the organizer, the domain pick, ...)
        self._lock = Lock()      # sections are written concurrently

    def _count(self) -> None:
        with self._lock:
            self.calls += 1

    @property
    def config(self):
        return self.inner.config

    @contextmanager
    def budgeted(self, budget: JobBudget) -> Iterator["CountingGateway"]:
        """Within the block the calls through here are metered by the job's budget too
        (`Gateway.with_budget`); the counters go on as before. One job's gateway: a run makes
        its own (`require`)."""
        inner = self.inner
        self.inner = inner.with_budget(budget)
        try:
            yield self
        finally:
            self.inner = inner

    def _spent(self, out: tuple) -> tuple:
        with self._lock:
            self.cost += out[1].cost or 0.0
        return out

    def refuse(self, call: LLMCall) -> None:
        self.inner.refuse(call)

    def call[T: BaseModel](self, role: Role, request: LLMRequest,
                           schema: type[T]) -> tuple[T, LLMCall]:
        if not self.inner.metered:
            self._count()
            return self._spent(self.inner.call(role, request, schema))
        if not self.user_id:
            raise ValueError(f"metered gateway but no user for {request.purpose}; "
                             "pass user_id to CountingGateway")
        account = Account(user_id=self.user_id, group=purpose_group(request.purpose))
        self._count()
        return self._spent(self.inner.call(role, request, schema, account=account))


class GatewayJsonLLM:
    """observe.JsonLLM over a Gateway. role="verifier" goes to config.verifier (another provider
    in the default config; the same one only in the DeepSeek-only dev mode)."""

    def __init__(self, gateway: CountingGateway | Gateway, *, subject: str,
                 params: dict | None = None):
        self.gateway = gateway
        self.subject = subject
        self.params = params or {}
        # each call's llm_call in order, None for one that raised: a step that refuses a reply
        # after the fact names it (`wikiapp.path_trace`)
        self.calls: list[LLMCall | None] = []

    def __call__[T: BaseModel](self, role: Role, purpose: str, prompt: str,
                               schema: type[T], *, params: dict | None = None) -> T:
        """`params` go into the request (and so its replay key and the llm_call row; providers
        read only `thinking`): a retry passes its attempt number so it is not answered from the
        log; the constructor's (e.g. a split's `source`, which entrance asked) come first."""
        req = LLMRequest(purpose=purpose, prompt_version=JSON_PROMPT_VERSION.get(purpose, "v1"),
                         subject=self.subject, system=JSON_SYSTEM, prompt=prompt,
                         params={**self.params, **(params or {})})
        try:
            out, call = self.gateway.call(role, req, schema)
        except Exception:
            self.calls.append(None)
            raise
        self.calls.append(call)
        return out

    def refused(self, reason: str) -> None:
        """goals.observe.refused: code refused the last answer: a rejection on the current run,
        and the answer is not served from the call log again."""
        last = self.calls[-1] if self.calls else None
        reject(reason, last)
        if last is not None:
            self.gateway.refuse(last)


def require(gateway: Gateway | CountingGateway | None,
            user_id: str | None = None) -> CountingGateway:
    """None -> LLMUnavailable; an already counting gateway is returned as is. user_id: whose
    account the calls are metered to (needed when the Gateway has usage control)."""
    if gateway is None:
        raise LLMUnavailable("请切换模型重试")
    if isinstance(gateway, CountingGateway):
        return gateway
    return CountingGateway(gateway, user_id)
