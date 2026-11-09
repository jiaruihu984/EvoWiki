"""Test double of the page writer (`deep.writer.PageWriter`, single writer 2026-10-10) for wikiapp / service
tests: no agent loop, no LLM. It calls the real plan_page tool first: a page without an outline
(`RefineInput.spec`) takes its outline from the next scripted PLAN_PAGE answer (a DraftOutline dict,
what the writer would pass; a refused one takes the next, at most OUTLINE_TRIES), and the accepted
page reaches `on_outline` as it would from the agent. Each section it must write (the outline's
required sections, or the scope of a rewrite) then takes the next scripted `goals.write` answer
from the test's `Script` ({"text": "回想：…" + body}, the shape the old section writer used). Writes
go through a real `Workspace`, so `on_change` streams them like the agent's writes."""

import json
from collections.abc import Callable
from threading import Event

from evowiki.agent.tools import ToolContext
from evowiki.deep.agent import RefineFailed, RefineInput, RefineOutcome, outline_of
from evowiki.deep.contracts import RefinedSection
from evowiki.deep.store import MemoryAssetStore
from evowiki.deep.tools import OutlineSetup, PlanPageTool, Workspace
from evowiki.deep.writer import WriteFailed, WriteResult
from evowiki.wikiapp.organize import Writing
from evowiki.wikiapp.refine_spec import WikiSpecSource

WRITE = "goals.write"
PLAN_PAGE = "plan_page"      # the outline a scripted writer passes to plan_page (a DraftOutline dict)
OUTLINE_TRIES = 3
MODEL = "anthropic:claude-sonnet-5-5"
# push one of these as a `goals.write` answer to fail the write with that RefineFailure
FAILURES = {"BUDGET": "budget_exceeded", "UNAVAILABLE": "llm_unavailable"}
PLAN = {"observable": "o", "one_at_a_time": ["一步"], "contrast": "c", "spoken_answer": "s",
        "practice": "不用当场练：测试"}


class ScriptWriter:
    """`write` answers from `script`'s `goals.write` queue, one answer per section; a FAILURES key
    fails the write with that code, any other non-JSON answer with `no_draft`. `inputs`: every
    RefineInput it was given. `before_write`: called at the start of every write (a test changing
    the wiki "while the page is being written")."""

    def __init__(self, script, *, model: str = MODEL, cost_usd: float = 0.0):
        self.script, self.model, self.cost_usd = script, model, cost_usd
        self.inputs: list[RefineInput] = []
        self.readers: list = []                  # the material reader of every write (None: none)
        self.before_write: Callable[[], None] | None = None

    def write(self, inp: RefineInput, *, budget=None,
              on_writer: Callable[[str, bool, str], None] = lambda m, f, r: None,
              on_change=None, on_spend=None, on_outline=None, cancel: Event | None = None,
              reader=None) -> WriteResult:
        self.inputs.append(inp)
        self.readers.append(reader)
        if self.script is None:                  # service tests: the test's own Script
            from tests.wikiapp.script import Script
            self.script = Script.latest
        if self.before_write is not None:
            self.before_write()
        on_writer(self.model, False, "")
        ws = Workspace(inp.page, scope=inp.scope, page_cells=inp.page_cells,
                       required=inp.required, on_change=on_change)
        scripted_plan(inp, ws, on_outline, lambda prompt: self.script.take(PLAN_PAGE, prompt),
                      cost_usd=self.cost_usd, model=self.model)
        assert ws.page is not None
        by_id = {s.id: s for s in ws.page.outline.sections}
        todo = ws.required or inp.scope
        total = max(1, len(todo))
        for n, sid in enumerate(todo, 1):
            if cancel is not None and cancel.is_set():
                raise WriteFailed(RefineFailed("cancelled", "写作被取消"),
                                  round(self.cost_usd * (n - 1) / total, 6), self.model)
            raw = self.script.take(WRITE)          # unscripted: AssertionError (a test bug)
            if raw in FAILURES:
                raise WriteFailed(RefineFailed(FAILURES[raw], f"scripted {raw}"), self.cost_usd,
                                  self.model)
            if not raw.startswith("{"):          # unusable output: the agent never finished
                raise WriteFailed(RefineFailed("no_draft", f"unusable: {raw[:40]}"),
                                  self.cost_usd, self.model)
            text = json.loads(raw)["text"].strip()
            first, _, rest = text.partition("\n")
            cue, body = (first[len("回想："):].strip(), rest.strip()) if first.startswith("回想：") else ("", text)
            s = by_id[sid]
            ws.sections[sid] = RefinedSection(section_id=sid, heading=s.heading, role=s.role,
                                              depth="example", cells=s.cells,
                                              cue=(cue or s.heading)[:60], answer=f"{s.heading}的答案",
                                              body=body or "…")
            if on_spend is not None:
                on_spend(round(self.cost_usd * n / total, 6), n)
            ws.changed(sid)
        draft = ws.draft(inp.page_id)
        return WriteResult(outcome=RefineOutcome(draft=draft, checks={}, self_check=(), rounds=1,
                                                 page=ws.planned),
                           cost_usd=self.cost_usd, model=self.model, fallback_reason="",
                           calls=len(todo))



def scripted_plan(inp: RefineInput, ws: Workspace, on_outline, take: Callable[[str], str], *,
                  cost_usd: float = 0.0, model: str = MODEL) -> None:
    """The real plan_page as a scripted writer's first call: the plan, and for a page without an
    outline `take(prompt)`'s outline JSON (`prompt`: the writer's outline part, what an `always`
    answer reads); a refused outline takes the next one, as the model would fix it."""
    setup = None if inp.spec is None else OutlineSetup(spec=inp.spec, kview=inp.kview)
    tool = PlanPageTool(ws, known=inp.known, setup=setup, on_outline=on_outline)
    ctx = ToolContext(user_id=inp.user_id, session_id="fake")
    for _ in range(OUTLINE_TRIES if setup is not None else 1):
        args: dict = {"plan": PLAN}
        if setup is not None:
            args["outline"] = json.loads(take(outline_of(inp)))   # unscripted: a test bug
        got = tool.run(args, ctx)
        if got.error is None:
            return
    raise WriteFailed(RefineFailed("no_draft", f"plan_page refused: {got.content[:300]}"),
                      cost_usd, model)


def writing(env_or_repo, script=None, **kw) -> Writing:
    """The writer kit over a test repo: ScriptWriter, the wiki's spec source, a memory asset store."""
    repo, s = (env_or_repo[0], env_or_repo[1]) if isinstance(env_or_repo, tuple) else (env_or_repo,
                                                                                       script)
    return Writing(writer=ScriptWriter(s, **kw), specs=WikiSpecSource(repo),
                   assets=MemoryAssetStore())
