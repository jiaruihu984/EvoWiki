"""A deep write round records what a replay needs: the snapshot seqs (`Repo.load_at`'s arguments), mode,
instruction, scope, spec version, and the page and domain it was given in place of the stored ones.
Scripted gateways only."""

from dataclasses import replace

from evowiki.deep.agent import PROMPT_VERSION, SnapshotSeqs
from evowiki.deep.skill import version as skill_version
from evowiki.deep.writer import PageWriter
from evowiki.wikiapp.refine_apply import refine_input
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import MemoryRepo
from tests.deep.fakes import DeepScript, FakeChecker, library
from tests.deep.test_writer import outline_input, write_all
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized


class Recorder:
    def __init__(self) -> None:
        self.starts: list[dict] = []

    def start(self, run_id, *, user_id, agent, subject, pack_version, input) -> None:
        self.starts.append({"run_id": run_id, "agent": agent, "subject": subject, "input": input})

    def model(self, *a, **k) -> None:
        pass

    def tool(self, *a, **k) -> None:
        pass

    def finish(self, *a, **k) -> None:
        pass


def _write(inp) -> Recorder:
    rec = Recorder()
    ds = DeepScript().plan().write(*write_all(1))
    llm = replace(ds.llm(), trace=rec)
    PageWriter(llm=llm, checker=FakeChecker(), library=library()).write(inp)
    return rec


def _env():
    return MemoryRepo(["u1"]), Script(), Ids()


def test_a_round_records_the_snapshot_it_was_built_from():
    env = _env()
    pid = organized(env)
    snap = env[0].load("u1")
    inp = outline_input(env, pid)
    assert inp.snapshot_seqs == SnapshotSeqs(snap.wiki_seq, snap.aspect_seq, snap.infer_version)
    (start,) = _write(inp).starts
    assert start["agent"] == "deep.write"
    assert start["input"] == {
        "page_id": pid, "title": inp.title, "round": 0, "must_fix": [], "language": inp.language,
        "prompt_version": PROMPT_VERSION, "skill": skill_version(),
        "snapshot": {"wiki_seq": snap.wiki_seq, "aspect_seq": snap.aspect_seq,
                     "infer_version": snap.infer_version},
        "mode": "page", "instruction": "",
        "scope": {"sections": [], "dry_run": False}, "spec_version": inp.teaching.version,
        # the fresh outline (no texts) the write was given, not the stored page
        "page": inp.page.model_dump(mode="json"), "domain": None}
    assert start["input"]["page"]["texts"] == {}
    assert snap.state.pages[pid].page.texts                     # the stored page has texts


def test_a_refine_of_the_stored_page_traces_no_page():
    env = _env()
    pid = organized(env)
    snap = env[0].load("u1")
    inp = refine_input(snap, pid, NOW, WikiSpecSource(env[0]), mode="page")
    assert inp.given_page is None
    (start,) = _write(inp).starts
    assert start["input"]["page"] is None and "domain" in start["input"]


def test_scope_instruction_and_dry_run_are_recorded():
    env = _env()
    pid = organized(env)
    snap = env[0].load("u1")
    base = refine_input(snap, pid, NOW, WikiSpecSource(env[0]), page=outline_input(env, pid).page,
                        mode="feedback", instruction="讲慢一点", scope=("s2",))
    (start,) = _write(replace(base, dry_run=True, required=(), language="en")).starts
    got = start["input"]
    assert got["mode"] == "feedback" and got["instruction"] == "讲慢一点"
    assert got["scope"] == {"sections": ["s2"], "dry_run": True}
    assert got["language"] == "en"


def test_a_hand_built_input_records_no_snapshot():
    env = _env()
    pid = organized(env)
    inp = replace(outline_input(env, pid), snapshot_seqs=None)
    (start,) = _write(inp).starts
    assert start["input"]["snapshot"] is None
    assert set(start["input"]) >= {"mode", "instruction", "scope", "spec_version", "snapshot"}
