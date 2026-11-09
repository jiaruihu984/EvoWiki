import copy
import dataclasses
import hashlib
import json
import logging
import random
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from evowiki.harness.pack import EMPTY, HarnessPack
from evowiki.harness.source import HarnessSource
from evowiki.llm import budget, run_context
from evowiki.llm.chat import ChatRequest, ChatResponse, ChatTurn, StreamSink, chat_input_hash
from evowiki.llm.deadline import (
    POLL_S,
    RequestCancelled,
    cancel_requested,
    cancel_scope,
    cancellable_sleep,
)
from evowiki.llm.log import AttemptStore, CallStore, RequestLog
from evowiki.llm.models import (
    THINKING_PARAM,
    AttemptStatus,
    GatewayConfig,
    LLMAttempt,
    LLMCall,
    LLMRequest,
    ModelRef,
    ProviderResponse,
    Role,
)
from evowiki.llm.prompt_steps import MARK_OPEN, apply_marks
from evowiki.llm.usage import Account, Usage
from evowiki.llm.usage_control import UsageControl

log = logging.getLogger(__name__)

Mode = Literal["replay", "record"]
# user id -> {step key: that user's guidance text} (llm/prompt_steps.py); built by the service
Overrides = Callable[[str], Mapping[str, str]]
USER_VERSION = "+user"     # appended to prompt_version when a user's guidance replaced a default


def _reasoned[T: LLMRequest | ChatRequest](request: T, ref: ModelRef) -> T:
    effort = ref.reasoning_effort
    if effort is None:
        return request
    if effort == "default":
        params = {k: v for k, v in request.params.items() if k not in (THINKING_PARAM, "reasoning_effort")}
        return request.model_copy(update={"params": params})
    params = {**request.params, "reasoning_effort": effort}
    if params.get(THINKING_PARAM) is False:
        from evowiki.llm.catalog import reasoning_levels

        levels = reasoning_levels(ref.provider, ref.model)
        # Explicit compression/extraction constraints use this model's valid minimum.
        effort = "none" if "none" in levels else levels[0]
        params["reasoning_effort"] = effort
        params[THINKING_PARAM] = effort not in ("none", "default")
    if ref.provider == "kimi" and ref.model.startswith("kimi-k3"):
        params.pop("temperature", None)  # fixed by the API, including compression requests
    params.setdefault(THINKING_PARAM, effort != "none")
    return request.model_copy(update={"params": params})

RECORD_MAX_ATTEMPTS = 3  # provider calls per input in record mode (incl. the first)
RETRYABLE_STATUSES: frozenset[AttemptStatus] = frozenset(
    {"parse_error", "truncated", "empty_output"})
# HTTP 429 (`Gateway._past_rate_limit`), as the official SDKs do it (max_retries there is 0 so that every
# attempt is audited here and nothing is resent after streaming): Retry-After when it is short, else
# exponential backoff with jitter. A longer Retry-After fails at once: he should not sit through it.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_S = 1.0
RATE_LIMIT_WAIT_MAX_S = 30.0
# A structured reply cut at max_tokens or empty: with a thinking model, the hidden reasoning ate
# the output budget. Retried once with thinking off, never as the same request (`_thinking_off`).
STARVED_STATUSES: frozenset[AttemptStatus] = frozenset({"truncated", "empty_output"})

REDACTED = "[client context redacted]"

# Purposes whose input is public and deterministic, so a reply recorded for one user may serve
# another from the call log. Every other call with a user is keyed by that user too (`_scoped`).
# Empty on purpose: every product call with an account carries that user's context (at least the
# learner-profile block, `_with_profile`; most also their pages, goals, notes or materials), and
# two users whose profiles happen to be empty must not share a reply either. A call without a user
# (offline tools, frozen experiments) is keyed as before.
SHARED_PURPOSES: frozenset[str] = frozenset()

def _workflow_durable() -> Any:
    """The `evowiki.agent.durable` module when a durable workflow is running, else None.

    Deliberately no module-level import: the frozen experiment harnesses import this gateway and
    their import graph is pinned by freeze tests, so `evowiki.agent` must not join it. A running
    workflow implies its body already imported `durable`, so looking it up in `sys.modules` is
    enough: absent module means "not in a workflow"."""
    mod = sys.modules.get("evowiki.agent.durable")
    return mod if mod is not None and mod.in_workflow() else None


# Costs charged by the metered path of the durable step that is running (None outside one).
_charged: ContextVar[list[float] | None] = ContextVar("gateway_charged", default=None)


def _needles(needles: Iterable[str]) -> tuple[str, ...]:
    """Distinct non-empty needles, longest first (so a needle inside another never splits it)."""
    return tuple(sorted({n for n in needles if n}, key=len, reverse=True))


def redact_text(s: str, needles: Iterable[str]) -> str:
    """Replace every literal occurrence of a non-empty needle in s by REDACTED, longest first.

    Plain-string contract: no escaped forms are matched (the gateway adds JSON/repr-escaped
    forms itself for persisted raw provider output and error messages)."""
    for n in _needles(needles):
        s = s.replace(n, REDACTED)
    return s


def _persisted_needles(needles: tuple[str, ...]) -> tuple[str, ...]:
    """Needles for PERSISTED copies: each literal plus the escaped forms under which it can
    appear in raw provider output (JSON, ASCII or not) or in an exception message (repr)."""
    forms = set()
    for n in needles:
        forms.update((n, json.dumps(n)[1:-1], json.dumps(n, ensure_ascii=False)[1:-1],
                      repr(n)[1:-1]))
    return _needles(forms)


def _redact_json(obj: Any, needles: tuple[str, ...]) -> Any:
    """Redact str leaves of a JSON value; dict keys and structure are kept."""
    if isinstance(obj, str):
        return redact_text(obj, needles)
    if isinstance(obj, dict):
        return {k: _redact_json(v, needles) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_json(v, needles) for v in obj]
    return obj


class MissingRecordingError(LookupError):
    pass


class ProviderUnavailableError(RuntimeError):
    pass


class LLMParseError(ValueError):
    """A provider's reply did not fit the schema. `cost`: what the unparseable replies of this
    call cost (None: not known, e.g. a replayed recording); a caller that repairs and asks again
    adds it to its own total."""

    cost: float | None = None


class ProviderOutputError(RuntimeError):
    """A provider answered but gave no usable output. Carries what is known about the
    (possibly paid) response so the attempt audit can record it."""

    default_status: AttemptStatus = "empty_output"
    rate_limited = False               # HTTP 429: worth waiting for (`Gateway._past_rate_limit`)
    retry_after: float | None = None   # its Retry-After header, in seconds

    def __init__(self, message: str = "", *, raw_output: str | None = None,
                 model_version: str | None = None, cost: float | None = None,
                 status: AttemptStatus | None = None, input_tokens: int | None = None,
                 output_tokens: int | None = None, cache_read_tokens: int | None = None,
                 cache_write_tokens: int | None = None,
                 cache_write_1h_tokens: int | None = None):
        super().__init__(message)
        self.raw_output = raw_output
        self.model_version = model_version
        self.cost = cost
        self.status: AttemptStatus = status or self.default_status
        self.input_tokens = input_tokens    # paid failures are metered too (plan rev 3.2 §9.4)
        self.output_tokens = output_tokens
        self.cache_read_tokens = cache_read_tokens    # prompt-cache tokens of a paid failure
        self.cache_write_tokens = cache_write_tokens
        self.cache_write_1h_tokens = cache_write_1h_tokens   # the 1-hour part of the writes


class _Streamed:
    """The sink of one streamed attempt: passes text on and keeps what it passed."""

    def __init__(self, sink: StreamSink):
        self._sink = sink
        self._parts: list[str] = []

    @property
    def streamed(self) -> str:
        return "".join(self._parts)

    def text(self, chunk: str) -> None:
        self._parts.append(chunk)
        self._sink.text(chunk)


def _shown(rec: _Streamed | None) -> bool:
    """The attempt streamed text the learner may have seen."""
    return rec is not None and rec.streamed != ""


def _thinking_off(request: LLMRequest, ref: ModelRef | None = None) -> LLMRequest:
    """`request` with thinking off (DeepSeek: thinking disabled; Anthropic: effort low,
    providers.py), so the whole output budget goes to the answer. The structured retry of
    DeepTutor (HKUDS/DeepTutor, Apache-2.0, services/llm/structured_retry.py: ask the same model
    for the same thing with its thinking turned down)."""
    lowered = request.model_copy(update={"params": {**request.params, THINKING_PARAM: False}})
    return _reasoned(lowered, ref) if ref is not None else lowered


class Provider(Protocol):
    def complete(
        self, model: str, request: LLMRequest, json_schema: dict[str, Any]
    ) -> ProviderResponse: ...


class _MeteredProvider:
    """Wraps a provider for one metered call or turn: notes the usage of every response it
    sees, including paid failures (ProviderOutputError), so retries are billed too. A call
    that never got a response (network error) has nothing to bill. Wraps both `complete`
    (JSON calls) and `chat` (tool-calling turns; billed from the ChatResponse's reported
    tokens, None when unreported, never from the turn's zero-defaulted counts).

    A job meter with a limit (`llm.budget`) bounds every attempt: its max_tokens is what the job
    has left, and an attempt that cannot fit is refused before it is sent; a reply cut at that
    lowered cap is a budget stop (PageBudgetExceeded), not a truncation to retry."""

    def __init__(self, inner: Any, usage: UsageControl, provider_name: str,
                 account: Account | None = None):
        self._inner = inner
        self._usage = usage
        self._name = provider_name
        self._account = account
        self.seen: list[Usage] = []

    def _cap(self, model: str, text: str) -> budget.OutputCap | None:
        if self._account is None:
            return None
        pending = sum(u.cost_usd or 0.0 for u in self.seen)
        return budget.output_cap(self._usage.meter, self._account, self._usage.prices,
                                 self._name, model, text, pending_usd=pending)

    def _budget_stop(self, cap: budget.OutputCap | None, exc: ProviderOutputError) -> None:
        if cap is not None and cap.bound and exc.status == "truncated":
            assert self._account is not None
            raise budget.PageBudgetExceeded(self._account,
                                            self._usage.meter.limit_usd) from exc

    def _paid_failure(self, model: str, exc: ProviderOutputError,
                      started_at: datetime | None) -> None:
        u = self._usage.usage_of(self._name, model, exc.model_version,
                                 exc.input_tokens, exc.output_tokens,
                                 exc.cache_read_tokens, exc.cache_write_tokens,
                                 exc.cache_write_1h_tokens, started_at=started_at,
                                 ended_at=self._usage.now())
        self.seen.append(u)
        if u.cost_usd is not None:
            exc.cost = u.cost_usd       # the attempt row carries the priced cost too

    def _cancelled(self, model: str, exc: RequestCancelled, started_at: datetime | None) -> None:
        """A request cut by 「停止」: what the stream had reported (if anything) is still billed."""
        if not exc.reported:
            return
        self.seen.append(self._usage.usage_of(
            self._name, model, exc.model_version, exc.input_tokens, exc.output_tokens,
            exc.cache_read_tokens, exc.cache_write_tokens, exc.cache_write_1h_tokens,
            started_at=started_at, ended_at=self._usage.now()))

    def _note(self, model: str, resp: Any, started_at: datetime | None) -> Any:
        """Record the response's usage and stamp its cost: the one priced by the usage control
        (the same figure the usage meter records; the off-peak rate when the call lay wholly in
        the window) wins; a model with no price keeps whatever the provider reported (None for
        DeepSeek: never a fake 0)."""
        u = self._usage.usage_of(self._name, model, resp.model_version,
                                 resp.input_tokens, resp.output_tokens,
                                 resp.cache_read_tokens, resp.cache_write_tokens,
                                 resp.cache_write_1h_tokens, started_at=started_at,
                                 ended_at=self._usage.now())
        self.seen.append(u)
        return resp if u.cost_usd is None else resp.model_copy(update={"cost": u.cost_usd})

    def complete(self, model: str, request: LLMRequest,
                 json_schema: dict[str, Any]) -> ProviderResponse:
        cap = self._cap(model, budget.request_text(request, json_schema))
        t0 = self._usage.now()
        try:
            with budget.capped(cap):
                resp = self._inner.complete(model, request, json_schema)
        except RequestCancelled as exc:
            self._cancelled(model, exc, t0)
            raise
        except ProviderOutputError as exc:
            self._paid_failure(model, exc, t0)
            self._budget_stop(cap, exc)
            raise
        return self._note(model, resp, t0)

    @property
    def streams(self) -> bool:
        return bool(getattr(self._inner, "streams", False))

    def chat(self, model: str, request: ChatRequest,
             stream: StreamSink | None = None) -> ChatResponse:
        cap = self._cap(model, budget.request_text(request))
        t0 = self._usage.now()
        try:
            with budget.capped(cap):
                resp = (self._inner.chat(model, request) if stream is None
                        else self._inner.chat(model, request, stream=stream))
        except RequestCancelled as exc:
            self._cancelled(model, exc, t0)
            raise
        except ProviderOutputError as exc:
            self._paid_failure(model, exc, t0)
            self._budget_stop(cap, exc)
            raise
        return self._note(model, resp, t0)


def input_hash(ref: ModelRef, request: LLMRequest, schema_name: str) -> str:
    payload = {
        "provider": ref.provider,
        "model": ref.model,
        "purpose": request.purpose,
        "prompt_version": request.prompt_version,
        "subject": request.subject,
        "system": request.system,
        "prompt": request.prompt,
        "params": request.params,
        "schema": schema_name,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# Every guidance marker opens with it (llm/prompt_steps.MARK_OPEN): a cheap pre-check.
MARK = MARK_OPEN.split("{", 1)[0]


_STRAY = " \t\r\n}]"      # what may follow a complete object (`_parse`)


def _parse[T: BaseModel](schema: type[T], raw: str) -> T | None:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    try:
        return schema.model_validate_json(text)
    except ValidationError:
        pass
    # One narrow repair: a complete JSON object followed only by stray closing brackets — seen from
    # DeepSeek with thinking off (an object, then a lone "}" on the next line); it failed a
    # calibration case twice. Anything else still fails (no guessing at truncated or mixed output).
    try:
        obj, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    if not isinstance(obj, dict) or text[end:].strip(_STRAY):
        return None
    try:
        return schema.model_validate(obj)
    except ValidationError:
        return None


class _SingleFlight:
    """One provider call at a time per request hash, in this process. Two identical requests made
    at once (the same text twice in a concurrent split or extraction) both miss the log; without
    this both are paid for and the second append fails on the hash's unique row. The later one
    waits and is then served from the log, or asks the provider itself if the first one failed."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._flights: dict[str, list] = {}         # hash -> [its lock, who holds or waits for it]

    @contextmanager
    def one(self, key: str) -> Iterator[None]:
        with self._guard:
            flight = self._flights.setdefault(key, [threading.RLock(), 0])
            flight[1] += 1
        try:
            if cancel_requested():
                raise RequestCancelled("cancelled while waiting for an identical request")
            while not flight[0].acquire(timeout=POLL_S):
                if cancel_requested():
                    raise RequestCancelled("cancelled while waiting for an identical request")
            try:
                if cancel_requested():
                    raise RequestCancelled("cancelled while waiting for an identical request")
                yield
            finally:
                flight[0].release()
        finally:
            with self._guard:
                flight[1] -= 1
                if flight[1] == 0:
                    del self._flights[key]


_FLIGHTS = _SingleFlight()         # per process: gateways of one server share one call log


class Gateway:
    """Typed LLM boundary. replay: log only. record: provider on cache miss, then log
    (only outputs that parse are logged, so a failed call can be retried). In record mode
    every provider call, failed or not, is also appended to the optional attempt log.
    Record mode retries parse_error / truncated / empty_output outcomes up to
    record_max_attempts provider calls in total (default RECORD_MAX_ATTEMPTS); refusals and
    other errors are not retried. Two exceptions: a `call` reply that is truncated or empty is
    retried once with thinking off, then fails (`_record_attempts`); a `converse` attempt that
    streamed text is never retried (`converse`).

    request.redact (audit-only): a redacted request never reads the cache, is never replayed
    (MissingRecordingError in replay mode) and is stored as a non-replayable execution with its
    own id; only the PERSISTED call/attempt copies are redacted (every attempt of the retry
    loop, before truncation), the caller gets the live output.

    usage (plan rev 3.2 §9.4): with call(account=...) or converse(account=...), the tier picks
    the model, the budget is checked before any provider call, and every paid provider response
    (retries and paid failures included) or cache replay is recorded. Without an account nothing
    changes.

    requests (design 2026-10-21 §9.1): when a `call` makes a new llm_call, the system and prompt
    it sent (guidance applied, redacted like the persisted call) go to this log under the call's
    id, just before the call is appended; a cache replay records nothing (the input is already
    there).

    Guidance markers (llm/prompt_steps.py): before a request is hashed, logged or sent, every
    marked span in a `call` request's system and prompt, and in a `converse` request's system only,
    is replaced by the account's guidance for that step (the user's harness pack, `harness`, or
    the plain `overrides` mapping; one of them, consulted only with an account) and the markers there are removed; an applied override adds USER_VERSION to the
    request's prompt_version. With no override the text is the unmarked prompt, byte for byte.

    Learner profile (`profile`, evowiki.profile): a call or turn with an account gets the profile
    block for its purpose appended to the end of its system prompt (after the static text, so the
    cached prefix stays), before it is hashed, logged or sent. One place for every pipeline: no
    caller builds its own learner context. A block already in the system (a request `as_sent`
    built) is not added twice: the block's first line is the check."""

    def __init__(
        self,
        config: GatewayConfig,
        log: CallStore,
        *,
        mode: Mode = "replay",
        providers: Mapping[str, Provider] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        attempts: AttemptStore | None = None,
        record_max_attempts: int = RECORD_MAX_ATTEMPTS,
        usage: UsageControl | None = None,
        overrides: Overrides | None = None,
        harness: HarnessSource | None = None,
        requests: RequestLog | None = None,
        salt: str | None = None,
        profile: Callable[[str, str], str] | None = None,
        sleep: Callable[[float], None] = cancellable_sleep,
    ):
        if harness is not None and overrides is not None:
            raise ValueError("pass harness or overrides, not both")
        if (isinstance(record_max_attempts, bool) or not isinstance(record_max_attempts, int)
                or record_max_attempts < 1):
            raise ValueError(f"record_max_attempts must be an int >= 1, "
                             f"got {record_max_attempts!r}")
        self._config = config
        self._log = log
        self._attempts = attempts
        self._mode = mode
        self._providers = dict(providers or {})
        self._clock = clock
        self._record_max_attempts = record_max_attempts
        self._sleep = sleep
        self._usage = usage
        self.overrides = overrides
        self._harness = harness
        self._requests = requests
        self._salt = salt
        self._profile = profile

    @property
    def config(self) -> GatewayConfig:
        return self._config

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def metered(self) -> bool:
        """True when a usage control is configured; only then may callers pass an account."""
        return self._usage is not None

    def _guidance(self, account: Account | None,
                  pinned: Mapping[str, str] | None = None) -> Mapping[str, str]:
        """The account's guidance overrides (none without an account or a source). `pinned`: the
        guidance of the pack an agent run read once (`pack_for_run`), used instead of a live read
        of the harness source."""
        if account is None:
            return {}
        if self._harness is not None:
            return pinned if pinned is not None else self.harness_for(account.user_id).guidance()
        if self.overrides is None:
            return {}
        return self.overrides(account.user_id)

    def with_harness(self, source: HarnessSource) -> "Gateway":
        """A shallow copy whose packs come from `source` (a harness replay's candidate pack). It
        shares this gateway's call log, attempt log, request log, usage control, providers and
        clock, so its calls are recorded and charged like any other; this gateway is untouched.
        The copy reads no `overrides` (a pack source replaces them)."""
        out = copy.copy(self)
        out._harness = source
        out.overrides = None
        return out

    def salted(self, salt: str) -> "Gateway":
        """A shallow copy (shared logs, usage, providers, clock, packs) whose calls are keyed by
        `salt` too: nothing recorded without that salt is served from the call log, so a harness
        replay draws its own samples (the reference and every candidate alike) instead of the
        recorded run's. The salt enters only the input hash, never the request sent."""
        out = copy.copy(self)
        out._salt = salt
        return out

    def with_log(self, log: CallStore) -> "Gateway":
        """A shallow copy (shared attempt / request logs, usage, providers, clock, packs) that
        reads and writes the call log `log` instead (a harness round wraps the shared log to see
        which calls it made, `harness.round`)."""
        out = copy.copy(self)
        out._log = log
        return out

    def with_model(self, ref: ModelRef) -> "Gateway":
        """Pin both roles to one model, preserving providers, logs, prices and billing.
        Product tier and model-picker settings cannot override this copy's model."""
        out = copy.copy(self)
        out._config = GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True)
        if self._usage is not None:
            out._usage = dataclasses.replace(self._usage, tier_configs=None, model_configs=None,
                                             default_model=None)
        return out

    def with_budget(self, job: "budget.JobBudget") -> "Gateway":
        """A shallow copy (shared logs, providers, clock, packs) whose calls are also metered by
        the job's budget (`llm.budget.JobMeter` around this gateway's meter): one page job's
        planning and writing share one limit. Without usage control a limit cannot hold: a
        ValueError when `job` has one, else this gateway as is."""
        if self._usage is None:
            if job.limit_usd is not None:
                raise ValueError("a job limit needs a metered gateway")
            return self
        out = copy.copy(self)
        out._usage = dataclasses.replace(
            self._usage, meter=budget.JobMeter(self._usage.meter, budget=job))
        return out

    def _key(self, h: str) -> str:
        """The call-log key of input hash `h` (unchanged without a salt)."""
        if self._salt is None:
            return h
        return hashlib.sha256(f"{h}|salt:{self._salt}".encode()).hexdigest()

    @staticmethod
    def _scoped(h: str, purpose: str, user_id: str | None) -> str:
        """The call-log key of `h` for `user_id` (SHARED_PURPOSES): a private call is served only
        from that user's own recordings (unchanged without a user)."""
        if user_id is None or purpose in SHARED_PURPOSES:
            return h
        return hashlib.sha256(f"{h}|user:{user_id}".encode()).hexdigest()

    def _served(self, call: LLMCall, user_id: str | None, latency: float) -> LLMCall:
        """A call answered from the log, as a logical call of its own (ruling 2026-10-29): a
        non-replayable row for this user with `served_from` the recorded call, cost 0 and the
        lookup's latency, so call counts and lineage stay whole and nothing is billed twice. A call
        without a user (offline tools, frozen experiments) writes nothing and gets the recording."""
        if user_id is None:
            return call
        hit = call.model_copy(update={
            "id": f"{call.id}_hit{uuid.uuid4().hex[:12]}", "user_id": user_id,
            "served_from": call.id, "cost": 0.0, "latency": latency, "ts": self._clock(),
            "replayable": False, "run_id": run_context.current_run_id()})
        self._log.append(hit)
        return hit

    def harness_for(self, user_id: str | None) -> HarnessPack:
        """The user's harness pack (design 2026-10-21 §3), EMPTY without a source or a user."""
        if user_id is None or self._harness is None:
            return EMPTY
        return self._harness(user_id)

    def _with_profile(self, system: str, purpose: str, account: Account | None) -> str:
        """`system` with the account's profile block for `purpose` at its end (class doc)."""
        if account is None or self._profile is None:
            return system
        block = self._profile(account.user_id, purpose)
        if not block or block.partition("\n")[0] in system:   # as_sent added it already
            return system
        return f"{system}\n\n{block}" if system else block

    def _guided_request(self, request: LLMRequest, account: Account | None) -> LLMRequest:
        """`request` with its guidance markers applied and the profile appended (class doc).
        Both texts are code-built prompts: a JSON call's prompt holds the step's rules next to
        the call's data (the markers sit in the builder's literals; user text is only
        interpolated around them)."""
        system = self._with_profile(request.system, request.purpose, account)
        if system != request.system:
            request = request.model_copy(update={"system": system})
        if MARK not in request.system and MARK not in request.prompt:
            return request
        ov = self._guidance(account)
        system, a1 = apply_marks(request.system, ov)
        prompt, a2 = apply_marks(request.prompt, ov)
        version = request.prompt_version + (USER_VERSION if a1 or a2 else "")
        return request.model_copy(update={"system": system, "prompt": prompt,
                                          "prompt_version": version})

    def as_sent(self, request: ChatRequest, account: Account | None,
                guidance: Mapping[str, str] | None = None) -> ChatRequest:
        """The chat request exactly as `converse` would send it for `account` (guidance applied,
        markers removed, USER_VERSION on an applied override). `converse` of the result sends it
        unchanged (it has no markers left), so a caller that traces what was sent passes this.
        `guidance`: the run's pinned pack guidance (`HarnessPack.guidance()`), so every request of
        a run carries the text of the pack version the run recorded, not a later live read."""
        return self._guided_chat(request, account, guidance)

    def _guided_chat(self, request: ChatRequest, account: Account | None,
                     pinned: Mapping[str, str] | None = None) -> ChatRequest:
        """`request` with its guidance markers applied to the system only and the profile
        appended (class doc): messages carry user and tool text, which is sent exactly as
        written, markers or not."""
        system = self._with_profile(request.system, request.purpose, account)
        if system != request.system:
            request = request.model_copy(update={"system": system})
        if MARK not in request.system:
            return request
        system, applied = apply_marks(request.system, self._guidance(account, pinned))
        version = request.prompt_version + (USER_VERSION if applied else "")
        return request.model_copy(update={"system": system, "prompt_version": version})

    def route_provider(self, role: Role, account: Account | None = None,
                       purpose: str | None = None) -> str:
        """The provider a call with this role / account / purpose would go to (the account's
        tier when metered, as `_route`); without usage control the account is ignored."""
        return self.route_ref(role, account, purpose).provider

    def route_ref(self, role: Role, account: Account | None = None,
                  purpose: str | None = None) -> ModelRef:
        """The model ref (provider and model) `route_provider` routes to."""
        if self._usage is None or account is None:
            return self._config.ref(role)
        return self._usage.config_for(account, self._config, purpose).ref(role)

    def _route(self, role: Role, account: Account | None, purpose: str | None = None
               ) -> tuple[UsageControl | None, ModelRef]:
        """The usage control that applies (None without an account) and the model ref, routed
        through the account's tier when metered (per-purpose tier defaults apply)."""
        if account is not None and self._usage is None:
            raise ValueError("account given but this gateway has no usage control")
        usage = self._usage if account is not None else None
        config = self._config if usage is None else usage.config_for(account, self._config, purpose)
        return usage, config.ref(role)

    def _run_metered[R](self, usage: UsageControl | None, account: Account | None,
                        purpose: str, ref: ModelRef, provider: Any,
                        run: Callable[[Any], R], call_id: str) -> R:
        """Cache miss: check the budget before any provider call (BudgetExceededError: nothing
        is called), run the attempts through a metering wrapper, and record every paid response
        it saw (retries and paid failures included), also when the attempts raise. Each record
        names `call_id`, the llm_call the attempts produce; none when they raised (no call)."""
        if usage is None:
            return run(provider)
        assert account is not None
        usage.meter.check(account)
        metered = _MeteredProvider(provider, usage, ref.provider, account)
        made: str | None = None
        try:
            out = run(metered)
            made = call_id
            return out
        finally:
            for u in metered.seen:
                u = u.model_copy(update={"call_id": made})
                usage.meter.record(account, purpose, u, replayed=False)
                charged = _charged.get()
                if charged is not None and u.cost_usd:
                    charged.append(u.cost_usd)

    @staticmethod
    def _record_replay(usage: UsageControl | None, account: Account | None, purpose: str,
                       ref: ModelRef, call_id: str) -> None:
        """Served from the log: recorded with zero tokens (cache tokens too) as a replay, never
        billed; `call_id` is the logical call (`_served`)."""
        if usage is None:
            return
        assert account is not None
        usage.meter.record(account, purpose,
                           Usage(provider=ref.provider, model=ref.model, input_tokens=0,
                                 output_tokens=0, call_id=call_id), replayed=True)

    def _past_rate_limit(self, send: Callable[[], Any], ref: ModelRef, h: str,
                         request: LLMRequest | ChatRequest, *, rec: "_Streamed | None" = None,
                         persisted: tuple[str, ...] = ()) -> Any:
        """`send()`, again after a wait when the provider answers HTTP 429: a moment's limit, not a failure of
        this turn. Each 429 is an audited attempt; only a reply that went through reaches the call log, so a
        turn has one llm_call however many tries it took. Waiting holds the call's single-flight lock, which
        is right: whoever else wants this call wants the same answer. The per-request deadline starts anew
        with each send and a stopped run ends the wait. Never after text was shown (it would repeat)."""
        for retry in range(RATE_LIMIT_RETRIES + 1):
            start = time.perf_counter()
            try:
                return send()
            except ProviderOutputError as exc:
                wait = self._rate_limit_wait(exc, retry)
                if wait is None or _shown(rec):
                    raise
                self._audit(ref, h, request, "provider_error", start=start, error=exc, persisted=persisted)
                log.info("%s:%s HTTP 429 for %s, retry %d in %.1f s", ref.provider, ref.model,
                         request.purpose, retry + 1, wait)
                self._sleep(wait)
        raise AssertionError("unreachable: the last retry returns or raises")

    @staticmethod
    def _rate_limit_wait(exc: "ProviderOutputError", retry: int) -> float | None:
        """Seconds before the next try; None: give up."""
        if not exc.rate_limited or retry >= RATE_LIMIT_RETRIES:
            return None
        if exc.retry_after is not None:
            return exc.retry_after if 0 <= exc.retry_after <= RATE_LIMIT_WAIT_MAX_S else None
        backoff = RATE_LIMIT_BACKOFF_S * 2 ** retry
        return backoff * (1 - random.random() / 4)

    def _audit(self, ref: ModelRef, h: str, request: LLMRequest | ChatRequest,
               status: AttemptStatus, *, start: float, raw_output: str | None = None,
               model_version: str | None = None, cost: float | None = None,
               error: BaseException | None = None, persisted: tuple[str, ...] = ()) -> None:
        """Append one provider attempt to the attempt log (if any). persisted: escaped
        redaction needles applied to raw_output and the error message before truncation."""
        if self._attempts is None:
            return
        message = str(error) if error is not None else None
        if persisted:
            raw_output = redact_text(raw_output, persisted) if raw_output is not None else None
            message = redact_text(message, persisted) if message is not None else None
        self._attempts.append(LLMAttempt(
            id=f"att_{h[:16]}_{uuid.uuid4().hex[:8]}",
            input_hash=h,
            purpose=request.purpose,
            prompt_version=request.prompt_version,
            subject=request.subject,
            provider=ref.provider,
            model=ref.model,
            model_version=model_version,
            params=request.params,
            raw_output=raw_output,
            status=status,
            error_type=type(error).__name__ if error is not None else None,
            error_message=message[:500] if message is not None else None,
            cost=cost,
            latency=time.perf_counter() - start,
            ts=self._clock(),
        ))

    def call[T: BaseModel](
        self, role: Role, request: LLMRequest, schema: type[T], *,
        account: Account | None = None,
    ) -> tuple[T, LLMCall]:
        """Inside a traced run (llm/run_context.py) the call, or its failure, is appended to the
        run as a step; outside the durable step, so a re-executed workflow body appends it again
        (a no-op: one step per run and call id). A call with an account outside any run gets a
        run of its own (`run_context.auto_run`)."""
        user = account.user_id if account is not None else None
        with run_context.auto_run(request.purpose, request.subject, user):
            try:
                parsed, call = self._call_any(role, request, schema, account=account)
            except Exception as exc:
                run_context.record_call(request, schema.__name__, None, exc)
                raise
            run_context.record_call(request, schema.__name__, call, None)
            return parsed, call

    def refuse(self, call: LLMCall) -> None:
        """Code refused this answer after it parsed: an identical prompt is not answered with it
        again, it goes to the provider (a replay corpus stays as recorded)."""
        if self._mode != "replay":
            self._log.refuse(call.served_from or call.id)

    def _call_any[T: BaseModel](
        self, role: Role, request: LLMRequest, schema: type[T], *,
        account: Account | None = None,
    ) -> tuple[T, LLMCall]:
        if _workflow_durable() is None:
            return self._call_now(role, request, schema, account=account)

        def run() -> dict[str, Any]:
            parsed, call = self._call_now(role, request, schema, account=account)
            return {"parsed": parsed.model_dump(mode="json"), "call": call.model_dump(mode="json")}

        out = self._durable(f"llm.call:{request.purpose}", account, run)
        return schema.model_validate(out["parsed"]), LLMCall.model_validate(out["call"])

    def _call_now[T: BaseModel](
        self, role: Role, request: LLMRequest, schema: type[T], *,
        account: Account | None = None,
    ) -> tuple[T, LLMCall]:
        request = self._guided_request(request, account)
        usage, ref = self._route(role, account, request.purpose)
        request = _reasoned(request, ref)
        # the account's user, else the traced run's: a private call is keyed by its user
        user = account.user_id if account is not None else run_context.current_user_id()
        h = self._scoped(self._key(input_hash(ref, request, schema.__name__)), request.purpose,
                         user)
        needles = _needles(request.redact)
        persisted = _persisted_needles(needles)  # literal + escaped forms, longest first
        # a redacted call is its own execution, never the log's: nothing to share
        with nullcontext() if needles else _FLIGHTS.one(h):
            return self._answer(request, schema, usage, ref, user, h, needles, persisted, account)

    def _answer[T: BaseModel](
        self, request: LLMRequest, schema: type[T], usage: UsageControl | None, ref: ModelRef,
        user: str | None, h: str, needles: tuple[str, ...], persisted: tuple[str, ...],
        account: Account | None,
    ) -> tuple[T, LLMCall]:
        """The call's answer: from the log, else from the provider and logged."""
        looked = time.perf_counter()
        call = None if needles else self._log.get(h)  # redacted requests never read the cache
        if call is None:
            if self._mode == "replay":
                why = "; redacted calls are never replayable" if needles else ""
                raise MissingRecordingError(
                    f"no recorded llm_call for {request.purpose}/{request.subject} "
                    f"({ref.provider}:{ref.model}, hash {h[:12]}); re-run in record mode{why}"
                )
            provider = self._providers.get(ref.provider)
            if provider is None:
                raise ProviderUnavailableError(
                    f"provider {ref.provider!r} is not configured; set its API key env var "
                    f"(see `python -m evowiki.pipeline status`)"
                )
            # Each accepted redacted execution gets its own id; never the canonical cache id. A
            # cache row whose texts expired (store.retention) still holds the canonical id. The id
            # is final before the provider runs: the usage records name it (`_run_metered`).
            cid = f"llm_{h[:20]}"
            if needles:
                cid = f"{cid}_r{uuid.uuid4().hex}"
            elif self._log.taken(cid):
                cid = f"{cid}_e{uuid.uuid4().hex}"
            resp, parsed, latency, params = self._run_metered(
                usage, account, request.purpose, ref, provider,
                lambda p: self._record_attempts(ref, h, request, schema, p, persisted), cid)
            call = LLMCall(
                id=cid,
                input_hash=h,
                purpose=request.purpose,
                prompt_version=request.prompt_version,
                subject=request.subject,
                provider=ref.provider,
                model=ref.model,
                model_version=resp.model_version,
                params=params,                  # as sent: thinking off after a starved reply
                raw_output=resp.raw_output,
                parsed_output=parsed.model_dump(mode="json"),
                cost=resp.cost,
                latency=latency,
                ts=self._clock(),
                replayable=not needles,
                user_id=user,
                run_id=run_context.current_run_id(),
            )
            if self._requests is not None:
                # what was sent, redacted like the call's own copy; first, so a failed append
                # below never loses the input. Provenance also owns orphan request rows.
                self._requests.record(call.id, redact_text(request.system, persisted),
                                       redact_text(request.prompt, persisted), schema.__name__,
                                       source_user_id=request.params.get('source_user_id') or user,
                                       source_nodes=request.params.get('source_nodes', ()))
            self._log.append(call if not needles else call.model_copy(update={
                "raw_output": redact_text(call.raw_output, persisted),
                "parsed_output": _redact_json(call.parsed_output, persisted)}))
        else:
            call = self._served(call, user, time.perf_counter() - looked)
            self._record_replay(usage, account, request.purpose, ref, call.id)
        if call.parsed_output is None:  # legacy recordings of parse failures
            raise LLMParseError(f"llm_call {call.id} output did not match {schema.__name__}")
        return schema.model_validate(call.parsed_output), call

    def _record_attempts[T: BaseModel](
        self, ref: ModelRef, h: str, request: LLMRequest, schema: type[T], provider: Provider,
        persisted: tuple[str, ...] = (),
    ) -> tuple[ProviderResponse, T, float, dict[str, Any]]:
        """persisted: escaped redaction needles (empty = no redaction); applied to EVERY
        audited attempt's raw_output and error_message, before truncation. Returns the accepted
        response, its parsed value, its latency and the params it was sent with.

        A truncated or empty reply (STARVED_STATUSES) is retried once with thinking off
        (`_thinking_off`), one attempt beyond record_max_attempts, then fails closed; a request
        already sent with thinking off fails at once. parse_error is retried as before."""
        json_schema = schema.model_json_schema()
        wasted: float | None = None     # cost of the attempts whose reply did not parse
        sent, lowered, attempt = request, False, 0
        while True:
            attempt += 1
            start = time.perf_counter()
            last = attempt >= self._record_max_attempts
            try:
                resp = self._past_rate_limit(lambda sent=sent: provider.complete(ref.model, sent, json_schema),
                                             ref, h, sent, persisted=persisted)
            except ProviderOutputError as exc:
                self._audit(ref, h, sent, exc.status, start=start, raw_output=exc.raw_output,
                            model_version=exc.model_version, cost=exc.cost, error=exc,
                            persisted=persisted)
                if exc.status in STARVED_STATUSES:
                    if lowered or sent.params.get(THINKING_PARAM) is False:
                        raise
                    candidate = _thinking_off(sent, ref)
                    if candidate.params == sent.params:
                        raise
                    sent, lowered = candidate, True
                    continue
                if last or exc.status not in RETRYABLE_STATUSES:
                    raise
                continue
            except RequestCancelled:    # the run was stopped: no attempt row, no retry
                raise
            except Exception as exc:
                self._audit(ref, h, sent, "provider_error", start=start, error=exc,
                            persisted=persisted)
                raise
            latency = time.perf_counter() - start
            parsed = _parse(schema, resp.raw_output)
            if parsed is None:  # not cached: a later record run may retry the provider
                err = LLMParseError(
                    f"{ref.provider}:{ref.model} output for {request.purpose}/"
                    f"{request.subject} did not match {schema.__name__}; not recorded")
                if resp.cost is not None:
                    wasted = (wasted or 0.0) + resp.cost
                err.cost = wasted
                self._audit(ref, h, sent, "parse_error", start=start,
                            raw_output=resp.raw_output, model_version=resp.model_version,
                            cost=resp.cost, error=err, persisted=persisted)
                if last:
                    raise err
                continue
            self._audit(ref, h, sent, "accepted", start=start, raw_output=resp.raw_output,
                        model_version=resp.model_version, cost=resp.cost, persisted=persisted)
            return resp, parsed, latency, sent.params

    def converse(self, role: Role, request: ChatRequest, *, account: Account | None = None,
                 stream: StreamSink | None = None) -> tuple[ChatTurn, LLMCall]:
        """`stream`: the answer text goes to it live while a provider generates it (only when
        the provider streams and the turn is not served from the log; a replayed durable step
        runs nothing, so streams nothing). A failure after some text was streamed carries that
        text as `partial_text` on the exception (a JSON attribute: it crosses a step boundary)
        and is never retried: the learner saw that text, a second attempt would show it twice or
        mixed (DeepTutor's `output_emitted` rule, HKUDS/DeepTutor agents/loop/agent_loop.py)."""
        if request.deferred:
            # native_deferred (llm.exposure): no provider here sends deferred tools yet (the
            # Anthropic shape awaits its live check, plan T19); fail closed, the caller retries the
            # call two_phase
            from evowiki.llm.exposure import ToolSearchFailed
            raise ToolSearchFailed("deferred tools are not supported by this provider yet")
        # a chat call outside an agent loop is appended to the current run with its full message
        # list (llm/run_context.py), in a run of its own when there is none; a loop records its
        # own turns
        user = account.user_id if account is not None else None
        with run_context.auto_run(request.purpose, request.subject, user):
            try:
                turn, call = self._converse_any(role, request, account=account, stream=stream)
            except Exception as exc:
                run_context.record_chat(request, None, None, exc)
                raise
            run_context.record_chat(request, turn, call, None)
            return turn, call

    def _converse_any(self, role: Role, request: ChatRequest, *, account: Account | None,
                      stream: StreamSink | None) -> tuple[ChatTurn, LLMCall]:
        if _workflow_durable() is None:
            return self._converse_now(role, request, account=account, stream=stream)

        def run() -> dict[str, Any]:
            turn, call = self._converse_now(role, request, account=account, stream=stream)
            return {"turn": turn.model_dump(mode="json"), "call": call.model_dump(mode="json")}

        out = self._durable(f"llm.converse:{request.purpose}", account, run)
        return ChatTurn.model_validate(out["turn"]), LLMCall.model_validate(out["call"])

    def durable_step(self, name: str, account: Account | None,
                     run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """`run` (model calls on threads of its own) as ONE durable step, charged like `_durable`; a plain call outside a
        workflow. Its calls are served from the llm_call log when the step re-executes after a crash."""
        return run() if _workflow_durable() is None else self._durable(name, account, run)

    def _durable(self, name: str, account: Account | None,
                 run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """One model call as a durable step (module doc of `agent.durable`): the step returns the
        result and the call as JSON plus what it charged. A replayed step does not run, so its
        charge is added back to the job meter (`JobMeter.restore`) and the page's own spend and
        limit survive a restart. A step that re-executes after a crash is served from the
        llm_call log at zero cost, so that call's spend is missing from the page meter (accepted,
        see the spec's reliability boundary). In a DBOS workflow the call runs in a cancel scope on
        that workflow's status (`durable.workflow_cancel_check`): a cancelled workflow aborts the
        request in flight (`llm.deadline.RequestCancelled`)."""
        durable = _workflow_durable()
        if durable is None:  # callers check first; fail closed rather than run unrecorded
            raise RuntimeError("durable step requested outside a workflow")

        # 「停止」 cancels the workflow; the call in flight is aborted, not read to its end
        stopped = getattr(durable, "workflow_cancel_check", lambda: None)()

        def step() -> dict[str, Any]:
            charged: list[float] = []
            token = _charged.set(charged)
            try:
                with cancel_scope(stopped):
                    out = run()
            except Exception as exc:  # noqa: BLE001 - crosses the step as JSON, re-raised below
                return {"raised": durable.encode_exc(exc), "charged": sum(charged)}
            finally:
                _charged.reset(token)
            return {**out, "charged": sum(charged)}

        out, ran = durable.step_ran(name, step)
        if not ran and out["charged"] and account is not None and self._usage is not None:
            restore = getattr(self._usage.meter, "restore", None)
            if restore is not None:
                restore(out["charged"])
        if "raised" in out:
            raise durable.decode_exc(out["raised"])
        return out

    def _converse_now(self, role: Role, request: ChatRequest, *,
                      account: Account | None = None,
                      stream: StreamSink | None = None) -> tuple[ChatTurn, LLMCall]:
        """One tool-calling model turn. Same rules as `call`: replay serves the log only; record
        calls the provider on a miss, retries parse_error / truncated / empty_output, audits
        every attempt and logs only accepted turns. Metering mirrors `call`: with an account the
        tier picks the model, the budget is checked before any provider call, every paid
        response (retries and paid failures included) is recorded, and a turn served from the
        log is recorded as a zero-token replay. `account` never enters the request; its user
        keys the call-log entry (`_scoped`); an account without usage control is a ValueError."""
        request = self._guided_chat(request, account)
        usage, ref = self._route(role, account, request.purpose)
        request = _reasoned(request, ref)
        user = account.user_id if account is not None else run_context.current_user_id()
        h = self._scoped(self._key(chat_input_hash(ref, request)), request.purpose, user)
        with _FLIGHTS.one(h):
            return self._turn(request, usage, ref, user, h, account, stream)

    def _turn(self, request: ChatRequest, usage: UsageControl | None, ref: ModelRef, user: str | None, h: str,
              account: Account | None, stream: StreamSink | None) -> tuple[ChatTurn, LLMCall]:
        """The turn: from the log, else from the provider and logged."""
        looked = time.perf_counter()
        call = self._log.get(h)
        if call is None:
            if self._mode == "replay":
                raise MissingRecordingError(
                    f"no recorded llm_call for {request.purpose}/{request.subject} "
                    f"({ref.provider}:{ref.model}, hash {h[:12]}); re-run in record mode")
            provider = self._providers.get(ref.provider)
            if provider is None or not hasattr(provider, "chat"):
                raise ProviderUnavailableError(
                    f"provider {ref.provider!r} is not configured for tool calling")
            cid = f"llm_{h[:20]}"
            resp, latency = self._run_metered(
                usage, account, request.purpose, ref, provider,
                lambda p: self._record_chat_attempts(ref, h, request, p, stream), cid)
            call = LLMCall(
                id=cid, input_hash=h, purpose=request.purpose,
                prompt_version=request.prompt_version, subject=request.subject,
                provider=ref.provider, model=ref.model, model_version=resp.model_version,
                params=request.params, raw_output=resp.turn.model_dump_json(),
                parsed_output=resp.turn.model_dump(mode="json"), cost=resp.cost,
                latency=latency, ts=self._clock(), user_id=user,
                run_id=run_context.current_run_id())
            self._log.append(call)
        else:
            call = self._served(call, user, time.perf_counter() - looked)
            self._record_replay(usage, account, request.purpose, ref, call.id)
        return ChatTurn.model_validate(call.parsed_output), call

    def _record_chat_attempts(self, ref: ModelRef, h: str, request: ChatRequest,
                              provider: Any, stream: StreamSink | None = None
                              ) -> tuple[ChatResponse, float]:
        live = stream is not None and bool(getattr(provider, "streams", False))
        rec: _Streamed | None = None
        for attempt in range(1, self._record_max_attempts + 1):
            start = time.perf_counter()
            last = attempt == self._record_max_attempts
            if live:              # a retry only ever follows an attempt that showed nothing
                assert stream is not None
                rec = _Streamed(stream)
            try:
                try:
                    resp = self._past_rate_limit(
                        (lambda rec=rec: provider.chat(ref.model, request, stream=rec)) if live
                        else (lambda: provider.chat(ref.model, request)), ref, h, request, rec=rec)
                except Exception as exc:
                    if rec is not None and rec.streamed:
                        exc.partial_text = rec.streamed    # type: ignore[attr-defined]
                    raise
            except ProviderOutputError as exc:
                self._audit(ref, h, request, exc.status, start=start, raw_output=exc.raw_output,
                            model_version=exc.model_version, cost=exc.cost, error=exc)
                if last or exc.status not in RETRYABLE_STATUSES or _shown(rec):
                    raise
                continue
            except RequestCancelled:    # the run was stopped: no attempt row, no retry
                raise
            except Exception as exc:
                self._audit(ref, h, request, "provider_error", start=start, error=exc)
                raise
            raw = resp.turn.model_dump_json()
            if resp.turn.empty:
                err = ProviderOutputError(
                    f"{ref.provider}:{ref.model} returned an empty turn for "
                    f"{request.purpose}/{request.subject}", raw_output=raw,
                    model_version=resp.model_version, cost=resp.cost, status="empty_output")
                if rec is not None and rec.streamed:
                    err.partial_text = rec.streamed    # type: ignore[attr-defined]
                self._audit(ref, h, request, "empty_output", start=start, raw_output=raw,
                            model_version=resp.model_version, cost=resp.cost, error=err)
                if last or _shown(rec):
                    raise err
                continue
            self._audit(ref, h, request, "accepted", start=start, raw_output=raw,
                        model_version=resp.model_version, cost=resp.cost)
            return resp, time.perf_counter() - start
        raise AssertionError("unreachable: record_max_attempts is validated >= 1")
