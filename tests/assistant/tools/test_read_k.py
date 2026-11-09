from evowiki.agent.read_k import ReadKTool
from evowiki.belief.aspects import AspectKView
from evowiki.goals.observe import k_text
from tests.assistant.tools.conftest import T0


def test_reads_cells_like_the_organizer_does(aspect_k, ctx):
    kv = AspectKView(aspect_k, T0)
    r = ReadKTool(kv, names={"zscore": "z 分数"}).run({"concepts": ["zscore"]}, ctx)
    assert r.error is None
    assert k_text(kv, ["zscore"], {"zscore": "z 分数"}) in r.content
    assert "zscore.procedure" in r.content and "m=" in r.content


def test_unknown_concept_is_reported_not_fatal(aspect_k, ctx):
    r = ReadKTool(AspectKView(aspect_k, T0)).run({"concepts": ["nope", "stdev"]}, ctx)
    assert r.error is None and "nope" in r.content and "stdev.term" in r.content


def test_all_unknown_is_error(aspect_k, ctx):
    assert ReadKTool(AspectKView(aspect_k, T0)).run({"concepts": ["nope"]}, ctx).error


def test_bad_args_and_cap(aspect_k, ctx):
    t = ReadKTool(AspectKView(aspect_k, T0))
    assert t.run({"concepts": []}, ctx).error
    assert t.run({"concepts": [""]}, ctx).error                     # empty concept id
    assert t.run({"concepts": ["a"] * 9}, ctx).error                # at most 8
    assert len(t.run({"concepts": ["zscore", "stdev"]}, ctx).content) <= 4000


def test_kview_failure_becomes_error(ctx):
    class Broken:
        def aspects(self, c):
            raise RuntimeError("k store down")
    r = ReadKTool(Broken()).run({"concepts": ["zscore"]}, ctx)
    assert r.error and "RuntimeError" in r.error


def test_concepts_field_is_described(aspect_k):
    props = ReadKTool(AspectKView(aspect_k, T0)).spec.parameters["properties"]
    assert "read_page" in props["concepts"]["description"]
