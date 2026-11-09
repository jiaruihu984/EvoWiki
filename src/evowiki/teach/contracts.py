"""Teaching spec, feedback and per-user RSI shared contracts (plan rev 3.3 §10.4–10.5; architecture spec
§0 / §7). Pure data — no IO, no LLM.

RSI stays as defined in the architecture spec: model weights never change; the system recursively changes
only θ_infer,u (how evidence becomes K for this user), θ_rep,u (how the wiki is organized and explained for
this user) and Patch Memory, and every change passes a Validation Gate. Shared skeleton:
    structured logs → Failure Localizer → Proposer (retrieves Patch Memory) → Validation Gate → Policy Registry
Lanes build against these types in parallel:
  R1  teaching spec (θ_rep,u text part), exemplars, feedback signals, per-page structured log;
  R2  Policy Registry, Patch Memory, localizer frame, RSI-A (θ_infer,u + channel A);
  R3  RSI-B (θ_rep,u + channel B);
  UI windows render the HTTP shapes.
Changing a type here is a cross-lane change: do it in its own commit and tell the other lanes."""

from typing import Annotated, Any, Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---- θ_rep,u: teaching spec (R1 owns storage; R3 patches it) ----

SpecScope = Literal["any", "concept", "exercise", "quick"]
SUGGESTED_SPEC_ITEMS = 30                # prompt guidance only (RSI proposer): never enforced in code
MAX_ITEM_TEXT = 500

# Rule provenance contract (the product specification §1.1):
# `origin` = where a rule FIRST came from (never changes), `authority` = who may change it NOW,
# `origin_ref` = the reference of that origin (message / ledger row / patch id / seed name), `rev` =
# the rule's own content version (text, scope or domain changed → +1), `rev_by` = who made the last
# content change. `protected`: a base constraint of the default spec RSI never changes on its own
# (decision 3). The checkpoint that enforces who may change what is `rules.authority.settle`.
Origin = Literal["default", "seed", "user_ui", "user_chat", "assistant_drafted",
                 "memory_backfill", "rsi_b", "rsi_h", "legacy_unknown"]
Authority = Literal["user", "rsi", "pending"]
RevBy = Literal["user", "rsi", "system"]
HARNESS_REF = "harness:"                 # origin_ref prefix of a harness (track H) change
# The default rules (decision 3; the user's classification of 2026-10-28,
# the product specification): d12 simplifications said
# aloud, d16 interactives from the component library, d18 one right answer, d20 no internal
# wording are base constraints RSI never changes on its own (`protected`). d06 / d13 / d15 are
# teaching strategy, but each carries a floor (runnable code; no unfounded "already known";
# accurate, accessible figures) that is not yet a protected rule or a code check of its own: until
# it is, RSI does not change them automatically either (HELD_DEFAULTS). The other 15 are RSI's to
# tune after validation. The user edits any of them freely (the edit makes it theirs).
PROTECTED_DEFAULTS: frozenset[str] = frozenset({"d12", "d16", "d18", "d20"})
HELD_DEFAULTS: frozenset[str] = frozenset({"d06", "d13", "d15"})


def rsi_may_change(item: "SpecItem") -> bool:
    """Whether an automatic (RSI / system) change may edit or remove `item`: it is RSI's, not
    protected, and not a held default."""
    return (item.authority == "rsi" and not item.protected
            and not (item.origin == "default" and item.item_id in HELD_DEFAULTS))


class SpecItem(_M):
    """One teaching rule (ACE playbook item). Pages log which item ids were in force, so a failure can be
    traced back to items (the spec's `triggered_by_rule_id`).

    Provenance (rule provenance contract §1.1): `origin` / `authority` / `origin_ref` / `rev` /
    `rev_by` / `protected`. No user predates the contract (user ruling 2026-10-28: 「旧的一律删了就
    好了」; the old events were deleted): an item in the old shape (a `source`, a free-text
    `origin`) is invalid, never read or mapped."""

    item_id: str
    text: str = Field(min_length=1, max_length=MAX_ITEM_TEXT)
    scope: SpecScope = "any"
    domain: str | None = Field(default=None, max_length=60)    # None = every domain
    origin: Origin = "legacy_unknown"
    authority: Authority = "user"
    origin_ref: str | None = Field(default=None, max_length=200)   # seed name / message / patch id
    rev: int = Field(default=1, ge=1)
    rev_by: RevBy = "user"
    protected: bool = False
    helpful: float = Field(default=0.0, ge=0.0)               # decayed counts (Patch Memory rule)
    harmful: float = Field(default=0.0, ge=0.0)


class Exemplar(_M):
    """A page the user holds up as good: calibrates deep refine, and is an anchor for RSI-B's Gate."""

    exemplar_id: str
    kind: Literal["wiki_page", "vault_note"]
    ref: str = Field(min_length=1, max_length=400)             # page id or vault-relative path
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=60_000)         # snapshot at the time it was chosen


class RepParams(_M):
    """Numeric part of θ_rep,u, clamped (plan Phase 5 (b))."""

    recap_max_per_page: int = Field(default=4, ge=0, le=8)
    analogy_level: int = Field(default=1, ge=0, le=2)          # 0 none · 1 known-only · 2 liberal
    fading_level: int = Field(default=1, ge=0, le=2)           # worked-example fading
    interactive_bias: int = Field(default=1, ge=0, le=2)       # 0 avoid · 1 when needed · 2 prefer


class TeachingSpec(_M):
    """θ_rep,u as every writer reads it. Absorbs the old 原则 (Guideline) list."""

    version: int = Field(ge=1)
    items: tuple[SpecItem, ...]
    exemplars: tuple[Exemplar, ...] = Field(default=(), max_length=3)
    params: RepParams = RepParams()
    # the RepParams paths the user set themself (rule provenance contract §1.3 for params: an
    # automatic change never moves them; stamped by the checkpoint, `teach.rule_authority`)
    user_params: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> "TeachingSpec":
        ids = [i.item_id for i in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("spec item ids repeat")
        bad = [p for p in self.user_params if p not in RepParams.model_fields]
        if bad or len(set(self.user_params)) != len(self.user_params):
            raise ValueError(f"user_params must name distinct RepParams paths: {self.user_params}")
        return self

    def complexity(self) -> int:
        """Architecture spec §7.4: Complexity = #items + #params away from default."""
        default = RepParams()
        moved = sum(getattr(self.params, f) != getattr(default, f) for f in RepParams.model_fields)
        return len(self.items) + moved


# ---- θ_infer,u: RSI-A surface (R2) ----

EvidenceKind = Literal["quiz", "check", "self", "feedback", "ask", "recall", "slip_claim",
                       "learned_claim"]
# The user's own reading of a wrong answer (wrong-answer follow-up), not observations: their weights
# are trust levels in [0, 1]. slip_claim = the share of the wrong answer's evidence set aside while
# 「粗心选错了」 stands; learned_claim = the weight of the weak 「我学会了」 positive while it waits for
# its confirmation question. Conservative defaults; RSI-A learns them from the confirmation results.
CLAIM_KINDS: tuple[EvidenceKind, ...] = ("slip_claim", "learned_claim")
_DEFAULT_WEIGHTS: dict[str, float] = {"quiz": 1.0, "check": 1.0, "self": 0.6, "feedback": 0.4,
                                      "ask": 0.3, "recall": 1.0, "slip_claim": 0.3,
                                      "learned_claim": 0.3}


class InferParams(_M):
    """Whitelisted, clamped θ_infer,u paths (architecture spec §7.3). Safety constants stay out (plan §5)."""

    evidence_weight: dict[EvidenceKind, float] = Field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    forgetting_scale: float = Field(default=1.0, ge=0.5, le=2.0)   # × the default half-life
    slip: float = Field(default=0.1, ge=0.0, le=0.3)
    guess_scale: float = Field(default=1.0, ge=0.5, le=1.5)        # × 1/options
    inference_strength: float = Field(default=1.0, ge=0.0, le=1.5)
    # 一开始就会的可能 (BKT's P(L0)): the K's belief in a cell before any evidence. The default is the
    # K's general prior mean; K applies the odds ratio to this default (aspects.py), so the default
    # leaves every configured prior unchanged. Rows stored before this field get the default.
    start_known: float = Field(default=0.3, ge=0.05, le=0.95)

    @model_validator(mode="before")
    @classmethod
    def _claim_defaults(cls, data: Any) -> Any:
        """θ stored before the claim kinds existed gets their defaults (only those two keys: any
        other missing kind still fails closed downstream)."""
        if isinstance(data, dict) and isinstance(data.get("evidence_weight"), dict):
            w = data["evidence_weight"]
            data = {**data, "evidence_weight": {
                **{k: _DEFAULT_WEIGHTS[k] for k in CLAIM_KINDS if k not in w}, **w}}
        return data

    @model_validator(mode="after")
    def _weights(self) -> "InferParams":
        for k, w in self.evidence_weight.items():
            hi = 1.0 if k in CLAIM_KINDS else 2.0
            if not 0.0 <= w <= hi:
                raise ValueError(f"evidence weight {k} out of [0, {hi:g}]")
        return self


# ---- feedback signals (R1 records; R2/R3 read) ----

FeedbackChip = Literal["unclear", "too_long", "term_before_explained", "no_example", "too_shallow",
                       "too_hard"]
CHIP_LABEL: dict[FeedbackChip, str] = {
    "unclear": "不像人话", "too_long": "太长", "term_before_explained": "术语没先讲",
    "no_example": "例子不够", "too_shallow": "太浅", "too_hard": "太难"}


class PageFeedbackIn(BaseModel):
    """POST /wiki/pages/{id}/feedback — the page-level box (section buttons stay as they are)."""

    model_config = ConfigDict(extra="forbid")

    chips: tuple[FeedbackChip, ...] = Field(default=(), max_length=6)
    text: str = Field(default="", max_length=500)
    section_id: str | None = None
    # the section version the user was shown (its `artifact_id`; 0091); None = page-level
    artifact_id: str | None = Field(default=None, pattern=r"^art_[0-9a-f]{24}$")
    # 「也按这条调整以后页面的讲法」(default on): with text, the feedback is answered with `rsi` =
    # "queued"; since RSI-B retired (2026-10-29) it reaches the harness round like any signal
    apply: bool = True

    @model_validator(mode="after")
    def _something(self) -> "PageFeedbackIn":
        if not self.chips and not self.text.strip():
            raise ValueError("pick a chip or write something")
        return self


SignalKind = Literal["section_know", "section_deeper", "section_other", "page_feedback", "note_edit",
                     "assistant_selection", "check_answer", "review_grade", "expand", "proposal_decision",
                     "card_feedback", "check_unclear", "block_result", "assistant_rejected",
                     "assistant_rated", "user_complaint", "page_fit"]
# page_fit: the learner pressed 基本合适 at the end of a page (POST /wiki/pages/{id}/fit).
# block_result: an EvoUI block's results came back (POST /wiki/assets/{id}/results): text = quiz
# summary + final state, outcome = the quiz score (None without a quiz).
# assistant_rejected: derived at read time (wikiapp.signals.assistant_rejections), never stored: the user
# rejected / undid something an assistant turn produced. About the assistant's answer, not a page section.
# assistant_rated: derived at read time too (wikiapp.signals.assistant_ratings): the user's current 👎 on
# an assistant answer (a 👍 is no signal; a rating cleared or turned 👍 is no longer one). Not a page section.
# user_complaint: the learner complained in the assistant chat about what the system does (「我都说了不会了，
# 为什么还问」); stored by the assistant's report_problem: text = the problem + his words, page = the
# session's page. Not a page section's failure: the harness round's improver reads it.
# check_unclear: after a wrong answer the user said 「看不懂」, or a 「我学会了」 failed its confirmation
# question — the section that teaches the cell did not land (section_id = that teaching section).
# How strongly a signal speaks about the artifact it targets (RSI observability plan 2026-10-29 §3, the
# user's grading): strong = names a problem (page feedback, 看不懂, self-report, card feedback);
# medium = a wrong answer / block result / complaint (hard to understand, or a bad question);
# medium_weak = a review grade (memory more than teaching); weak = an assistant 👎; not_quality = an
# assistant change rejected (not a sign the teaching was bad); edit_diff = the learner's own edit (a
# diff, not proof the text was wrong); very_weak = expand / selection / proposal decision. Phase ③
# weights attribution by it; one signal counts once.
EvidenceType = Literal["strong", "medium", "medium_weak", "weak", "not_quality", "edit_diff",
                       "very_weak"]
EVIDENCE: dict[str, EvidenceType] = {
    "page_feedback": "strong", "check_unclear": "strong", "section_know": "strong",
    "section_deeper": "strong", "section_other": "strong", "card_feedback": "strong",
    "check_answer": "medium", "block_result": "medium", "user_complaint": "medium",
    "review_grade": "medium_weak", "assistant_rated": "weak", "assistant_rejected": "not_quality",
    "note_edit": "edit_diff", "expand": "very_weak", "assistant_selection": "very_weak",
    "proposal_decision": "very_weak", "page_fit": "very_weak"}
assert set(EVIDENCE) == set(get_args(SignalKind)), "every signal kind has an evidence type"
CardFeedbackKind = Literal["unclear", "wrong", "too_easy", "drop"]   # 复习卡「这张卡有问题…」


class Signal(_M):
    """One observable signal, normalised. Never contains hidden simulator state (anti-loop isolation)."""

    signal_id: str
    kind: SignalKind
    at: AwareDatetime
    page_id: str | None = None
    section_id: str | None = None
    chips: tuple[FeedbackChip, ...] = ()
    text: str = Field(default="", max_length=2000)            # user's words / diff summary / selection
    outcome: float | None = None                               # check/review: 1 correct … 0 wrong
    spec_version: int | None = None                            # θ_rep,u in force when the page was made
    infer_version: int | None = None                           # θ_infer,u in force
    # the artifact version the user was shown when he reacted (store.artifacts, 0091): sent by the
    # client, never looked up at submit time; None = not known (older signals, a page-level reaction)
    target_artifact_id: str | None = None
    evidence_type: EvidenceType | None = None                  # None → EVIDENCE[kind] (filled below)

    @model_validator(mode="after")
    def _evidence(self) -> "Signal":
        if self.evidence_type is None:
            object.__setattr__(self, "evidence_type", EVIDENCE[self.kind])
        return self


class SectionLog(_M):
    """Per-section structured log written whenever a page is organized, patched or refined (plan Phase 5
    step 1). This is what the localizer traces back through."""

    page_id: str
    section_id: str
    at: AwareDatetime
    producer: Literal["organizer", "writer", "patch", "refine", "edit"]
    cells: tuple[str, ...] = ()
    bands: dict[str, str] = Field(default_factory=dict)        # cell → band at the time
    role: str
    why: str = Field(default="", max_length=600)
    spec_items: tuple[str, ...] = ()                           # item ids in force
    spec_version: int
    infer_version: int
    guardrails_ok: bool = True


# ---- RSI skeleton (R2), shared by RSI-A and RSI-B ----

Track = Literal["A", "B"]
FailureClass = Literal[
    # RSI-A (architecture spec §7.2)
    "overconfident", "underconfident", "stale", "propagation_error",
    # RSI-B (spec §7.2 + plan rev 3.3 §10.4)
    "redundant", "prereq_omitted", "misframed", "missed_connection", "hub_rejected", "navigation_cost",
    "unclear", "too_long", "no_example", "too_shallow", "too_hard"]


class Localization(_M):
    track: Track
    failure: FailureClass
    target: Literal["spec_item", "rep_param", "infer_param", "k_estimate", "guardrail"]
    ref: str = Field(min_length=1, max_length=120)            # item id / param path
    signals: tuple[str, ...] = Field(min_length=1)             # signal ids
    reason: str = Field(min_length=1, max_length=600)


class SpecPatchOp(_M):
    op: Literal["add_item", "edit_item", "remove_item"]
    item_id: str | None = None
    text: str | None = Field(default=None, max_length=MAX_ITEM_TEXT)
    scope: SpecScope = "any"
    domain: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "SpecPatchOp":
        if self.op != "add_item" and not self.item_id:
            raise ValueError("edit/remove need item_id")
        if self.op != "remove_item" and not self.text:
            raise ValueError("add/edit need text")
        return self


class ParamPatchOp(_M):
    op: Literal["set_param"] = "set_param"
    track: Track
    path: str = Field(min_length=1, max_length=80)             # e.g. "evidence_weight.self", "analogy_level"
    value: float


# ---- A1 statistical fitter (layer 1): model structure M and the fit patch ----

FitAlgorithm = Literal["coord_map@1", "coord_map_recency@1"]
# θ_infer paths (mirrors teach.infer_paths.PATHS, which imports this module, so it is rebuilt here)
# (the unpacking form: the plugin's schema-parity reader skips tuples it cannot read statically)
_FIT_PATHS = (*("evidence_weight." + k for k in get_args(EvidenceKind)),
              "forgetting_scale", "slip", "guess_scale", "inference_strength", "start_known")
# start_known is fitted by default (Task 20): left out, a user who starts out knowing more than the
# K's prior pushes slip down and guess_scale up (the ASSISTments finding); when the start is the
# default it stays there and the fit is unchanged (the product specification).
# The implicit A2 v1 (`layer2.method_registry.DEFAULT_FIT_MODEL`) uses these, so it now fits 7 paths
# under the same version 1; stored v1 rows keep the paths they stored.
DEFAULT_FIT_PATHS = ("slip", "guess_scale", "forgetting_scale", "evidence_weight.self",
                     "evidence_weight.slip_claim", "evidence_weight.learned_claim", "start_known")


class FitModel(_M):
    """Model structure M of A1 (what A2 changes): which θ_infer paths are fitted, how strongly the
    hierarchical prior pulls, which fitting algorithm. Paths not listed keep their current value."""

    version: int = Field(ge=1)
    paths: tuple[str, ...] = Field(min_length=1)
    # which defaults this model started from: the implicit A2 v1 says "fit-defaults@2" (7 paths);
    # a model stored before this field existed has None (the 6-path defaults), so two "v1"s with
    # different content are told apart. Carried by commits / rollbacks (copies keep it).
    defaults_id: str | None = None
    prior_strength: float = Field(default=1.0, ge=0.125, le=8.0)
    algorithm: FitAlgorithm = "coord_map@1"
    recency_half_life_days: float | None = Field(default=None, ge=7.0, le=365.0)

    @model_validator(mode="after")
    def _shape(self) -> "FitModel":
        if len(set(self.paths)) != len(self.paths):
            raise ValueError("duplicate fit paths")
        bad = [p for p in self.paths if p not in _FIT_PATHS]
        if bad:
            raise ValueError(f"unknown fit paths: {bad}")
        if (self.algorithm == "coord_map_recency@1") != (self.recency_half_life_days is not None):
            raise ValueError("recency_half_life_days is set iff algorithm is coord_map_recency@1")
        return self


class FitPatchOp(_M):
    op: Literal["fit"] = "fit"
    track: Literal["A"] = "A"
    values: dict[str, float] = Field(min_length=1)               # path → fitted value (MAP)
    intervals: dict[str, tuple[float, float]] = Field(default_factory=dict)   # path → 90 % interval
    model_version: int = Field(ge=1)                             # FitModel.version used

    @model_validator(mode="after")
    def _shape(self) -> "FitPatchOp":
        bad = [p for p in (*self.values, *self.intervals) if p not in _FIT_PATHS]
        if bad:
            raise ValueError(f"unknown fit paths: {bad}")
        for path, (lo, hi) in self.intervals.items():
            if path not in self.values or not lo <= self.values[path] <= hi:
                raise ValueError(f"interval for {path} must contain its value")
        return self


PatchBody = Annotated[SpecPatchOp | ParamPatchOp | FitPatchOp, Field(discriminator="op")]
# "awaiting_user": a change that passed its Gate waits for the user (their Bot level is `ask`,
# `teach.approval`), or a record from before changes applied without confirmation; "rejected": the
# user said no to it (or, track A, its gate-run cap ran out)
PatchStatus = Literal["candidate", "gate_failed", "awaiting_user", "personal_accepted", "rejected",
                      "rolled_back"]


class Patch(_M):
    """One patch per track per cycle (plan §10.4). Lifecycle from architecture spec §7.5; global_* states
    are out of scope while there is one user."""

    patch_id: str
    track: Track
    localization: Localization
    body: PatchBody
    why: str = Field(min_length=1, max_length=600)
    status: PatchStatus = "candidate"
    from_version: int
    to_version: int | None = None
    created_at: AwareDatetime


class GateCase(_M):
    page_id: str
    section_id: str
    anchor: bool                                               # exemplar/anchor page vs the problem page
    before: str = Field(max_length=20_000)
    after: str = Field(max_length=20_000)
    verdict: Literal["better", "same", "worse"]
    note: str = Field(default="", max_length=600)


class GateResult(_M):
    """Channel A: time-split log loss / Brier on this user's later outcomes. Channel B: offline rewrite
    comparison + anchors + vetoes; a pass applies the patch (no confirmation)."""

    patch_id: str
    channel: Literal["A", "B"]
    passed: bool
    vetoes: tuple[str, ...] = ()                               # which veto fired, if any
    utility_before: float | None = None
    utility_after: float | None = None
    complexity_before: int | None = None
    complexity_after: int | None = None
    n_outcomes: int | None = None                              # A: < 30 → stays candidate
    ci_low: float | None = None                                # A: paired bootstrap CI of the improvement
    ci_high: float | None = None
    cases: tuple[GateCase, ...] = ()                           # B
    cost_usd: float = 0.0
    # B: a rewrite or judge call raised (provider / infra error, veto "gate_error"): which call and
    # the exception, e.g. "judge: BadRequestError: …". Not a verdict: the patch stays retryable.
    error: str = Field(default="", max_length=300)


AnchorMetric = Literal["pred_logloss", "pred_auc", "check_correct", "unclear_rate",
                       "review_recall", "feedback_negative", "feedback_too_shallow",
                       "feedback_too_hard", "undo_rate", "child_helpful", "page_cost",
                       "page_latency", "assistant_down_rate"]
# child_helpful (layer 2): share of decided child changes judged helpful under a method version
# page_cost / page_latency (harness RSI, design 2026-10-21 §5.7): mean cost (USD) / summed model
# latency (s) of one deep write run; only `harness.observe` computes them
# assistant_down_rate (harness RSI, 助手回答评价): the 👎 share of the rated assistant answers
# (down / (up + down), one per answer, placed by the answer's time); only `harness.observe` computes it
Verdict = Literal["pending", "helpful", "harmful", "no_effect"]


class PatchMemoryEntry(_M):
    """Architecture spec §7.6: (failure signature, patch, θ path, validation, ΔU, complexity, online
    performance). Counts decay; one noisy outcome never bans a direction forever (Stage 0 HB lesson)."""

    patch_id: str
    track: Track
    signature: str = Field(min_length=1, max_length=200)       # failure class + target + scope
    path: str
    gate: GateResult
    status: PatchStatus
    delta_utility: float | None = None
    online_helpful: float = 0.0
    online_harmful: float = 0.0
    long_term: Verdict = "pending"                             # default keeps old stored rows valid
    at: AwareDatetime


class AnchorDelta(_M):
    """One fixed anchor metric before vs after a change; `delta` is oriented so + is better."""

    metric: AnchorMetric
    before: float | None = None
    after: float | None = None
    delta: float | None = None
    lo: float | None = None                                    # 90 % CI of delta
    hi: float | None = None
    n_before: int = 0
    n_after: int = 0


class OutcomeOut(_M):
    """Long-term outcome of one RSI change, shown in 参数. The point written when a change stops
    being applied copies the last measurement and carries a machine note saying who rolled it
    back ("user_undo" / "auto_rollback"; "rollback_blocked_by_newer" when a newer change held an
    automatic rollback back: `layer2.outcomes`)."""

    change_id: str
    at: AwareDatetime
    verdict: Verdict
    deltas: tuple[AnchorDelta, ...] = ()
    note: str = ""                                 # one plain-Chinese line, or a rollback note


# ---- layer 2: method registry (A2 = FitModel versions) ----

# B2 (BMethod versions of the retired RSI-B) left with it on 2026-10-29 (migration 0099)
MethodKind = Literal["A2"]


class MethodVersion(_M):
    """Method registry row: one version of A1's FitModel (A2) for one user. The carried model's
    own `version` equals the row's."""

    kind: MethodKind
    version: int = Field(ge=1)
    at: AwareDatetime
    change_id: str | None = None                               # None = initial / rollback copy
    fit_model: FitModel

    def require_next(self, user_id: str, last: int) -> None:
        """Require the next append-only version for this user's method registry."""
        if self.version != last + 1:
            raise ValueError(
                f"{user_id}/{self.kind}: version {self.version} is not the next ({last + 1})")

    @model_validator(mode="after")
    def _one(self) -> "MethodVersion":
        if self.fit_model.version != self.version:
            raise ValueError("the carried model's version must equal the row's")
        return self


# awaiting_user / rejected: the user's Bot level for `rsi` is `ask` (`teach.approval`)
MethodStatus = Literal["applied", "gate_failed", "rolled_back", "awaiting_user", "rejected"]


class MethodChange(_M):
    """One layer-2 method change (shown in 参数): applied after its gate, or refused by it, or
    rolled back later. `gate`: the held-out comparison."""

    change_id: str
    kind: MethodKind
    status: MethodStatus
    summary: str = Field(max_length=300)                       # plain Chinese: what changed
    why: str = Field(max_length=600)
    from_version: int = Field(ge=1)
    to_version: int | None = Field(default=None, ge=1)
    gate: dict[str, Any] = Field(default_factory=dict)
    created_at: AwareDatetime
    updated_at: AwareDatetime | None = None


class PolicyVersion(_M):
    """Policy Registry row: one version of θ_infer,u or θ_rep,u for one user."""

    track: Track
    version: int = Field(ge=1)
    at: AwareDatetime
    patch_id: str | None = None                                # None = initial / import / user edit
    infer: InferParams | None = None                           # track A
    spec: TeachingSpec | None = None                           # track B

    @model_validator(mode="after")
    def _one(self) -> "PolicyVersion":
        if (self.track == "A") != (self.infer is not None) or (self.track == "B") != (self.spec is not None):
            raise ValueError("track A carries infer, track B carries spec")
        return self


class RsiSettings(_M):
    # manual (the default): never due, nothing runs until the user turns RSI on (`RSI_SWITCH_ON`).
    # weekly (what the switch turns on, 2026-10-28): at most once a week, and only when at least
    # `cycle.WEEKLY_MIN_SIGNALS` new signals arrived since the last run, so an unused wiki never
    # runs one (with `harness` on: since the last harness round, design 2026-10-21 §5.1; a Bot
    # trigger obeys the same week, `rsi_runs.bot_rsi_runner`). every_10_feedback: once 10 new
    # signals arrived; every_5_feedback is kept for old settings
    cycle: Literal["every_10_feedback", "every_5_feedback", "weekly", "manual"] = "manual"
    # the one RSI improvement round (design 2026-10-21 §5, `harness.round`): on, a cycle runs one
    # harness round after track A; off, a cycle runs track A (+ A2 and the observation of changes
    # in force) only. Off until the user turns it on (their decision; not a cost cap)
    harness: bool = False
    # the harness rounds' spend this month (`store.harness_rsi.month_cost`); None = no cap
    harness_month_limit_usd: float | None = Field(default=None, ge=0.0)


# The settings' one switch (PUT /wiki/rsi/switch). Running takes all three values: the round on, a
# cycle that can come due, and no cap of 0 (spend + estimate >= 0 skips every round).
RSI_SWITCH_ON = RsiSettings(cycle="weekly", harness=True, harness_month_limit_usd=None)
RSI_SWITCH_OFF = RsiSettings(cycle="manual", harness=False, harness_month_limit_usd=0.0)


class RsiSwitch(_M):
    on: bool


def rsi_on(s: RsiSettings) -> bool:
    """Rounds can actually run under these settings."""
    return s.harness and s.cycle != "manual" and s.harness_month_limit_usd != 0.0


# ---- feedback understanding (Obsidian flow design §4.1, B1) ----

class SectionAddition(_M):
    section_id: str
    add: str = Field(min_length=1, max_length=1500)            # a paragraph appended to the section


class PagePlan(_M):
    """How this page changes right away: paragraphs added to named sections, and at most one
    library component inserted after a section (component ids come only from the library). A
    configurable component comes with its `component_config` (validated against the component's
    schema; invalid → not inserted) and every component with `component_why` (why this one fits)."""

    summary: str = Field(min_length=1, max_length=200)
    sections: tuple[SectionAddition, ...] = Field(default=(), max_length=4)
    component: str | None = None
    component_after: str | None = None                         # section id; None = after the first
    component_config: dict[str, Any] | None = None
    component_why: str = Field(default="", max_length=300)
    # instead of a component: one EvoUI block (`deep.evoui.contracts.EvoUIBlock` as JSON), validated and
    # render-checked before it is placed (invalid → not placed); never both a component and a block
    block: dict[str, Any] | None = None
    block_why: str = Field(default="", max_length=300)


class StylePlan(_M):
    """The teaching-rule change this feedback asks for (shown with the feedback's reading)."""

    kind: str = Field(min_length=1, max_length=20)             # 问题类型, a few characters
    before: str = Field(min_length=1, max_length=300)          # the rule as it reads now
    after: str = Field(min_length=1, max_length=300)           # the rule it should become


class FeedbackReading(_M):
    """One understanding call's result. `reading` restates the user's own words; `missing` are
    concept ids of this page / its path the user said they do not know."""

    reading: str = Field(min_length=1, max_length=300)
    missing: tuple[str, ...] = Field(default=(), max_length=8)
    page: PagePlan
    style: StylePlan


# ---- "系统最近为你改了什么" (every layer) ----

ChangeTrack = Literal["A", "B", "G", "H"]     # G / H = rows from an external ChangeSource (graph / harness)


class ChangeOut(BaseModel):
    """GET /wiki/rsi/changes: every RSI change of every layer, newest first. RSI changes apply
    without confirmation once their Gate passes (decision 3); undo: POST
    /wiki/rsi/patches/{id}/undo (A / B patches restore from_version, a layer-2 method change rolls
    its registry back to from_version, an external source's row goes to that source).

    `layer`: 0 = an A fit / set_param patch or a B rule patch (they change θ); 2 = an A2 / B2
    method change (`patch_id` is its MethodChange.change_id, `track` "A" / "B", status "applied"
    shown as personal_accepted). Layer 1 = the improvers themselves: RSI never changes their code,
    so no layer-1 rows exist. `target`: 「学习状态评估」 / 「讲解规则」 / 「估计方法」 / 「提议方法」.
    `outcome` = the latest long-term verdict, `effect` = its series of measured points (oldest
    first, at most the newest 20), `effect_line` = one plain-Chinese line for the latest point
    (a change no longer applied says who rolled it back, never 「还在观察」). `can_undo` is False
    for a track-A patch while a newer accepted patch changed one of its values (undo that one
    first). Rows of an external source (track "G") carry whatever layer / target / outcome fields
    the source sets. `pages_waiting` (track B): how many pages this change marked out of date that
    are still waiting for their rewrite, which starts when the learner next opens each one;
    `pages_failed`: how many of the pages it marked stopped retrying after their rewrite failed
    twice under the current spec (still out of date; a later spec change re-arms them)."""

    patch_id: str
    track: ChangeTrack
    status: PatchStatus
    summary: str                                               # one plain-Chinese line
    why: str
    signals: tuple[str, ...]                                   # short quotes of what triggered it
    before: str = ""                                           # B: excerpt; A: "确认题预测误差 0.31"
    after: str = ""
    at: AwareDatetime
    can_undo: bool
    tool_live: bool = False                                    # H: a forged tool in force by this change
    # B, display only: `reading` = the system's understanding of the feedback, `kind` = problem type,
    # `rule_before` / `rule_after` = the rule before / after the change. `user_edited`: a record
    # accepted with the user's own wording before changes applied automatically.
    reading: str = ""
    kind: str = ""
    rule_before: str = ""
    rule_after: str = ""
    user_edited: bool = False
    layer: Literal[0, 1, 2] = 0
    target: str = ""
    outcome: Verdict = "pending"
    effect: tuple[OutcomeOut, ...] = ()                        # oldest first
    effect_line: str = ""
    pages_waiting: int = Field(default=0, ge=0)
    pages_failed: int = Field(default=0, ge=0)
