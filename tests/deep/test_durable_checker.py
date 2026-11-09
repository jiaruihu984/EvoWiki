from evowiki.agent import durable
from evowiki.deep.contracts import InteractiveDraft, Probe
from evowiki.deep.durable import DurableChecker, DurableLibrary
from evowiki.deep.library import MemoryLibrary
from tests.deep.fakes import HTML, FakeChecker, component


def _spy(monkeypatch):
    names: list[str] = []
    real = durable.step_ran
    monkeypatch.setattr(durable, "step_ran", lambda n, fn: (names.append(n), real(n, fn))[1])
    return names


def test_checker_results_equal_the_inner_ones_and_are_steps(monkeypatch):
    names = _spy(monkeypatch)
    chk = DurableChecker(FakeChecker())
    draft = InteractiveDraft(interactive_id="i1", section_id="s1", title="t",
                             learning_question="q?", html=HTML, height=400, probes=(Probe(target="#b", action="click", expect_change="#out"),))
    with durable.virtual_workflow("wf"):
        a = chk.check(draft)
        b = chk.check_block("b1", None)
    assert a == FakeChecker().check(draft) and b == FakeChecker().check_block("b1", None)
    assert names == ["render.check", "render.check_block"]


def test_checker_outside_a_workflow_is_a_plain_call():
    inner = FakeChecker()
    assert DurableChecker(inner).check_block("b1", None).ok and inner.checked == ["b1"]


def test_library_list_roundtrips(monkeypatch):
    names = _spy(monkeypatch)
    lib = DurableLibrary(MemoryLibrary([component("aa"), component("bb")]))
    with durable.virtual_workflow("wf"):
        got = lib.list()
        one = lib.get("aa")
        none = lib.get("zz")
    assert got == [component("aa"), component("bb")] and one == component("aa") and none is None
    assert names == ["library.list", "library.get", "library.get"]
    lib.put(component("cc"))
    assert [c.component_id for c in lib.list()] == ["aa", "bb", "cc"]
