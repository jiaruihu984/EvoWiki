from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Role = Literal["generator", "verifier"]


class ModelRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    # Applied to request params before hashing/auditing; absent on existing pipeline refs.
    reasoning_effort: str | None = Field(default=None, exclude=True)


class GatewayConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    generator: ModelRef
    verifier: ModelRef
    # Spec §8 requires distinct providers. Single-provider mode is an explicit, user-approved
    # downgrade (only one API key available); content it produces is weaker evidence.
    allow_single_provider: bool = False

    @model_validator(mode="after")
    def _distinct_providers(self) -> "GatewayConfig":
        if self.single_provider and not self.allow_single_provider:
            raise ValueError("generator and verifier must use different providers "
                             "(set allow_single_provider to opt into single-provider mode)")
        return self

    @property
    def single_provider(self) -> bool:
        return self.generator.provider == self.verifier.provider

    def ref(self, role: Role) -> ModelRef:
        return self.generator if role == "generator" else self.verifier


TEMPERATURE_PARAM = "temperature"   # request.params[TEMPERATURE_PARAM]: DeepSeek without thinking only
THINKING_PARAM = "thinking"   # request.params[THINKING_PARAM]: bool, set only by a harness pack (H10)


class LLMRequest(BaseModel):
    """One typed request to the LLM boundary.

    redact: audit-only: strings replaced in PERSISTED copies; never sent, never hashed."""

    model_config = ConfigDict(frozen=True)

    purpose: str
    prompt_version: str
    subject: str
    system: str
    prompt: str
    params: dict[str, Any] = Field(default_factory=dict)
    redact: tuple[str, ...] = ()


class ProviderResponse(BaseModel):
    raw_output: str
    model_version: str | None = None
    cost: float | None = None
    # Token counts as the provider reported them (None: not reported). Read only by usage
    # accounting (plan rev 3.2 §9.4); never persisted, so llm_call rows are unchanged.
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    # Prompt-cache tokens (Anthropic cache_read_input_tokens / cache_creation_input_tokens;
    # DeepSeek prompt_cache_hit_tokens as reads, no writes). input_tokens stays the uncached
    # remainder (DeepSeek: prompt_cache_miss_tokens); the prompt total is the sum of the three.
    # cache_write_1h_tokens: the part of cache_write_tokens written with the 1-hour TTL (priced
    # higher; Anthropic usage.cache_creation.ephemeral_1h_input_tokens).
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    cache_write_1h_tokens: int | None = Field(default=None, ge=0)


class LLMCall(BaseModel):
    """One recorded LLM boundary call (spec §4.2 llm_call): one accepted provider execution.

    Every accepted provider execution whose output is returned to or referenced by downstream
    runtime state has its own llm_call.id. Failed/unaccepted executions remain represented only
    by llm_attempt. input_hash is the stable, nonce-free identity of the provider request;
    replayable rows are the unique replay/cache corpus."""

    model_config = ConfigDict(frozen=True)

    id: str
    input_hash: str
    purpose: str
    prompt_version: str
    subject: str
    provider: str
    model: str
    model_version: str | None
    params: dict[str, Any]
    raw_output: str
    parsed_output: dict[str, Any] | None
    cost: float | None
    latency: float = Field(ge=0.0)
    ts: AwareDatetime
    # False: audit-only execution whose persisted copy is redacted; never served by get()/replay.
    # True rows are unique per input_hash.
    replayable: bool = True
    # Observability (migration 0090): the user the call ran for (None: no user, e.g. offline
    # tools), the traced run it ran in (`llm.run_context`), and, for a logical call answered from
    # the log, the llm_call it was served from (a non-replayable row, cost 0).
    user_id: str | None = None
    run_id: str | None = None
    served_from: str | None = None


AttemptStatus = Literal[
    "accepted", "parse_error", "truncated", "empty_output", "refusal", "provider_error"]


class LLMAttempt(BaseModel):
    """One provider call made in record mode, successful or not (audit only, never replayed)."""

    model_config = ConfigDict(frozen=True)

    id: str
    input_hash: str
    purpose: str
    prompt_version: str
    subject: str
    provider: str
    model: str
    model_version: str | None
    params: dict[str, Any]
    raw_output: str | None
    status: AttemptStatus
    error_type: str | None
    error_message: str | None
    cost: float | None
    latency: float = Field(ge=0.0)
    ts: AwareDatetime
