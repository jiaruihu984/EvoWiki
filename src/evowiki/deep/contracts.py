"""Deep refine shared contracts (plan rev 3.3 §10.2–10.3). Pure data and two Protocols — no IO, no LLM.

A page is first written fast (the skeleton, Phase 1). Deep refine then rewrites it in the background with
an agent: plan from K → write sections and interactives, then self-check them against the user's
teaching spec in the same loop (recorded, not a gate) → render-check every interactive in a headless
browser (a gate: broken interactives are fixed or dropped) → commit once through the existing minimal
patch (fold, never cut). Lanes build against these types in parallel:
  D1 (backend)  runs the agent, the render check, the asset store and the routes;
  UI windows    render interactives (sandboxed iframe / Obsidian `embedded-html`) from the HTTP shapes;
  R3 (RSI-B)    reruns refine on single sections to compare teaching-spec versions (`RefineScope`).
Changing a type here is a cross-lane change: do it in its own commit and tell the other lanes."""

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from evowiki.deep.evoui import app_images
from evowiki.deep.evoui.contracts import BlockDraft, EvoUIBlock, block_placeholder
from evowiki.goals.contracts import SectionRole
from evowiki.teach.standard import CHECKLIST


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


Depth = Literal["quick", "example", "interactive"]
"""How deep one section goes (the user's own wiki: teaching_depth). quick = one plain conclusion and the
direct result; example = + a minimal runnable / observable example; interactive = + an interactive, only
when space, order, state or data flow genuinely needs it, or when what the section teaches must be
remembered or done and is practised on the page (standard@v9)."""


# ---- planning (step 1; K enters here) ----

class RefinePlan(_M):
    """The writer's plan: the spoken answer, the order things arrive, practice and interactives.
    `known` is written from K's known / likely cells and the profile, never guessed.
    `observable` / `contrast` (deep@v31): optional — a concrete example or a contrast only when the
    topic has one (they were the old seven-step shape's 普通做法 vs 新做法); plans stored before keep
    their text."""

    known: str = Field(min_length=1, max_length=1200)          # what the learner already knows
    observable: str = Field(default="", max_length=1200)       # a concrete example / result that shows it
    one_at_a_time: tuple[str, ...] = Field(min_length=1, max_length=12)   # the order new things arrive
    contrast: str = Field(default="", max_length=1200)         # a contrast that helps tell things apart
    spoken_answer: str = Field(min_length=1, max_length=4000)  # "if he asked you directly"
    # what the learner must remember or be able to do after the page, and how he practises it on the
    # page (retrieval / worked example then practice), or why nothing needs practice (deep@v14).
    # Plans stored before this field parse as "".
    practice: str = Field(default="", max_length=1200)
    # one entry per planned interactive: its use (看懂 / 练熟 / 辨析), the learning question it answers
    # and why text is not enough. Empty = no interactive needed. Plans stored before this field parse
    # as empty.
    visual_questions: tuple[str, ...] = Field(default=(), max_length=6)


class PlanAnswers(BaseModel):
    """The model's part of `RefinePlan` (`known` is code's): the `plan` of the writer's plan_page
    tool (deep@v28). The descriptions are what the model reads about each field."""

    model_config = ConfigDict(extra="forbid")

    observable: str = Field(default="", max_length=1200, description=(
        "（可选）打算用哪个具体的例子、计算或现象让他看到这个知识在起作用；不需要就留空"))
    one_at_a_time: list[str] = Field(min_length=1, max_length=12, description=(
        "内容按什么顺序逐步呈现，让前面的解释帮助理解后面的内容；每项一句，末尾用（quick / example / "
        "interactive）标出这一步讲多深；他已经会的别列"))
    contrast: str = Field(default="", max_length=1200, description=(
        "（可选）有没有一组对照（对 vs 错、两种情况、改前改后）能帮他分清容易混的地方；不需要就留空"))
    spoken_answer: str = Field(min_length=1, max_length=4000, description=(
        "如果他当面问你，你会怎样一句一句讲给他听；正文按这个讲解思路写"))
    practice: str = Field(min_length=1, max_length=1200, description=(
        "读完这一页他要记住或会做的是什么，打算怎样让他当场回想或动手做一次、马上知道对错"
        "（例如「45 个假名 → 闪卡接分页测验」）；没有要练的写「不用当场练：」加原因。"
        "确认题段不算练习"))
    visual_questions: list[Annotated[str, Field(min_length=1, max_length=400)]] = Field(
        default_factory=list, max_length=6, description=(
            "这一页每个交互一项（看懂的只是建议，画不画由可视化那一步定）：「看懂｜看不见的：……｜想让他看到：……」；"
            "练习写「练熟或辨析｜要练的：……」。"))


# ---- interactives (steps 3–4) ----

# A placed library component's HTML (component + config). 600 kB: a component may inline a vendored library
# (components/vendor, e.g. Cytoscape.js ~370 kB for network-edit); model-written interactives are EvoUI blocks.
INTERACTIVE_MAX_BYTES = 600_000
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


class Probe(_M):
    """One scripted try of a control. The render check performs `action` on `target` and expects the
    text of `expect_change` to differ afterwards (the "operate it once" rule)."""

    target: str = Field(min_length=1, max_length=200)          # CSS selector of the control
    action: Literal["click", "input", "select"]
    value: str | None = Field(default=None, max_length=200)    # input / select only
    expect_change: str = Field(min_length=1, max_length=200)   # CSS selector of the result region

    @model_validator(mode="after")
    def _value(self) -> "Probe":
        if (self.action == "click") != (self.value is None):
            raise ValueError("value is required for input/select and forbidden for click")
        return self


class InteractiveDraft(_M):
    """A standalone UTF-8 HTML document: inline CSS/JS only, no network, no external fonts/scripts.
    Reads host theme from CSS variables / prefers-color-scheme; encodes meaning with labels, shapes or
    position, never colour alone."""

    interactive_id: str                                        # page-unique, kebab-case
    section_id: str
    title: str = Field(min_length=1, max_length=120)
    learning_question: str = Field(min_length=1, max_length=300)   # the question this visual answers
    html: str = Field(min_length=1, max_length=INTERACTIVE_MAX_BYTES)
    height: int = Field(ge=400, le=1600)                       # same bounds as the user's embedded-html
    probes: tuple[Probe, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _id(self) -> "InteractiveDraft":
        if not _ID.match(self.interactive_id):
            raise ValueError("interactive_id must be kebab-case [a-z0-9-], 2–64 chars")
        return self


CONFIG_ELEMENT_ID = "evowiki-config"
"""A configurable component reads its config from `<script type="application/json" id="evowiki-config">`,
injected into its HTML when it is placed (`deep.components.instantiate`)."""


class LibraryComponent(_M):
    """A registered interactive (B5): the understanding call and deep refine may only place these.
    Registered only after a passing render check; `params` documents what the reader can adjust
    (name → what it does).

    Two kinds:
    - fixed-content (`config_schema` None): one topic, the HTML is placed as it is (`check`);
    - configurable: one generic interaction (a distribution on a number line, a step-through process,
      …) that teaches any topic from a config. `config_schema` is a JSON Schema in the subset Anthropic
      structured outputs accept (`deep.components.check_schema`); whoever places it must give a config
      that validates (else nothing is placed — fail closed), and the render check runs on component +
      that config. `example_configs` (≥2, different topics) each passed the render check
      (`example_checks`, same order; `check` is the first). Probe targets/values may reference the
      config as `{{config.path}}` (`deep.components.resolve_probes`)."""

    component_id: str
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)     # what it shows and lets you do
    learning_question: str = Field(min_length=1, max_length=300)
    params: dict[str, str] = Field(default_factory=dict)
    html: str = Field(min_length=1, max_length=INTERACTIVE_MAX_BYTES)
    height: int = Field(ge=400, le=1600)
    probes: tuple[Probe, ...] = Field(min_length=1, max_length=8)
    check: "RenderCheck"
    source: str = Field(default="", max_length=200)            # where it came from
    scenarios: tuple[str, ...] = ()                            # topics it fits (for the catalog)
    config_schema: dict[str, Any] | None = None                # None = fixed-content component
    example_configs: tuple[dict[str, Any], ...] = ()
    example_checks: tuple["RenderCheck", ...] = ()

    @model_validator(mode="after")
    def _ok(self) -> "LibraryComponent":
        if not _ID.match(self.component_id):
            raise ValueError("component_id must be kebab-case [a-z0-9-], 2–64 chars")
        if not self.check.ok or self.check.interactive_id != self.component_id:
            raise ValueError("only a component whose own render check passed can be registered")
        if self.config_schema is None:
            if self.example_configs or self.example_checks:
                raise ValueError("a fixed-content component takes no example configs")
            return self
        if len(self.example_configs) < 2:
            raise ValueError("a configurable component needs at least 2 example configs")
        if len(self.example_checks) != len(self.example_configs) or not all(
                c.ok and c.interactive_id == self.component_id for c in self.example_checks):
            raise ValueError("every example config must have passed the render check")
        return self

    @property
    def configurable(self) -> bool:
        return self.config_schema is not None


class RenderCheck(_M):
    """Deterministic result of opening one interactive in a headless browser (code, not model)."""

    interactive_id: str
    ok: bool
    console_errors: tuple[str, ...] = ()
    page_errors: tuple[str, ...] = ()        # uncaught exceptions
    network_requests: int = 0                # anything > 0 fails: interactives are offline
    overflow_px: int = 0                     # document wider than the frame at 375 px and at desktop
    probes_passed: int = 0
    probes_total: int = 0
    detail: str = Field(default="", max_length=1000)
    # what the screen said: the block's text at first, then what each trial operation changed
    readings: tuple[str, ...] = ()


class RenderChecker(Protocol):
    def check(self, draft: InteractiveDraft) -> RenderCheck: ...


class AppProbe(_M):
    """One operation the visual's author says shows the idea (`deep.visual`): performed in order inside
    the app's frame, photographed after each."""

    target: str = Field(min_length=1, max_length=200)          # CSS selector inside the app
    action: Literal["click", "input", "drag"] = "click"
    value: str | None = Field(default=None, max_length=200)    # input: the value
    by: tuple[int, int] | None = None                          # drag: how far, in px
    see: str = Field(default="", max_length=300)               # what should show afterwards


class BlockLook(_M):
    """What a block really looks like (`BlockChecker.look_block`): JPEGs in base64 by what they show, and what
    happened to each probe."""

    shots: tuple[tuple[str, str], ...] = ()
    notes: tuple[str, ...] = ()


class BlockChecker(Protocol):
    """Render check of an EvoUI block (`deep.evoui.contracts.EvoUIBlock`) through `evoui.js`: no errors,
    no network, no overflow at 375 / 1280 px, and its probes (or two values per control) change what the
    block shows. `interactive_id` of the result is `block_id`. `images`: what an app's `evowiki.image`
    answers, image id -> WebP in base64. `touch`: the operations the app's author says show the idea, tried first."""

    def check_block(self, block_id: str, block: "EvoUIBlock", images: Mapping[str, str] | None = None,
                    touch: Sequence[AppProbe] = ()) -> RenderCheck: ...

    def look_block(self, block_id: str, block: "EvoUIBlock", probes: Sequence[AppProbe],
                   images: Mapping[str, str] | None = None) -> BlockLook: ...


# ---- images (find_image / place_image, deep@v24) ----

IMAGE_FENCE = "evowiki-image"
IMAGE_MAX_B64 = 2_100_000                 # the stored WebP (deep.images.MAX_IMAGE_BYTES) in base64


class ImageDraft(_M):
    """An openly licensed image the writer placed (`{{image:<image_id>}}` in a body), downloaded and
    re-encoded server side (`deep.images`). The note shows it with its attribution (title · author ·
    licence · source) in small grey text under it; the bytes become a wiki asset of kind "image"."""

    image_id: str
    section_id: str
    alt: str = Field(min_length=1, max_length=200)            # what the image shows, for the reader
    title: str = Field(min_length=1, max_length=200)
    author: str = Field(default="", max_length=160)
    license: str = Field(min_length=1, max_length=40)
    license_url: str = Field(default="", max_length=300)
    source: str = Field(min_length=1, max_length=60)           # the library ("Wikimedia Commons")
    source_url: str = Field(min_length=1, max_length=500)      # the file's page
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    data_b64: str = Field(min_length=1, max_length=IMAGE_MAX_B64, repr=False)   # image/webp

    @model_validator(mode="after")
    def _id(self) -> "ImageDraft":
        if not _ID.match(self.image_id):
            raise ValueError("image_id must be kebab-case [a-z0-9-], 2–64 chars")
        return self


def image_placeholder(image_id: str) -> str:
    return f"{{{{image:{image_id}}}}}"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def image_fence(asset_id: str, img: ImageDraft) -> str:
    """The note form of a placed image: a ```evowiki-image fence of `key: value` lines (the asset
    holds the bytes; the plugin saves them as `assets/<asset_id>.webp` and draws the image with its
    attribution line)."""
    rows = [("asset", asset_id), ("alt", img.alt), ("title", img.title), ("author", img.author),
            ("license", img.license), ("license_url", img.license_url), ("source", img.source),
            ("source_url", img.source_url), ("width", str(img.width)),
            ("height", str(img.height))]
    body = "\n".join(f"{k}: {_one_line(v)}" for k, v in rows if v)
    return f"```{IMAGE_FENCE}\n{body}\n```"


# ---- writing and review (steps 2, 5) ----

class RefinedSection(_M):
    """One section of the refined page. `section_id` of an existing section rewrites it (its fold state
    and recall cue rules are kept); a new id is inserted after `after`."""

    section_id: str
    after: str | None = None
    heading: str = Field(min_length=1, max_length=120)
    role: SectionRole
    depth: Depth
    cells: tuple[str, ...] = ()
    cue: str = Field(min_length=1, max_length=60)              # recall cue shown when folded
    answer: str = Field(default="", max_length=200)            # the cue's answer on the review card
    body: str = Field(min_length=1, max_length=20_000)         # markdown; interactives by `{{interactive:<id>}}`
    simplifications: tuple[str, ...] = ()                      # stated on the page ("这里简化了…")


class ReviewFinding(_M):
    rule: str = Field(min_length=1, max_length=80)   # a teaching-spec item_id or a builtin key below
    section_id: str | None = None
    ok: bool
    note: str = Field(default="", max_length=500)


# The rules every self-check covers besides the teaching-spec items in force for the page: the
# teaching standard's six dimensions (CHECKLIST, `teach.standard`; deep@v31 — they replace c01–c18
# and the seven extra rules, whose points they cover: single-answer checks under accuracy, never
# claiming what K says he lacks under fits_learner).
BUILTIN_REVIEW_RULES = tuple(c.id for c in CHECKLIST)

class RefineDraft(_M):
    """`interactives`: library components placed (`{{interactive:<id>}}`); `blocks`: EvoUI blocks the
    writer composed (`{{block:<id>}}`); `images`: openly licensed images (`{{image:<id>}}`). Ids are
    unique across all three."""

    page_id: str
    plan: RefinePlan
    sections: tuple[RefinedSection, ...] = Field(min_length=1)
    interactives: tuple[InteractiveDraft, ...] = ()
    blocks: tuple[BlockDraft, ...] = ()
    images: tuple[ImageDraft, ...] = ()

    @property
    def needs_assets(self) -> bool:
        """The commit stores assets (an asset store is required)."""
        return bool(self.interactives or self.blocks or self.images)

    @model_validator(mode="after")
    def _refs(self) -> "RefineDraft":
        ids = [s.section_id for s in self.sections]
        if len(set(ids)) != len(ids):
            raise ValueError("section ids repeat")
        iids = [i.interactive_id for i in self.interactives]
        if len(set(iids)) != len(iids):
            raise ValueError("interactive ids repeat")
        bodies = "\n".join(s.body for s in self.sections)
        for i in self.interactives:
            if i.section_id not in ids:
                raise ValueError(f"interactive {i.interactive_id} points at an unknown section")
            if f"{{{{interactive:{i.interactive_id}}}}}" not in bodies:
                raise ValueError(f"interactive {i.interactive_id} is never placed in a body")
        bids = [b.block_id for b in self.blocks]
        if len(set(bids)) != len(bids) or set(bids) & set(iids):
            raise ValueError("block ids repeat (or clash with an interactive id)")
        for b in self.blocks:
            if b.section_id not in ids:
                raise ValueError(f"block {b.block_id} points at an unknown section")
            if block_placeholder(b.block_id) not in bodies:
                raise ValueError(f"block {b.block_id} is never placed in a body")
        mids = [m.image_id for m in self.images]
        if len(set(mids)) != len(mids) or set(mids) & (set(bids) | set(iids)):
            raise ValueError("image ids repeat (or clash with an interactive / block id)")
        in_apps = {im.get("asset") for b in self.blocks for im in app_images(b.block)}
        for m in self.images:
            if m.section_id not in ids:
                raise ValueError(f"image {m.image_id} points at an unknown section")
            if m.image_id not in in_apps and image_placeholder(m.image_id) not in bodies:
                raise ValueError(f"image {m.image_id} is never placed in a body")
        return self


# ---- jobs ----

# "rsi": the lazy rewrite of a page an RSI change left out of date, started when it is opened;
# "reorder": the same for a page written for a path order that has changed since;
# "harness_replay": a dry one-section replay of a past run under a candidate pack (harness/replay.py);
# "harness": the lazy rewrite of a page a harness RSI change marked (harness/round.py);
# "patch": the scoped write of the sections a K-change patch left waiting (wikiapp/stale.py
# `patch_pending`; those sections are required, `DeepService.run`)
RefineTrigger = Literal["auto_first_page", "user", "rsi", "reorder", "harness_replay",
                        "harness", "patch"]
# "review" (isolated review), "render_failed" and "review_failed" are no longer produced; they stay so
# job snapshots stored before deep-refine-2 still parse. "no_draft": the writer never finished a draft.
# "plan" is no longer produced either (stage 4: the plan is plan_page, inside "write"); a job stored
# while it planned still parses.
RefineStage = Literal["queued", "plan", "write", "render", "review", "commit", "done", "failed",
                      "cancelled"]
# llm_busy: the provider was overloaded (429 / queued); llm_timeout: the request timed out; the
# detail of either is the text to show (「请切换模型重试」, 「请切换模型重试」)
RefineFailure = Literal["budget_exceeded", "llm_unavailable", "llm_busy", "llm_timeout",
                        "render_failed",
                        "review_failed",
                        "no_draft", "page_changed", "internal", "cancelled"]


class RefineScope(_M):
    """What to refine. `sections` empty = the whole page. `dry_run` = return the draft, commit nothing
    (RSI-B's Gate uses it to compare two teaching-spec versions)."""

    page_id: str
    sections: tuple[str, ...] = ()
    dry_run: bool = False
    spec_version: int | None = None          # None = the user's current teaching spec


class DroppedInteractive(_M):
    """An interactive that still failed the render check after the last write round: taken out of the
    draft (its placeholder removed), the section text committed without it."""

    interactive_id: str
    section_id: str
    reason: str = Field(max_length=1000)


class RefineJob(_M):
    job_id: str
    page_id: str
    trigger: RefineTrigger
    stage: RefineStage
    round: int = Field(default=0, ge=0, le=3)                 # write rounds used (max 2 rewrites)
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    cost_usd: float = Field(default=0.0, ge=0.0)
    model: str = ""
    spec_version: int | None = None
    failure: RefineFailure | None = None
    failure_detail: str = Field(default="", max_length=500)   # for logs; UIs show their own copy
    changed_sections: tuple[str, ...] = ()
    interactives: tuple[str, ...] = ()                        # asset ids committed
    # the writer's own check at `finish`, one finding per rule (BUILTIN_REVIEW_RULES + the spec items in
    # force); for viewing only, never a reason to drop the draft
    self_check: tuple[ReviewFinding, ...] = ()
    dropped: tuple[DroppedInteractive, ...] = ()


class RefineSettings(_M):
    """The page writer's per-page spending cap. `auto` (first_page / all / manual: when a deep refine
    followed the draft) is gone with the single writer (2026-10-10); stored settings that still carry
    it parse and drop it."""

    # None = no per-page cap (the default while in development; the user may set one)
    per_page_limit_usd: float | None = Field(default=None, ge=0.05)

# ---- assets and HTTP shapes ----

class AssetMeta(_M):
    """`kind` html: a standalone HTML interactive (library component), served as text/html; `kind`
    evoui: an EvoUI block, served as application/json (its JSON is also inline in the note's ```evoui
    fence); `kind` image: an image/webp, served as its base64 text (text/plain; `sha256` is of that
    text), its attribution in the note's ```evowiki-image fence. `height` is the frame height for
    html; for evoui and image it is the minimum 400 (unused)."""

    asset_id: str
    page_id: str
    section_id: str
    interactive_id: str                                        # component id / placement id / block id
    title: str
    height: int = Field(ge=400, le=1600)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    check: RenderCheck
    kind: Literal["html", "evoui", "image"] = "html"


class RefineJobOut(BaseModel):
    """GET /wiki/pages/{id}/refine (latest job) and POST /wiki/pages/{id}/refine (202, starts one).
    Assets: GET /wiki/assets/{asset_id} → the HTML, served with a CSP that forbids network and a
    `sandbox allow-scripts` header; GET /wiki/assets/{asset_id}/meta → AssetMeta."""

    job: RefineJob | None
    assets: tuple[AssetMeta, ...] = ()
    # the last draft of a job that ended without committing it (failed): read-only, the page keeps
    # its text. None when the latest job committed, is still running, or never got that far.
    last_draft: RefineDraft | None = None


EMBED_FENCE = "embedded-html"


def embed_block(asset: AssetMeta, src: str) -> str:
    """The Obsidian code block for one interactive, in the same format as the user's own wiki
    (type / id / src / title / height), so either plugin can render it."""
    title = asset.title.replace("\n", " ")
    return (f"```{EMBED_FENCE}\ntype: {EMBED_FENCE}\nid: {asset.interactive_id}\nsrc: {src}\n"
            f"title: {title}\nheight: {asset.height}\n```")


LibraryComponent.model_rebuild()
