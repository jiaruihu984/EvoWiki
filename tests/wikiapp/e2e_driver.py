"""End-to-end z-score → PCA scenario over the wiki flow functions (no HTTP), with the scripted
synthetic LLM in tests/fixtures/wiki_e2e/script.json (see the README there: synthetic, scripted,
non-reportable).

`run_scenario` calls the flows in order with an injected `Repo`, Gateway, clock and id source and
returns every step's structured output (ChangeOut, PageOut / DiffNoteOut, CatalogOut, KGridOut,
...) for the assertions. The same script and the same ids / clock give the same run on
MemoryRepo and PgRepo (tests/wikiapp/test_replay.py)."""

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from evowiki.belief.aspects import AspectEvent
from evowiki.goals.contracts import cell_id
from evowiki.llm.gateway import Gateway
from evowiki.wikiapp.kchange import feedback_know
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.organize import Writing, organize_stream
from evowiki.wikiapp.page import open_page_flow
from evowiki.wikiapp.paths import next_path_page
from evowiki.wikiapp.repo import Commit, Repo, Snapshot
from evowiki.wikiapp.views import catalog_out, goal_draft_out, k_grid_out
from tests.runtime.helpers import FixedClock
from tests.wikiapp.flat_goal import flat_goal
from tests.wikiapp.goal_script import PROBE, answer_round, question_of, task_lines, write_item
from tests.wikiapp.runs import runs_for
from tests.wikiapp.script import Script
from tests.wikiapp.verify import verify_prereqs

SCRIPT = Path(__file__).resolve().parents[1] / "fixtures" / "wiki_e2e" / "script.json"
USER = "e2e-user"
Z_GOAL, Z_BACKGROUND = "我想学 z 分数", "我会算平均数，方差公式记不清"
PCA_GOAL, PCA_BACKGROUND = "我想学 PCA", "我学过 z 分数，我知道 PCA 用来降维"
MEAN_RECAP, VARIANCE_RECAP = "s2", "s3"          # sections of the z-score page (script.json)
# The script's intake prerequisites, verified right after each goal is created (relation layers:
# intake writes them as unverified recommended edges; an expert review verifies them) so the
# diagnosis and K infer along them as the scenario was written: 均值, 方差 → z 分数; z 分数, 方差 → PCA.
Z_VERIFIED = (("c2", "c1"), ("c3", "c1"))
PCA_VERIFIED = (("c1", "c4"), ("c3", "c4"))


class Ids:
    """Deterministic ids, one counter per prefix (`pg_0001`, `pg_0002`, ...), so page ids do not
    depend on how many other ids a flow draws."""

    def __init__(self) -> None:
        self.n: dict[str, int] = defaultdict(int)

    def __call__(self, prefix: str) -> str:
        self.n[prefix] += 1
        return f"{prefix}_{self.n[prefix]:04d}"


def bank_writer(outputs: list[dict]):
    """The probe model over script.json's `goals.probe` items (a bank: the KST diagnosis picks
    the node, so the number and order of probe calls is the algorithm's): a quiz takes the first
    bank quiz on one of the picked node's cells (the prompt names the node, not a cell: probe@v8),
    anything else is written generically (goal_script.write_item)."""
    bank = [q for o in outputs for q in o["questions"]]

    def write(prompt: str) -> dict:
        qs = []
        for kind, node, _cells in task_lines(prompt):
            hit = next((q for q in bank if kind == "quiz"
                        and q["target"].rpartition(".")[0] == node), None)
            if hit is None:
                line = next(x for x in prompt.splitlines() if f"{kind}：" in x and f" {node} " in x)
                hit = write_item(line)["questions"][0]
            qs.append(hit)
        return {"questions": qs}
    return write


def load_script(path: Path = SCRIPT) -> Script:
    s = Script()
    for purpose, outputs in json.loads(path.read_text(encoding="utf-8")).items():
        if purpose == PROBE:
            s.always(PROBE, bank_writer(outputs))
        else:
            s.push(purpose, *outputs)
    return s


def answer_rounds(repo: Repo, gw, u: str, res, pick, *, now, new_id) -> tuple[Any, list]:
    """Answer the draft's rounds with `pick(question)` until it finishes; returns the last
    result and every (question, answer) asked."""
    asked = []
    while res.open:
        q = question_of(res.draft.rounds[-1])      # the scripted assistant's question (ask_check)
        a = pick(q)
        asked.append((q, a))
        res = answer_round(repo, gw(), u, res.draft.draft_id, [a], now=now, new_id=new_id)
    return res, asked


class RecordingRepo:
    """Delegates to a Repo and keeps the snapshot the last commit returned (the runtime state,
    applied event by event, that replay must equal)."""

    def __init__(self, inner: Repo) -> None:
        self.inner = inner
        self.last: Snapshot | None = None

    def load(self, user_id: str) -> Snapshot:
        return self.inner.load(user_id)

    def aspect_events(self, user_id: str) -> list[AspectEvent]:
        return self.inner.aspect_events(user_id)

    def wiki_events_of(self, user_id: str, types):
        return self.inner.wiki_events_of(user_id, types)

    def wiki_events_since(self, user_id: str, types, after_seq: int = 0):
        return self.inner.wiki_events_since(user_id, types, after_seq)

    def commit(self, snap: Snapshot, c: Commit, *, once=None) -> Snapshot:
        self.last = self.inner.commit(snap, c, once=once)
        return self.last


SEEN_DIRECT = 0.9          # direct evidence that counts as already measured


def measured_cells(snap: Snapshot, now) -> set[str]:
    """Cells with direct evidence ≥ SEEN_DIRECT (what the diagnostic treats as already measured)."""
    kv = snap.kview(now)
    return {cell_id(c, a) for c in snap.k.concept_ids() for a in snap.k.aspects(c)
            if kv.cell(cell_id(c, a)).direct >= SEEN_DIRECT}


def run_scenario(repo: Repo, gateway: Gateway, clock: FixedClock, new_id,
                 kit: Writing) -> dict[str, Any]:
    out: dict[str, Any] = {}
    u = USER

    def gw() -> CountingGateway:          # one counter per request, like the routes
        return CountingGateway(gateway)

    def catalog():
        snap = repo.load(u)
        return catalog_out(snap.state, snap.k, clock())

    # 1. z-score: goal → draft, answers, organize, open
    z = flat_goal(repo, gw(), u, Z_GOAL, Z_BACKGROUND, now=clock(), new_id=new_id)
    verify_prereqs(repo, u, Z_VERIFIED, now=clock(), new_id=new_id)
    out["z_draft"], out["z_draft_out"] = z, goal_draft_out(z)
    # one quiz per round, picked by the KST diagnosis: every quiz answered with the key
    clock.advance(minutes=2)
    zres, asked = answer_rounds(repo, gw, u, z, lambda q: q.answer, now=clock(), new_id=new_id)
    out["z_answers"] = {q.target: a for q, a in asked}
    out["z_final"] = repo.load(u).state.drafts[z.draft.draft_id].rounds[-1]
    zpid = next_path_page(repo, u, zres.path_id, now=clock(), new_id=new_id).page_id
    out["z_page"] = zpid
    out["z_measured"] = measured_cells(repo.load(u), clock())
    out["z_organize"] = list(organize_stream(repo, gw(), u, zpid, now=clock(), new_id=new_id,
                                             writing=kit, runs=runs_for(repo)))
    clock.advance(minutes=1)
    out["z_open"] = open_page_flow(repo, gw(), u, zpid, now=clock(), new_id=new_id)

    # 2. 「这部分我懂」 on the mean recap (0 calls)
    clock.advance(minutes=5)
    out["know"] = feedback_know(repo, gw(), u, zpid, MEAN_RECAP, now=clock(), new_id=new_id)
    out["k_before_pca"] = repo.load(u).k.to_json()
    out["measured_before_pca"] = measured_cells(repo.load(u), clock())

    # 3. PCA: goal → draft (reuses z 分数 and 方差)
    clock.advance(minutes=10)
    pca = flat_goal(repo, gw(), u, PCA_GOAL, PCA_BACKGROUND, now=clock(), new_id=new_id)
    verify_prereqs(repo, u, PCA_VERIFIED, now=clock(), new_id=new_id)
    out["pca_draft"], out["pca_draft_out"] = pca, goal_draft_out(pca)

    # 4. every PCA question answered right (the variance one too), page, organize
    clock.advance(minutes=2)
    pres, asked = answer_rounds(repo, gw, u, pca, lambda q: q.answer, now=clock(),
                                new_id=new_id)
    out["pca_answers"] = {q.target: a for q, a in asked}
    ppid = next_path_page(repo, u, pres.path_id, now=clock(), new_id=new_id).page_id
    out["pca_page"] = ppid
    out["pca_organize"] = list(organize_stream(repo, gw(), u, ppid, now=clock(), new_id=new_id,
                                               writing=kit, runs=runs_for(repo)))

    # 5. the z-score page is stale; opening it folds the variance recap (0 calls, no patch)
    out["catalog_after_pca"] = catalog()
    clock.advance(minutes=30)
    out["z_reopen"] = open_page_flow(repo, gw(), u, zpid, now=clock(), new_id=new_id)

    # 6. the catalog after the reopen
    clock.advance(minutes=1)
    out["catalog_final"] = catalog()
    snap = repo.load(u)
    out["k_grid"] = k_grid_out(snap.state, snap.k, clock())
    return out
