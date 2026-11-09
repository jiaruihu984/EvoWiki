"""GET /wiki/engine (the plugin's 参数 tab): the prompts each step really uses and θ_infer,u.

Nothing here calls an LLM. Prompt texts are rendered by the modules' own prompt builders (so they
stay verbatim when a prompt changes); the per-call parts (goal, page, K, the learner's words) are
filled with 〈…〉 placeholders. The model label of a step is derived from the runtime config the
service builds (`runtime_gateway_config` / `runtime_tier_configs` / `runtime_deep_config`) and the
tier `tier_for` resolves for this user — the same routing a real call takes. Only key presence is
read from the environment (as `runtime_deep_config` does), never a key."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import tzinfo
from types import SimpleNamespace

from evowiki.deep import agent as deep_agent
from evowiki.goals import blocks, intake, page_fit, reading_unit
from evowiki.goals.contracts import CellBelief
from evowiki.goals.guardrails import PageConcept, PageSpec
from evowiki.llm.models import ModelRef
from evowiki.llm.prompt_steps import strip_marks
from evowiki.llm.usage import Account, PurposeGroup
from evowiki.llm.usage_control import Tier, tier_for
from evowiki.runtime.llm import DEEPSEEK_NOTHINK, runtime_gateway_config
from evowiki.runtime.usage import runtime_deep_config, runtime_tier_configs
from evowiki.teach import infer_paths
from evowiki.teach.contracts import InferParams, PageFeedbackIn, ParamPatchOp
from evowiki.teach.rsi_store import PatchRecord
from evowiki.wikiapp import followup as followup_mod
from evowiki.wikiapp import review_cards, understand
from evowiki.wikiapp.llm import JSON_PROMPT_VERSION, purpose_group
from evowiki.wikiapp.wiki_schemas import CardFeedbackIn, EngineParamOut, EnginePromptOut

Role = str                       # "generator" | "verifier"
DEEP = "deep"                    # the deep tier: its own config, no fast / good routing


@dataclass(frozen=True)
class Call:
    """One LLM call a step makes: its purpose (→ group → tier), version id, role and text."""

    purpose: str
    ver: str
    role: Role
    text: str
    part: str = ""               # header suffix when one version id has several prompts


@dataclass(frozen=True)
class Step:
    step: str
    calls: tuple[Call, ...]
    vers: tuple[str, ...]        # version ids shown (may name a prompt owned by another step)
    note: str = ""               # one-line header + text for a version shown without its text


# ---- model labels (pure) ----

def ref_label(ref: ModelRef) -> str:
    """Human label of one configured model."""
    if ref.provider == "deepseek":
        return "DeepSeek · 好"
    if ref.provider == DEEPSEEK_NOTHINK:
        return "DeepSeek · 快"
    if ref.provider in ("anthropic", "anthropic-think"):
        m = ref.model.lower()
        for family, name in (("sonnet", "Sonnet"), ("haiku", "Haiku"), ("opus", "Opus")):
            if family in m:
                return f"Claude {name}"
        return f"Claude · {ref.model}"
    return f"{ref.provider} · {ref.model}"


def model_label(env: Mapping[str, str], tier: Tier | None, role: Role = "generator") -> str:
    """The model a call would really use: tier None = the deep tier (`runtime_deep_config`, the
    same provider as the runtime gateway); otherwise the runtime config, routed by tier only in
    DeepSeek mode (in anthropic mode every account keeps the one runtime config, as `build_services` wires it)."""
    if tier is None:
        config = runtime_deep_config(env)
    else:
        config = runtime_gateway_config(env)
        if config.single_provider:
            config = runtime_tier_configs(env)[tier]
    return ref_label(config.verifier if role == "verifier" else config.generator)


def call_tier(user_id: str, purpose: str, chosen: Mapping[str, Tier]) -> Tier | None:
    if purpose.startswith(DEEP):
        return None
    group: PurposeGroup = purpose_group(purpose)
    return tier_for(Account(user_id=user_id, group=group), purpose, chosen.get(group))


def step_model(env: Mapping[str, str], user_id: str, calls: Sequence[Call],
               chosen: Mapping[str, Tier]) -> str:
    """Distinct labels of the step's calls, in call order, joined with ' / '."""
    labels: list[str] = []
    for c in calls:
        label = model_label(env, call_tier(user_id, c.purpose, chosen), c.role)
        if label not in labels:
            labels.append(label)
    return " / ".join(labels)


# ---- prompt texts (rendered with placeholders) ----

LANG = "zh"
GOAL = "〈学习目标〉"
TITLE = "〈页面标题〉"
CONCEPT = "〈概念〉"
EVIDENCE = "〈系统对他这一格的判断，和他在这一格上的作答和记录〉"


class _NoK:
    """A KView with no cells: the outline part's K / cell-plan blocks render their empty form."""

    def aspects(self, concept_id: str) -> tuple:
        return ()

    def cell(self, cell: str) -> CellBelief:
        raise KeyError(cell)

    def cells(self, concept_ids: tuple[str, ...]) -> dict[str, CellBelief]:
        return {}

    def note(self, concept_id: str) -> tuple:
        return ()


def _outline() -> str:
    """The outline part of the writer's first message (a page without an outline; plan_page)."""
    spec = PageSpec(page_id="page", goal=GOAL,
                    concepts=(PageConcept(id="c1", name=CONCEPT),), targets=("c1",))
    return deep_agent.outline_part(spec, _NoK())  # type: ignore[arg-type]


# An attached material (S1, intake@v31): the prompts of the source steps.
SOURCE = intake.SourceToc(source_id="s1", title="〈资料的标题〉", sections=(
    intake.TocSection(id="x1", label="§1", title="〈第一节的标题〉", pages="p.1–3",
                      lead="〈这一节的第一段〉"),))


def _intake(sources: Sequence[intake.SourceToc] = (),
            use: intake.SourceUse = "learn", mode: blocks.MaterialMode = "follow") -> str:
    """S1 (intake@v31, goals/blocks.py) as the 参数 steps show its guidance: with a material
    (按这份学, or only a reference: `mode`) or the one to read (读懂这篇), without the chat card's
    reply lines."""
    return blocks.blocks_prompt(GOAL, ("〈他为这个目标说的话〉",), (), LANG, sources=sources,
                                use=use, mode=mode, reply=False)


def _enter() -> str:
    """Entering a block (S2, goals.enter, enter@v2, goals/intake.py): its one-level split and, with
    his own words, the estimate of what he knows — one call."""
    scope = intake.IntakeScope(goal=GOAL, region_id="c1", region_name="〈这一块〉",
                               scope="〈这一块包含的小主题〉", parent_goal=GOAL,
                               siblings=(("〈这一块〉", "〈这一块包含的小主题〉"),
                                         ("〈另一块〉", "〈另一块包含的小主题〉")))
    return intake._outline_prompt(GOAL, (), scope, LANG, limits=("〈他为这个目标提的条件〉",),
                                  words=("〈他关于这一块说的原话〉",))


def _blocks() -> str:
    """S1 for the chat card (intake@v31, goals/blocks.py): first level only, with covers, the
    reply lines and the goal record."""
    return blocks.blocks_prompt(GOAL, ("〈他为这个目标说的话〉",), (), LANG)


def _page_fit() -> str:
    """Whether an existing page fits another goal's point (page-fit@v3, goals/page_fit.py)."""
    return page_fit.fit_prompt(goal="〈这个目标〉", point="〈知识点〉", about="〈它是什么〉", written_for="〈写这一页时的目标〉",
                               page=page_fit.PageText(title="〈页面标题〉", profile="", sections=(("〈小节〉", "〈正文〉"),)),
                               language=LANG)


def _reading_unit() -> str:
    """「看这一段」 (reading-unit@v1, goals/reading_unit.py): one unit of a 读懂 goal entered."""
    return reading_unit.unit_prompt(reading_unit.UnitInput(
        title="〈资料的标题〉", text="〈这一段的原文〉", order=2,
        units=(reading_unit.UnitRow(order=1, title="〈第一段的标题〉", teaches=("〈它讲过的点〉",)),
               reading_unit.UnitRow(order=2, title="〈这一段的标题〉", about="〈这一段大致讲什么〉")),
        blocks=(("c1", "〈前置块〉"),), language=LANG))



def _understand() -> str:
    ctx = understand.Context(page_id="page", title=TITLE,
                             sections=(("s1", "〈小节标题〉", "〈小节开头〉"),),
                             concepts={"c1": CONCEPT}, bands=("〈系统对他的判断〉",),
                             rules=("〈这一页遵守的讲法规则〉",),
                             components={"〈组件 id〉": "〈组件说明〉"})
    return understand.SYSTEM + "\n\n" + understand.prompt(ctx, PageFeedbackIn(text="〈他写的话〉"))


def _state() -> SimpleNamespace:
    return SimpleNamespace(language=LANG, names={"c1": SimpleNamespace(name=CONCEPT)})


def _card(check: bool) -> SimpleNamespace:
    item = SimpleNamespace(cell="c1.term", stem="〈现在的题干〉", options=["〈选项〉", "〈选项〉", "〈选项〉"],
                           answer=0)
    section = SimpleNamespace(id="s1", heading="〈小节标题〉", check=item if check else None)
    text = SimpleNamespace(body="〈这一段的正文〉", cue="〈现在的回想提示〉", answer="〈现在的答案〉")
    return SimpleNamespace(rec=SimpleNamespace(title=TITLE), section=section,
                           page=SimpleNamespace(texts={"s1": text},
                                                outline=SimpleNamespace(sections=())))


def _card_cue() -> str:
    body = CardFeedbackIn(text="〈他的话〉")                # the card box sends own words only
    return review_cards._cue_prompt(_state(), _card(False), body)  # type: ignore[arg-type]


def _card_check() -> str:
    body = CardFeedbackIn(text="〈他的话〉")
    return review_cards._check_prompt(_state(), _card(True), body,  # type: ignore[arg-type]
                                      EVIDENCE)


def _followup() -> str:
    item = SimpleNamespace(stem="〈原题题干〉", options=["〈选项〉", "〈选项〉", "〈选项〉"])
    teach = (SimpleNamespace(page=SimpleNamespace(texts={"s1": SimpleNamespace(body="〈相关正文〉")})),
             SimpleNamespace(id="s1"))
    return followup_mod._confirm_prompt(_state(), SimpleNamespace(title=TITLE),  # type: ignore[arg-type]
                                        item, "c1.term", teach,  # type: ignore[arg-type]
                                        picked="他选的：〈他选的选项〉",
                                        claim="〈他说为什么答错：粗心 / 学会了〉",
                                        evidence=EVIDENCE)


def steps() -> tuple[Step, ...]:
    """The six steps of the 参数 tab, in the demo's order and names."""
    jv = JSON_PROMPT_VERSION
    deep = deep_agent.PROMPT_VERSION
    return (
        # The goal flow (wikiapp/goal.py): S1 (one call per goal: the blocks and the goal
        # record); entering a block: one call (S2) splits it and estimates from his words. The
        # diagnosis's questions are the assistant's (ask_check in the chat, stage 3 T11): no call
        # of their own.
        Step("拆目标和摸底", (
            Call(intake.OUTLINE_PURPOSE, jv[intake.OUTLINE_PURPOSE], "generator", _blocks(),
                 "分块"),
            Call(intake.ENTER_PURPOSE, jv[intake.ENTER_PURPOSE], "generator", _enter(),
                 "进入一块"),
            Call(page_fit.PURPOSE, jv[page_fit.PURPOSE], "generator", _page_fit(), "已有页面能否复用"),
            ),
            (jv[intake.OUTLINE_PURPOSE], jv[intake.ENTER_PURPOSE], jv[page_fit.PURPOSE])),
        # the writer outlines a page in its own loop (plan_page, stage 4): 列大纲 shows the outline
        # part of its first message
        Step("列大纲", (Call("deep.write", deep, "generator", _outline()),), (deep,)),
        Step("写每一段", (Call("deep.write", deep, "generator", deep_agent.WRITE_SYSTEM),),
             (deep,)),
        Step("出确认题", (
            Call("deep.write", deep, "generator", ""),
            Call(followup_mod.PURPOSE, jv[followup_mod.PURPOSE], "generator", _followup())),
            (deep, jv[followup_mod.PURPOSE]),
            note="嵌入页面的确认题由写作者在列大纲时一起出（role=check 的段），见上。"),
        Step("理解你的反馈", (Call(understand.PURPOSE, understand.PROMPT_VERSION, "generator",
                                  _understand()),), (understand.PROMPT_VERSION,)),
        Step("复习卡", (
            Call(review_cards.PURPOSE, jv[review_cards.PURPOSE], "generator", _card_cue(), "回想提示"),
            Call(review_cards.PURPOSE, jv[review_cards.PURPOSE], "generator", _card_check(),
                 "确认题")),
            (jv[review_cards.PURPOSE],)),
    )


def step_text(s: Step) -> str:
    """One prompt: its text. Several: each under a one-line header 【<ver>[ · <part>]】, separated
    by a blank line. A call with no text (shown elsewhere) prints the step's note instead. Guidance
    markers (llm/prompt_steps.py) are removed: the text is what the model reads by default."""
    blocks = [(c.ver + (f" · {c.part}" if c.part else ""), strip_marks(c.text) or s.note)
              for c in s.calls]
    if len(blocks) == 1:
        return blocks[0][1]
    return "\n\n".join(f"【{head}】\n{text}" for head, text in blocks)


def prompts_out(env: Mapping[str, str], user_id: str,
                chosen: Mapping[str, Tier]) -> list[EnginePromptOut]:
    return [EnginePromptOut(step=s.step, ver=" · ".join(s.vers),
                            model=step_model(env, user_id, s.calls, chosen), text=step_text(s))
            for s in steps()]


# ---- θ_infer rows ----

_SCALES = frozenset({"forgetting_scale", "guess_scale"})
_DEFAULT = InferParams()


def fmt_value(path: str, v: float) -> str:
    """Weights / probabilities like "0.6"; multipliers like "× 1.0" (at least one decimal)."""
    s = f"{v:.3f}".rstrip("0")
    s = s + "0" if s.endswith(".") else s
    return f"× {s}" if path in _SCALES else s


def param_row(path: str, value: float, patches: Sequence[PatchRecord],
              tz: tzinfo) -> EngineParamOut:
    """Structured (the client words it): `default` when the value equals the default; else
    `tuned_at` (ISO date in `tz`) and `why` from the latest live track-A patch that set this path,
    both None when no such patch is found."""
    row = EngineParamOut(path=path, label=infer_paths.PATH_LABEL[path],
                         description=infer_paths.PATH_DESCRIPTION[path],
                         value=fmt_value(path, value),
                         default=value == infer_paths.get(_DEFAULT, path))
    if row.default:
        return row
    live = [r.patch for r in patches
            if r.patch.track == "A" and r.patch.status == "personal_accepted"
            and isinstance(r.patch.body, ParamPatchOp) and r.patch.body.path == path]
    if not live:
        return row
    p = max(live, key=lambda x: x.created_at)
    return row.model_copy(update={"tuned_at": p.created_at.astimezone(tz).date().isoformat(),
                                  "why": p.why})


def params_out(infer: InferParams, patches: Sequence[PatchRecord],
               tz: tzinfo) -> list[EngineParamOut]:
    return [param_row(path, infer_paths.get(infer, path), patches, tz)
            for path in infer_paths.PATHS]
