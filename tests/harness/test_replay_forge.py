"""Deep replays take a forged-tool candidate overlay (harness 3b, Task 6)."""
from types import SimpleNamespace

import pytest

from evowiki.deep.llm import FORGE_UNAVAILABLE, ForgeCandidateUnavailable, forge_opener
from evowiki.harness.pack import EMPTY
from evowiki.wikiapp.refine_service import DeepService, ReplayFailed


class _Runtime:
    def __init__(self):
        self.calls = []

    def for_run(self, **kw):
        self.calls.append(kw)


INP = SimpleNamespace(user_id="u", title="t", goal="g")


def test_forge_opener_passes_the_candidate_only_when_given():
    rt = _Runtime()
    with pytest.raises(ForgeCandidateUnavailable):    # the run did not reach the candidate
        forge_opener(rt, INP, None, candidate="fc_abc")("r1", None)  # type: ignore[arg-type]
    assert forge_opener(rt, INP, None)("r2", None) is None  # type: ignore[arg-type]
    assert rt.calls[0]["candidate"] == "fc_abc"
    assert "candidate" not in rt.calls[1]


class _Reaching:
    """for_run returns a run reaching the tools of these candidate ids."""

    def __init__(self, *cids):
        self.cids = cids

    def for_run(self, **kw):
        tools = {f"t{i}": SimpleNamespace(candidate_id=c) for i, c in enumerate(self.cids)}
        return SimpleNamespace(reachable=tools, tools="the run's tools")


def test_forge_opener_with_a_candidate_needs_the_run_to_reach_it():
    missing = []
    opened = forge_opener(_Reaching("fc_abc"), INP, None, candidate="fc_abc",  # type: ignore[arg-type]
                          on_missing=lambda: missing.append(1))
    assert opened("r1", None) == "the run's tools" and missing == []  # type: ignore[arg-type]
    opened = forge_opener(_Reaching("fc_other"), INP, None, candidate="fc_abc",  # type: ignore[arg-type]
                          on_missing=lambda: missing.append(1))
    with pytest.raises(ForgeCandidateUnavailable):
        opened("r1", None)  # type: ignore[arg-type]
    assert missing == [1]
    # without a candidate the run is whatever the forge opened, as before
    assert forge_opener(_Reaching(), INP, None)("r1", None) == "the run's tools"  # type: ignore[arg-type]


@pytest.mark.parametrize("runtime", [_Runtime(), _Reaching("fc_other")],
                         ids=["switch_off_or_pin_failed", "candidate_not_loaded"])
def test_a_replay_whose_run_lacks_the_candidate_fails_closed(pool, runtime):
    """The candidate must be in the replay's run: else ReplayFailed("forge_unavailable") before any
    writer call, never a replay without it (the judge would compare noise)."""
    from dataclasses import replace

    from evowiki.harness.diff import KeyOp, PackDiff
    from evowiki.harness.replay import ReplayDeps, replay
    from tests.deep.fakes import DeepScript, simple_round
    from tests.harness.test_replay import World
    world = World(pool)
    ds = DeepScript().plan().write(*simple_round())
    case = world.recorded(ds)
    served = len(ds.served)
    diff = PackDiff((KeyOp("H3.deep.t1", True), KeyOp("H12.tool.deep.t1", {"candidate_id": "fc_abc"})))
    svc = replace(world.service(ds), forge=runtime)
    with pytest.raises(ReplayFailed) as ei:
        replay(case, diff, base_pack=EMPTY, base_spec=world.spec(case),
               deps=ReplayDeps(deep=svc, repo=world.repo))
    assert ei.value.failure == "forge_unavailable" and ei.value.detail == FORGE_UNAVAILABLE
    assert not any(c in ei.value.detail for c in "abcdefghijklmnopqrstuvwxyz")
    assert len(ds.served) == served                   # no model call


def test_candidate_without_a_forge_runtime_fails_closed():
    fake = SimpleNamespace(llm=object(), forge=None)
    with pytest.raises(ReplayFailed) as ei:
        DeepService.replay_section(  # type: ignore[arg-type]
            fake, "u", snapshot=None, page_id="p", section_id="s", spec=None, harness=None,
            forge_candidate="fc_abc")
    assert ei.value.failure == "forge_unavailable"
    assert ei.value.detail
