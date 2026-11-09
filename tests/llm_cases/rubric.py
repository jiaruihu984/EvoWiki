"""Rubric judging for page / answer quality (spec the product specification
二 页面质量评估, TutorBench). Test-only harness, not product code (so not in the inventory scan):

- `parse_rubrics`: a TutorBench RUBRICS value → `RubricItem`s. Primary shape (the dataset's): a
  string holding a JSON array of `{"attributes": {explicitness, objectivity, severity,
  tutoring_skill, eval_dimension}, "criteria": "..."}`. Also accepted: a JSON list of strings, a
  list of objects with a criterion-like text field (weight / polarity kept), or a string holding
  either. Anything else raises `RubricFormatError` (never guessed).
- `Case`: one tutoring case in the TutorBench shape (learner question, prior explanation,
  follow-up, rubric); `tutorbench_cases` loads the fetched rows (image rows skipped and counted)
  and splits them by flow (FLOW_FIELDS: every text-only row feeds the assistant flow, only rows
  with UC1_INITIAL_EXPLANATION the rewrite flow; a row's missing field is recorded as a FlowSkip),
  `zh_cases` the hand-written Chinese cases (tests/llm_cases/fixtures/rubric_zh.json).
- `judge(gateway, case, response, *, language)`: ONE LLM call (purpose `llm_cases.rubric_judge`)
  → yes / no plus a one-line reason per rubric item; a reply that does not cover every item
  exactly once raises `RubricJudgeError` (a parse failure is the Gateway's LLMParseError). Both
  fail loudly. `score(results)`: fraction passed, critical-item pass rate, per-item results.
- The product calls being judged: `assistant_answer` (assistant.turn, the follow-up turn with the
  question + explanation as the earlier turn; a wrong-answer quote as the selection) and
  `rewrite_section` (「换个讲法」 as the product runs it: the deep single writer, deep@v12, in
  feedback mode scoped to the one section with `feedback_instruction("other", heading)`, the
  RefineInput `wikiapp.kchange._rewrite` builds, over a MemoryRepo seeded with a one-section page
  whose text is the explanation). The product's 换个讲法 button carries no learner text, so the
  follow-up has no product slot: it is appended to that instruction as one labelled line
  (FOLLOW_UP_LINE), the only difference from the product's request.
- `run_cases` runs both flows per case and records every failure with its cause (an assistant
  turn ending in an error event: `AssistantTurnError` carrying the event's code / retryable /
  detail); `resume_from(earlier report, cases)` + `run_cases(resume=...)` keep the earlier scored
  (case, flow) pairs and run only the errored / missing ones (a changed case id set starts
  fresh); `error_summary` counts errors by kind for an assertion message."""

import importlib.util
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from evowiki.goals.language import LANGUAGE_NAMES, Language
from evowiki.llm.models import LLMRequest

PURPOSE = "llm_cases.rubric_judge"
PROMPT_VERSION = "rubric-judge@v1"
FRESH_ENV = "EVOWIKI_TUTORBENCH_FRESH"
SYSTEM = "你是严格的辅导质量评分员。只输出要求的 JSON。"
TEXT_FIELDS = ("criteria", "criterion", "description", "text", "rubric")
KEPT_FIELDS = ("weight", "polarity")
ROOT = Path(__file__).resolve().parents[2]
TUTORBENCH_ROWS = ROOT / "tests" / "llm_cases" / "data" / "tutorbench" / "rows.json"
ZH_CASES = ROOT / "tests" / "llm_cases" / "fixtures" / "rubric_zh.json"
CaseKind = Literal["assistant", "rewrite", "both"]
FETCH_SCRIPT = ROOT / "scripts" / "fetch_tutorbench.py"


def fetch_script():
    """scripts/fetch_tutorbench.py as a module, loaded by path (under pytest `scripts` resolves
    to tests/scripts, so a package import would get the wrong one)."""
    name = "evowiki_fetch_tutorbench"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, FETCH_SCRIPT)
        assert spec is not None and spec.loader is not None, FETCH_SCRIPT
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


def has_image(row: dict[str, Any]) -> bool:
    return fetch_script().has_image(row)


class RubricFormatError(ValueError):
    """A RUBRICS value (or a fixture case) in a shape this harness does not know."""


class RubricJudgeError(ValueError):
    """The judge's reply does not give exactly one verdict per rubric item."""


@dataclass(frozen=True)
class RubricItem:
    text: str
    attributes: dict[str, str] = field(default_factory=dict)
    weight: float | None = None
    polarity: str | None = None

    @property
    def severity(self) -> str | None:
        return self.attributes.get("severity")

    @property
    def critical(self) -> bool:
        return (self.severity or "").lower() == "critical"


def _item(x: Any, i: int) -> RubricItem:
    if isinstance(x, str):
        if not x.strip():
            raise RubricFormatError(f"rubric item {i} is an empty string")
        return RubricItem(text=x.strip())
    if not isinstance(x, dict):
        raise RubricFormatError(f"rubric item {i} is a {type(x).__name__}, not a string or object")
    key = next((k for k in TEXT_FIELDS if isinstance(x.get(k), str) and x[k].strip()), None)
    if key is None:
        raise RubricFormatError(f"rubric item {i} has no criterion text (keys: {sorted(x)})")
    attrs = x.get("attributes", {})
    if not isinstance(attrs, dict):
        raise RubricFormatError(f"rubric item {i}: attributes is not an object")
    weight = x.get("weight")
    if weight is not None and (isinstance(weight, bool) or not isinstance(weight, int | float)):
        raise RubricFormatError(f"rubric item {i}: weight {weight!r} is not a number")
    polarity = x.get("polarity")
    return RubricItem(text=x[key].strip(), attributes={str(k): str(v) for k, v in attrs.items()},
                      weight=float(weight) if weight is not None else None,
                      polarity=str(polarity) if polarity is not None else None)


def parse_rubrics(raw: Any) -> tuple[RubricItem, ...]:
    value = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RubricFormatError(f"RUBRICS string is not JSON: {exc}") from exc
    if not isinstance(value, list):
        raise RubricFormatError(f"RUBRICS is a {type(value).__name__}, not a list")
    if not value:
        raise RubricFormatError("RUBRICS is an empty list")
    return tuple(_item(x, i) for i, x in enumerate(value, 1))


@dataclass(frozen=True)
class Case:
    case_id: str
    subject: str
    prompt: str                      # the learner's first question
    initial_explanation: str         # what the tutor explained first
    follow_up: str                   # the learner's follow-up
    rubrics: tuple[RubricItem, ...]
    kind: CaseKind = "both"
    wrong_quote: str | None = None   # a quoted wrong check (wikiapp.followup._quote shape)


def _text(row: dict[str, Any], col: str, where: str) -> str:
    v = row.get(col)
    if not isinstance(v, str) or not v.strip():
        raise RubricFormatError(f"{where}: {col} is missing or empty")
    return v.strip()


# The fields each flow needs from a TutorBench row. The assistant flow works without a prior
# explanation (TutorBench's follow-up rows, USE_CASE_2: PROMPT + the learner's own attempt in
# FOLLOW_UP_PROMPT): the learner's message is then PROMPT and FOLLOW_UP_PROMPT together. The rewrite
# flow rewrites the explanation, so it needs UC1_INITIAL_EXPLANATION.
FLOW_FIELDS: dict[str, tuple[str, ...]] = {
    "assistant": ("PROMPT", "FOLLOW_UP_PROMPT"),
    "rewrite": ("PROMPT", "UC1_INITIAL_EXPLANATION", "FOLLOW_UP_PROMPT"),
}


def _present(row: dict[str, Any], col: str) -> bool:
    v = row.get(col)
    return isinstance(v, str) and bool(v.strip())


def _opt(row: dict[str, Any], col: str) -> str:
    return row[col].strip() if _present(row, col) else ""


@dataclass(frozen=True)
class FlowSkip:
    """A text-only row left out of one flow because it lacks a field that flow needs."""

    task_id: str
    flow: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"task_id": self.task_id, "flow": self.flow, "reason": self.reason}


def tutorbench_row(row: dict[str, Any]) -> tuple[Case, list[FlowSkip]]:
    """One text-only row → its Case (kind = the flows it can feed) and the flows it is skipped
    for, each with the missing fields. Unparseable RUBRICS, or a row that feeds neither flow,
    raises RubricFormatError."""
    task_id = str(row.get("TASK_ID"))
    where = f"TutorBench row {row.get('TASK_ID')!r}"
    rubrics = parse_rubrics(row.get("RUBRICS"))
    flows, skips = [], []
    for flow, cols in FLOW_FIELDS.items():
        missing = [c for c in cols if not _present(row, c)]
        if missing:
            skips.append(FlowSkip(task_id, flow, f"{', '.join(missing)} missing or empty"))
        else:
            flows.append(flow)
    if not flows:
        raise RubricFormatError(f"{where}: no usable fields for any flow ("
                                + "; ".join(f"{s.flow}: {s.reason}" for s in skips) + ")")
    kind: CaseKind = "both" if len(flows) == 2 else flows[0]  # type: ignore[assignment]
    case = Case(case_id=task_id, subject=str(row.get("SUBJECT") or ""),
                prompt=_opt(row, "PROMPT"),
                initial_explanation=_opt(row, "UC1_INITIAL_EXPLANATION"),
                follow_up=_opt(row, "FOLLOW_UP_PROMPT"), rubrics=rubrics, kind=kind)
    return case, skips


def tutorbench_case(row: dict[str, Any]) -> Case:
    return tutorbench_row(row)[0]


@dataclass(frozen=True)
class TutorBenchRows:
    cases: list[Case]                # text-only rows; kind = the flows each can feed
    image_rows: list[str]            # TASK_IDs skipped because the row carries an image
    skipped: list[FlowSkip]          # (row, flow) pairs left out, with the missing fields

    def flow_count(self, flow: str) -> int:
        return sum(c.kind in (flow, "both") for c in self.cases)


def tutorbench_cases(path: Path = TUTORBENCH_ROWS) -> TutorBenchRows:
    """The fetched rows split by flow: every text-only row with PROMPT + FOLLOW_UP_PROMPT feeds the
    assistant flow; only rows that also carry UC1_INITIAL_EXPLANATION feed the rewrite flow. A
    missing field skips that row for that flow only (recorded in `skipped`, never a whole-case
    error); an unparseable RUBRICS or a row usable by neither flow raises RubricFormatError."""
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise RubricFormatError(f"{path}: not a list of rows")
    cases: list[Case] = []
    skipped: list[FlowSkip] = []
    for r in rows:
        if has_image(r):
            continue
        case, skips = tutorbench_row(r)
        cases.append(case)
        skipped.extend(skips)
    return TutorBenchRows(cases=cases, image_rows=[str(r.get("TASK_ID")) for r in rows
                                                   if has_image(r)], skipped=skipped)


def wrong_quote(stem: str, options: list[str], chosen: str, answer: int) -> str:
    """The 看不懂 quote of a check answered wrong, laid out as wikiapp.followup._quote does."""
    opts = "\n".join(f"{j + 1}) {o}" for j, o in enumerate(options))
    return f"题目：{stem}\n选项：\n{opts}\n我选的：{chosen}\n正确答案：{options[answer]}"


ZH_KEYS = {"id", "kind", "subject", "prompt", "initial_explanation", "follow_up", "rubrics"}


def zh_case(c: dict[str, Any]) -> Case:
    """One hand-written case; schema: ZH_KEYS (+ optional `wrong`: stem / options / chosen /
    answer), kind assistant | rewrite | both, 4–8 rubric items."""
    where = f"rubric_zh case {c.get('id')!r}"
    missing = ZH_KEYS - set(c)
    if missing:
        raise RubricFormatError(f"{where}: missing {sorted(missing)}")
    extra = set(c) - ZH_KEYS - {"wrong"}
    if extra:
        raise RubricFormatError(f"{where}: unknown keys {sorted(extra)}")
    if c["kind"] not in ("assistant", "rewrite", "both"):
        raise RubricFormatError(f"{where}: kind {c['kind']!r}")
    items = parse_rubrics(c["rubrics"])
    if not 4 <= len(items) <= 8:
        raise RubricFormatError(f"{where}: {len(items)} rubric items, want 4–8")
    quote = None
    if (w := c.get("wrong")) is not None:
        if (set(w) != {"stem", "options", "chosen", "answer"} or not isinstance(w["options"], list)
                or not isinstance(w["answer"], int) or not 0 <= w["answer"] < len(w["options"])
                or w["chosen"] not in w["options"] or w["chosen"] == w["options"][w["answer"]]):
            raise RubricFormatError(f"{where}: bad wrong-answer quote {w!r}")
        quote = wrong_quote(w["stem"], w["options"], w["chosen"], w["answer"])
    return Case(case_id=str(c["id"]), subject=_text(c, "subject", where),
                prompt=_text(c, "prompt", where),
                initial_explanation=_text(c, "initial_explanation", where),
                follow_up=_text(c, "follow_up", where), rubrics=items, kind=c["kind"],
                wrong_quote=quote)


def zh_cases(path: Path = ZH_CASES) -> list[Case]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RubricFormatError(f"{path}: not a list of cases")
    cases = [zh_case(c) for c in data]
    ids = [c.case_id for c in cases]
    if len(set(ids)) != len(ids):
        raise RubricFormatError(f"{path}: case ids repeat")
    return cases


# ---- the judge -----------------------------------------------------------------------------

class Verdict(BaseModel):
    index: int
    passed: bool
    reason: str


class JudgeOut(BaseModel):
    items: list[Verdict]


@dataclass(frozen=True)
class ItemResult:
    item: RubricItem
    passed: bool
    reason: str


def judge_prompt(case: Case, response: str, *, language: Language) -> str:
    rubric = "\n".join(f"{i}. {x.text}" for i, x in enumerate(case.rubrics, 1))
    wrong = f"\n他引用的、他答错的题：\n{case.wrong_quote}\n" if case.wrong_quote else ""
    explained = (f"\n之前给他的讲解：\n{case.initial_explanation}\n" if case.initial_explanation
                 else "\n（之前没有讲解：学习者直接给出了他自己的做法。）\n")
    return f"""下面是一段辅导对话。请按评分表逐条判断「待评回答」是否满足每一条标准。
学科：{case.subject or "（未注明）"}
学习者最初的问题：
{case.prompt}
{explained}
学习者接着说：
{case.follow_up}
{wrong}
待评回答：
{response}

评分表（共 {len(case.rubrics)} 条，编号从 1 开始）：
{rubric}

判断规则：
- 每一条单独判断，只看这一条说的事；别的条目做得好坏不影响这一条。
- 只有回答明确做到了这一条才算通过；含糊、只做到一部分或没有提到都不算通过。
- 标准写的是“不应该…/不要…”时，回答做了那件事就不通过。
- 理由用{LANGUAGE_NAMES[language]}写，一句话，说出回答里的依据。

输出格式：只回复 JSON {{"items": [{{"index": 1, "passed": true, "reason": "…"}}, …]}}，每条标准恰好一项，按编号顺序。"""


def judge(gateway, case: Case, response: str, *, language: Language) -> list[ItemResult]:
    """One judge call. `gateway`: a Gateway or CountingGateway (`call(role, request, schema)`)."""
    if not response.strip():
        raise RubricJudgeError(f"case {case.case_id}: empty response to judge")
    req = LLMRequest(purpose=PURPOSE, prompt_version=PROMPT_VERSION, subject=case.case_id,
                     system=SYSTEM, prompt=judge_prompt(case, response, language=language))
    out, _ = gateway.call("verifier", req, JudgeOut)
    n = len(case.rubrics)
    got = [v.index for v in out.items]
    if sorted(got) != list(range(1, n + 1)):
        raise RubricJudgeError(f"case {case.case_id}: judge gave indices {got}, want 1..{n} once each")
    if any(not v.reason.strip() for v in out.items):
        raise RubricJudgeError(f"case {case.case_id}: a verdict has no reason")
    by = {v.index: v for v in out.items}
    return [ItemResult(item=x, passed=by[i].passed, reason=by[i].reason.strip())
            for i, x in enumerate(case.rubrics, 1)]


@dataclass(frozen=True)
class Score:
    score: float                     # fraction of items passed
    critical: float | None           # fraction of critical items passed (None: no critical item)
    items: list[ItemResult]

    def as_dict(self) -> dict[str, Any]:
        return {"score": round(self.score, 4),
                "critical": None if self.critical is None else round(self.critical, 4),
                "items": [{"criteria": r.item.text, "severity": r.item.severity,
                           "passed": r.passed, "reason": r.reason} for r in self.items]}


def score(results: list[ItemResult]) -> Score:
    if not results:
        raise RubricJudgeError("nothing to score")
    crit = [r for r in results if r.item.critical]
    return Score(score=sum(r.passed for r in results) / len(results),
                 critical=sum(r.passed for r in crit) / len(crit) if crit else None,
                 items=list(results))


def mean(values: list[float | None]) -> float | None:
    xs = [v for v in values if v is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


# ---- the product calls being judged ------------------------------------------------------------

class NoNetwork:
    """web_search / fetch_url stand-ins: record a call, return nothing useful."""

    def __init__(self):
        self.calls: list[str] = []

    def search(self, query, limit):
        self.calls.append(query)
        return []

    def fetch(self, url):
        self.calls.append(url)
        raise RuntimeError("network disabled in this case")


class CaseData:
    """AssistantData for a case: no wiki page, no K, no principles, the case's language."""

    def __init__(self, language: Language):
        self._language = language

    def page(self, user_id, page_id):
        return None

    def pages(self, user_id):
        return []

    def k_text(self, user_id, concept_ids):
        return ""

    def guidelines(self, user_id):
        return []

    def language(self, user_id):
        return self._language

    def prereqs(self, user_id, concept_ids):
        return ()

    def named_nodes(self, user_id, text):
        return ()


@dataclass
class AssistantRun:
    answer: str
    last: str                        # the last event type ("done" or "error")
    tool_effects: list[str]
    network: list[str]
    error: dict[str, Any] | None = None   # the error event's payload (code / retryable / detail)


class AssistantTurnError(RuntimeError):
    """The assistant turn ended with an error event; the message carries its payload."""

    def __init__(self, run: AssistantRun):
        self.code = (run.error or {}).get("code")
        payload = ", ".join(f"{k}={v!r}" for k, v in (run.error or {}).items())
        super().__init__(f"assistant turn ended with {run.last}"
                         + (f": {payload}" if payload else ""))


def assistant_answer(gateway, case: Case, *, language: Language, user_id: str,
                     tools=None) -> AssistantRun:
    """assistant.turn on the follow-up: the case's question + explanation are the earlier turn of
    the session (no explanation: no earlier turn, the question and the follow-up are sent as one
    message); a wrong-answer quote goes in as the selection (Bridge order). `tools` None: the
    real six tools over an empty page store, network disabled."""
    from evowiki.assistant.contracts import MessageIn, Selection
    from evowiki.assistant.loop import TurnDeps, run_turn
    from evowiki.assistant.session import MemorySessionStore, SessionInfo
    from evowiki.assistant.tools import build_tools
    from evowiki.belief.aspects import AspectConfig, AspectK, AspectKView
    from evowiki.wikiapp.page_reader import InMemoryPageStore
    from tests.assistant.fakes import T0, Ids

    net = NoNetwork()
    if tools is None:
        k = AspectKView(AspectK(AspectConfig(prior_a=0.6, prior_b=1.4)),
                        T0)
        tools = list(build_tools(pages=InMemoryPageStore({}), kview=k, search=net,
                                 fetcher=net).values())
    sid = f"s-{case.case_id}"
    info = SessionInfo(session_id=sid, user_id=user_id, page_id=None, note_concept_id=None,
                       created_at=T0)
    store = MemorySessionStore()
    store.create(info)
    message = case.follow_up
    if case.initial_explanation:
        store.append(sid, "e-q", "user", {"message_id": "m0", "text": case.prompt,
                                          "selection": None}, T0)
        store.append(sid, "e-a", "assistant", {"message_id": "a0",
                                               "text": case.initial_explanation, "sources": [],
                                               "proposals": [], "tool_calls": 0,
                                               "llm_calls": 1}, T0)
    else:            # a follow-up row: the problem and the learner's own attempt, one message
        message = f"{case.prompt}\n\n{case.follow_up}"
    deps = TurnDeps(gateway=gateway, tools=tools, store=store, data=CaseData(language),
                    new_id=Ids(), clock=lambda: T0)
    sel = Selection(kind="wrong_answer", text=case.wrong_quote) if case.wrong_quote else None
    ev = list(run_turn(deps, info, MessageIn(text=message, selection=sel)))
    effects = [e.payload["effect"] for e in store.entries(sid) if e.type == "tool"]
    last = ev[-1]
    return AssistantRun(answer="".join(e.text for e in ev if e.type == "text_delta"),
                        last=last.type, tool_effects=effects, network=net.calls,
                        error=last.model_dump(exclude={"type"}) if last.type == "error" else None)


FOLLOW_UP_LINE = "学习者看完这一段后接着说：「{follow_up}」"


def rewrite_repo(case: Case, *, language: Language, user_id: str):
    """(repo, page_id, heading): a MemoryRepo with the case's one-explain-section page, its text
    the case's explanation (cue = the heading), the case language and one concept named after the
    subject (no evidence in K)."""
    from evowiki.belief.aspects import DefineConcept
    from evowiki.goals.contracts import (
        Aspect,
        OutlineSection,
        PageOutline,
        SectionRole,
        SectionText,
    )
    from evowiki.goals.fold import Page, states_for
    from evowiki.wikiapp.events import (
        ConceptName,
        ConceptsNamed,
        LanguageSet,
        PageRecord,
        PageSaved,
        WikiEvent,
    )
    from evowiki.wikiapp.repo import Commit, MemoryRepo
    from tests.assistant.fakes import T0

    pid = f"pg-{case.case_id}"
    heading = (case.subject or "讲解")[:60]
    outline = PageOutline(page_id=pid, goal=case.prompt, concepts=("topic",), targets=("topic",),
                          sections=(OutlineSection(id="s1", heading=heading,
                                                   role=SectionRole.EXPLAIN),))
    page = Page(outline=outline, states=states_for(outline), texts={
        "s1": SectionText(section_id="s1", cue=heading, body=case.initial_explanation,
                          cache_key="")})
    rec = PageRecord(page_id=pid, title=heading, goal=case.prompt,
                     concepts=("topic",), targets=("topic",), page=page, created_at=T0)
    repo = MemoryRepo([user_id])
    ev = [WikiEvent(event_id=f"wev-{case.case_id}-{i}", ts=T0, body=b) for i, b in enumerate((
        LanguageSet(language=language),
        ConceptsNamed(names=(ConceptName(concept_id="topic", name=heading),)),
        PageSaved(record=rec, action="created")))]
    repo.commit(repo.load(user_id), Commit(
        aspect=[DefineConcept(event_id=f"aev-{case.case_id}", ts=T0, concept_id="topic",
                              aspects=(Aspect.TERM, Aspect.PROCEDURE, Aspect.INTERPRET))],
        wiki=ev))
    return repo, pid, heading


def rewrite_input(case: Case, *, language: Language, user_id: str):
    """The RefineInput of 「换个讲法」 on the case's section, built as `kchange._rewrite` builds it
    (feedback_outline → refine_input(mode="feedback", scope=(s1,), instruction=feedback_instruction
    ("other", heading))), plus the follow-up line on the instruction."""
    from evowiki.goals.page_update import feedback_outline
    from evowiki.goals.rewrite_knobs import feedback_instruction
    from evowiki.wikiapp.refine_apply import refine_input
    from evowiki.wikiapp.refine_spec import WikiSpecSource
    from tests.assistant.fakes import T0

    repo, pid, heading = rewrite_repo(case, language=language, user_id=user_id)
    snap = repo.load(user_id)
    rec = snap.state.pages[pid]
    assert rec.page is not None
    target = rec.page.model_copy(update={"outline": feedback_outline(rec.page, "s1", "other")})
    instruction = (feedback_instruction("other", heading) + "\n"
                   + FOLLOW_UP_LINE.format(follow_up=case.follow_up))
    return refine_input(snap, pid, T0, WikiSpecSource(repo), page=target, scope=("s1",),
                        mode="feedback", instruction=instruction)


@dataclass
class RewriteRun:
    text: str                        # the rewritten section(s) in page order: cue line + body
    sections: list[str]
    rounds: int
    calls: int
    cost_usd: float
    model: str
    fallback_reason: str


def rewrite_section(deep_llm, case: Case, *, language: Language, user_id: str,
                    checker=None) -> RewriteRun:
    """「换个讲法」 through the product's page writer (`deep.writer.PageWriter`, deep.write
    with plan_page first) on `deep_llm` (a `wikiapp.refine_service.DeepLLM`). Render checks: `checker` (default the
    browser-free FakeChecker); the component library is empty, so interactives can only be EvoUI
    blocks. A failed write raises (WriteFailed)."""
    from evowiki.deep.writer import PageWriter
    from tests.deep.fakes import FakeChecker

    inp = rewrite_input(case, language=language, user_id=user_id)
    res = PageWriter(llm=deep_llm, checker=checker or FakeChecker(), library=None).write(inp)
    secs = list(res.outcome.draft.sections)
    if not any(s.section_id == "s1" and s.body.strip() for s in secs):
        raise RuntimeError(f"case {case.case_id}: the rewrite wrote no text for s1")
    text = "\n\n".join(f"{s.cue}\n\n{s.body}".strip() if s.cue else s.body for s in secs)
    return RewriteRun(text=text, sections=[s.section_id for s in secs],
                      rounds=res.outcome.rounds, calls=res.calls, cost_usd=res.cost_usd,
                      model=res.model, fallback_reason=res.fallback_reason)


FLOWS = ("assistant", "rewrite")


def case_flows(c: Case) -> list[str]:
    return [f for f in FLOWS if c.kind in (f, "both")]


@dataclass(frozen=True)
class Resume:
    """What an earlier report lets a run keep: `reuse` maps (case, flow) → the earlier scored
    entry, `networks` case → the earlier `assistant_network` (kept with a reused assistant entry),
    `note` says why (fresh run, case list changed, or how many pairs were reused)."""

    reuse: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    networks: dict[str, list[str]] = field(default_factory=dict)
    note: str = "no earlier report: fresh run"


def _scored(entry: Any) -> bool:
    s = entry.get("score") if isinstance(entry, dict) else None
    return isinstance(s, int | float) and not isinstance(s, bool)


def resume_from(previous: dict[str, Any] | None, cases: list[Case], *,
                fresh_reason: str | None = None) -> Resume:
    """The (case, flow) pairs of `previous` (an earlier run_cases report) that carry a score and
    that the current `cases` still run. Nothing is reused when `fresh_reason` is given, when there
    is no earlier report, or when the earlier report's case ids differ from the current ones (the
    case list changed: start fresh, said in the note). An entry without a numeric score (an error,
    a missing flow) is never reused: that pair runs again."""
    if fresh_reason is not None:
        return Resume(note=f"{fresh_reason}: fresh run")
    if previous is None:
        return Resume()
    now = {c.case_id for c in cases}
    rows = previous.get("rows") or []
    before = set(previous.get("case_ids") or [r.get("case") for r in rows])
    if before != now:
        return Resume(note=(f"case list changed ({len(before)} earlier ids, {len(now)} now; "
                            f"{len(now - before)} added, {len(before - now)} removed): fresh run"))
    flows = {c.case_id: case_flows(c) for c in cases}
    reuse: dict[tuple[str, str], dict[str, Any]] = {}
    networks: dict[str, list[str]] = {}
    for r in rows:
        cid = r.get("case")
        for flow in flows.get(cid, []):
            if _scored(r.get(flow)):
                reuse[(cid, flow)] = r[flow]
                if flow == "assistant":
                    networks[cid] = list(r.get("assistant_network") or [])
    return Resume(reuse=reuse, networks=networks,
                  note=f"resumed: {len(reuse)} scored (case, flow) pairs reused from the earlier "
                       f"report")


def error_kind(exc: BaseException) -> str:
    """The cause class an error is counted under: the exception type plus its code / failure
    (AssistantTurnError: the error event's code; WriteFailed: the RefineFailure)."""
    code = getattr(exc, "code", None) or getattr(exc, "failure", None)
    return f"{type(exc).__name__}: {code}" if code else type(exc).__name__


def error_summary(errors: list[dict[str, str]]) -> str:
    """The errors counted by kind, each with its first message, for an assertion message."""
    by: dict[str, list[dict[str, str]]] = {}
    for e in errors:
        by.setdefault(e.get("kind") or e["error"].split(":")[0], []).append(e)
    return f"{len(errors)} errors by kind: " + "; ".join(
        f"{k} ×{len(v)} (first: {v[0]['case']}/{v[0]['flow']}: {v[0]['error']})"
        for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])))


def run_cases(cases: list[Case], *, chat_gateway, gateway, deep_llm, language: Language,
              user_id: str, resume: Resume | None = None, run_flow=None) -> dict[str, Any]:
    """Each case: the assistant follow-up (kind assistant / both, over `chat_gateway`, a Gateway)
    and the 「换个讲法」 rewrite (kind rewrite / both, the deep writer over `deep_llm`), each judged
    with the case's rubric (over `gateway`). A failing product or judge call is recorded under
    `errors` with its reason and `kind` (never dropped silently; an assistant turn that ends with
    an error event carries the event's code / retryable / detail); the caller asserts there are
    none after reporting.

    `resume` (from `resume_from`): the (case, flow) pairs it holds are kept as they were (listed
    under `reused`) and not run; every other pair runs now (`run_now`). `mean` / `critical_mean`
    are over all scored entries, reused and new. `run_flow(flow, case, row) -> entry` replaces the
    product call + judge (offline tests); the default may set row["assistant_network"]."""
    resume = resume or Resume()

    def default_flow(flow: str, c: Case, row: dict[str, Any]) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if flow == "assistant":
            run = assistant_answer(chat_gateway, c, language=language, user_id=user_id)
            row["assistant_network"] = run.network
            if run.last != "done":
                raise AssistantTurnError(run)
            text = run.answer
        else:
            rw = rewrite_section(deep_llm, c, language=language, user_id=user_id)
            text = rw.text
            extra = {"sections": rw.sections, "rounds": rw.rounds, "calls": rw.calls,
                     "model": rw.model, "fallback_reason": rw.fallback_reason}
        sc = score(judge(gateway, c, text, language=language))
        return {"response": text[:1500], **extra, **sc.as_dict()}

    run_flow = run_flow or default_flow
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    reused: list[dict[str, str]] = []
    run_now = 0
    for c in cases:
        row: dict[str, Any] = {"case": c.case_id}
        for flow in case_flows(c):
            if (c.case_id, flow) in resume.reuse:
                row[flow] = resume.reuse[(c.case_id, flow)]
                if flow == "assistant":
                    row["assistant_network"] = resume.networks.get(c.case_id, [])
                reused.append({"case": c.case_id, "flow": flow})
                continue
            run_now += 1
            try:
                row[flow] = run_flow(flow, c, row)
            except Exception as exc:  # noqa: BLE001 - recorded with its reason, asserted by caller
                errors.append({"case": c.case_id, "flow": flow, "kind": error_kind(exc),
                               "error": f"{type(exc).__name__}: {exc}"[:500]})
        rows.append(row)

    def flow_mean(flow: str, key: str) -> float | None:
        return mean([r[flow][key] for r in rows if flow in r])
    return {"cases": len(cases), "case_ids": [c.case_id for c in cases], "rows": rows,
            "errors": errors, "reused": reused, "reused_count": len(reused), "run_now": run_now,
            "resume_note": resume.note,
            "mean": {f: flow_mean(f, "score") for f in FLOWS},
            "critical_mean": {f: flow_mean(f, "critical") for f in FLOWS}}


def earlier_report(path: Path, env: Mapping[str, str] = os.environ
                   ) -> tuple[dict[str, Any] | None, str | None]:
    """Read the previous report; malformed reports fail rather than silently resume fresh."""
    if env.get(FRESH_ENV) == "1":
        return None, f"{FRESH_ENV}=1"
    if not path.exists():
        return None, None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{path}: not a report object; delete it or set {FRESH_ENV}=1")
    return data, None


def report_extra(res: dict[str, Any], rows: TutorBenchRows, deep_out: dict[str, Any],
                 chat_cost_usd: float) -> dict[str, Any]:
    """Report this run's spend, keeping reused results separate from newly paid calls."""
    return {**res, "skipped_image_rows": rows.image_rows,
            "skipped_flows": [s.as_dict() for s in rows.skipped],
            "flow_rows": {f: rows.flow_count(f) for f in FLOWS},
            "deep_spend": deep_out,
            "spend_scope": "calls / cost_usd / deep_spend / total_cost_usd: this run's calls "
                           "only; reused pairs' spend is in the earlier report",
            "total_cost_usd": round(chat_cost_usd + deep_out["cost_usd"], 6),
            "per_row": {r["case"]: {f: r[f]["score"] for f in FLOWS if f in r}
                        for r in res["rows"]}}
