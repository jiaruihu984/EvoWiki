"""Sandboxed one-section replays (design 2026-10-21 §5.4): a past deep write run's section, rewritten on
the snapshot that run was built from, under a candidate pack and teaching spec. Writes nothing but the
replay's own trace (`DeepService.replay_section`: a dry run).

The current pack's reference replay of a case is `replay(case, PackDiff())`: the empty diff leaves the
base pack and spec as they are.

A case of kind "assistant" (harness 3b follow-up 3) is a traced assistant turn instead: `replay`
hands it to `harness.replay_assistant.replay_turn` on `ReplayDeps.assistant` (the round's recording
gateway, so its spend is the round's). Its `section_id` / `page_id` are empty, its `snapshot` is
NO_SEQS (unused), `anchor` is False and the teaching spec plays no part.

`ReplayResult.text` is what the judge reads: the section's recall cue line (`回忆线索：…`, the text
shown when the section is folded) and then its body. The heading is left out: a replay rewrites the
section's teaching, and the judge compares that.

**The writer's model**: a round's replays use the user's writer now, for both packs. Nothing compares
them with the traced text any more (2026-11-04: pinning the traced run's model, audit 2026-10-28, made
every old case unusable the day the user switched model). `ReplayDeps.writer_models` can still pin
one: it names the distinct models of a run's `deep.write` calls (`pg_writer_models`); a run that
wrote with two or with none is unusable (LookupError: MODEL_MIXED / MODEL_UNKNOWN), and so is one
whose model the deep tier can no longer route (`DeepLLM.pinned`).

**Which pack** (audit 2026-10-28): `apply_diff` keeps the base pack's version, so a replay's trace
run records the version its candidate is built on (`pack_at` of it is the base pack, never a
candidate). The replay's job marker (`input.job.candidate`) tells them apart: `candidate_ref` of
the diff, None for the current pack's reference replay.

**Parallel replays** (2026-10-28: one round was 5 sequential replays of 5–7 minutes each):
`run_all` runs independent replays on a small thread pool (REPLAY_WORKERS). Every replay builds its
own gateways, job meter and trace wrapper (`DeepService.replay_section`,
`replay_assistant.replay_turn`); what they share (the call log, the usage meter, the trace sink,
the wiki) is what concurrent page jobs share too, and the round's recording log takes a lock."""

import contextvars
import hashlib
import json
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from evowiki.deep.agent import SnapshotSeqs
from evowiki.goals.fold import Page
from evowiki.harness.diff import PackDiff, apply_diff, diff_to_json, h12_tool
from evowiki.harness.pack import HarnessPack
from evowiki.llm.models import ModelRef
from evowiki.teach import spec_ops
from evowiki.teach.contracts import ParamPatchOp, SpecPatchOp, TeachingSpec
from evowiki.wikiapp.refine_apply import KEEP_DOMAIN
from evowiki.wikiapp.refine_service import DeepService
from evowiki.wikiapp.repo import Repo

if TYPE_CHECKING:
    from evowiki.harness.replay_assistant import AssistantReplayDeps

CANDIDATE_PATCH = "harness-candidate"     # the `origin` of a spec item a candidate adds or edits
NO_SEQS = SnapshotSeqs(wiki_seq=0, aspect_seq=0, infer_version=0)   # an assistant case's (unused)
NO_ASSISTANT = "这一轮没有重放助手回答的条件"
MODEL_UNKNOWN = "找不到这次写作用的模型，不能重放"
MODEL_MIXED = "这次写作中途换过模型，不能重放"
REPLAY_WORKERS = 3        # replays run at once (`run_all`); each mostly waits on its model


@dataclass(frozen=True)
class Case:
    """One input to replay: section `section_id` of the run `run_id` (a deep write run of `page_id`,
    built from `snapshot`, with its `mode` and `instruction`), chosen for the signals `signal_ids`;
    `anchor` = a section of one of the user's exemplar pages (a regression check). `page` / `domain`:
    what the run was given in place of the snapshot's stored page and domain (its traced `page`,
    Page JSON, and `domain`); None / KEEP_DOMAIN = the snapshot's own. `page_seq`: set when `page`
    is the page the run's job saved (a section added during the run, `harness.cases`): the wiki
    seq of that commit; None = `page` is the run's own input. `kind` "assistant": `run_id` is a
    traced assistant turn (module doc); the deep-only fields are placeholders."""

    case_id: str
    run_id: str
    page_id: str
    section_id: str
    signal_ids: tuple[str, ...]
    anchor: bool
    snapshot: SnapshotSeqs
    mode: Literal["page", "rewrite", "feedback"]
    instruction: str
    user_id: str
    page: dict | None = None
    domain: Any = KEEP_DOMAIN
    page_seq: int | None = None
    kind: Literal["deep", "assistant"] = "deep"


@dataclass(frozen=True)
class ReplayResult:
    case_id: str
    text: str                 # module doc: cue line + body
    writer_provider: str      # the provider family that wrote it (after any fallback)
    run_id: str               # the replay's trace run
    cost: float               # what the replay's calls were charged (USD)
    # tools with an effect the replayed turn called and got no error from. All simulated: nothing is
    # written in a replay, so this says what the text would have done, never that it happened
    actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayDeps:
    deep: DeepService
    repo: Repo                # the wiki the snapshots are read from (`Repo.load_at`)
    # assistant cases (module doc); None: an assistant case cannot be replayed (LookupError)
    assistant: "AssistantReplayDeps | None" = None
    # a deep case's run id -> the distinct models of its `deep.write` calls (module doc, "The
    # writer's model"); None: not pinned (the offline eval's synthetic cases have no traced run)
    writer_models: Callable[[str], tuple[ModelRef, ...]] | None = None


def pg_writer_models(pool: Any) -> Callable[[str], tuple[ModelRef, ...]]:
    """`ReplayDeps.writer_models` over Postgres: the models of the run's traced `deep.write` model
    steps, distinct, in step order (one connection per read)."""
    from evowiki.store.db import fetch_all

    def models(run_id: str) -> tuple[ModelRef, ...]:
        with pool.connection() as conn:
            rows = fetch_all(conn, "select c.provider, c.model from agent_step s join llm_call c "
                                   "on c.id = s.call_id where s.run_id = %s and s.kind = 'model' "
                                   "and c.purpose = 'deep.write' order by s.seq", (run_id,))
        return tuple(dict.fromkeys(ModelRef(provider=r["provider"], model=r["model"])
                                   for r in rows))
    return models


def writer_of(case: "Case", deps: ReplayDeps) -> ModelRef | None:
    """The model a deep case's replay must write with (module doc, "The writer's model"); None when
    not pinned. LookupError (MODEL_UNKNOWN / MODEL_MIXED) for a run that cannot name one."""
    if deps.writer_models is None:
        return None
    models = deps.writer_models(case.run_id)
    if not models:
        raise LookupError(MODEL_UNKNOWN)
    if len(models) > 1:
        raise LookupError(MODEL_MIXED)
    return models[0]


def candidate_ref(candidate: PackDiff) -> str | None:
    """What a replay's trace records of its candidate (module doc, "Which pack"): a short digest of
    the diff's ops and spec ops; None for the empty diff (the current pack)."""
    if not candidate.ops and not candidate.spec_ops:
        return None
    raw = json.dumps(diff_to_json(candidate, {}), ensure_ascii=False, sort_keys=True, default=str)
    return "cand-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def run_all[T](jobs: Sequence[Callable[[], T]]) -> list[T | Exception]:
    """Runs `jobs` at once on at most REPLAY_WORKERS threads (module doc, "Parallel replays"), each in
    a copy of the caller's context (deadlines, output caps). Returns each job's result or the
    Exception it raised, in the order given; a single job runs on the calling thread."""
    def guarded(job: Callable[[], T]) -> T | Exception:
        try:
            return job()
        except Exception as e:  # noqa: BLE001 - handed back to the caller, in order
            return e
    if len(jobs) <= 1:
        return [guarded(j) for j in jobs]
    with ThreadPoolExecutor(max_workers=min(REPLAY_WORKERS, len(jobs)),
                            thread_name_prefix="harness-replay") as ex:
        futures = [ex.submit(contextvars.copy_context().run, guarded, j) for j in jobs]
        return [f.result() for f in futures]


def candidate_spec(base: TeachingSpec, ops: Sequence[SpecPatchOp | ParamPatchOp]) -> TeachingSpec:
    """`base` with the candidate's spec ops applied in order, through the pure `teach.spec_ops.apply`
    (version unchanged; `base` itself is never modified). PatchError when an op does not apply."""
    spec = base
    for op in ops:
        spec = spec_ops.apply(spec, op, patch_id=CANDIDATE_PATCH)
    return spec


def text_of(body: str, cue: str | None) -> str:
    return f"回忆线索：{cue}\n\n{body}" if cue else body


def replay(case: Case, candidate: PackDiff, *, base_pack: HarnessPack, base_spec: TeachingSpec,
           deps: ReplayDeps) -> ReplayResult:
    """The case's section rewritten under `base_pack` + `candidate` and `base_spec` + its spec ops.
    Raises what `replay_section` raises (snapshot or section gone, no draft, the writer's model no
    longer routed) and HarnessInvalid / PatchError for a candidate that does not apply; LookupError
    when the traced run's writer model is unknown or mixed. An assistant case: what `replay_turn`
    raises, and a LookupError (NO_ASSISTANT) without `deps.assistant`."""
    if case.kind == "assistant":
        from evowiki.harness.replay_assistant import ReplaySourceMissing, replay_turn
        if deps.assistant is None:
            raise ReplaySourceMissing(NO_ASSISTANT)
        return replay_turn(case, candidate, base_pack=base_pack, deps=deps.assistant)
    writer = writer_of(case, deps)
    pack = apply_diff(base_pack, candidate)
    spec = candidate_spec(base_spec, candidate.spec_ops)
    svc = replace(deps.deep, repo=deps.repo)
    tool = h12_tool(candidate)
    extra = {} if tool is None else {"forge_candidate": tool[2]}
    out = svc.replay_section(case.user_id, snapshot=case.snapshot, page_id=case.page_id,
                             section_id=case.section_id, spec=spec, harness=lambda uid: pack,
                             mode=case.mode, instruction=case.instruction,
                             page=None if case.page is None else Page.model_validate(case.page),
                             domain=case.domain, writer=writer,
                             candidate=candidate_ref(candidate), **extra)
    return ReplayResult(case_id=case.case_id, text=text_of(out.body, out.cue),
                        writer_provider=out.writer_provider, run_id=out.run_id, cost=out.cost)
