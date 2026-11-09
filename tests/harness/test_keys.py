from evowiki.harness.keys import KEY_BY_NAME, KEYS, h1_key, h2_key, is_h2
from evowiki.llm.prompt_steps import STEPS


def test_every_step_has_an_h1_key():
    for s in STEPS:
        k = KEY_BY_NAME[h1_key(s.key)]
        assert (k.dim, k.kind) == ("H1", "text")


def test_names_are_unique_and_dims_match_prefix():
    names = [k.name for k in KEYS]
    assert len(names) == len(set(names))
    for k in KEYS:
        assert k.name.startswith(k.dim + ".")


def test_int_keys_have_bounds():
    for k in KEYS:
        if k.kind == "int":
            assert k.lo is not None and k.hi is not None and k.lo <= k.hi


def test_h2_names():
    assert is_h2(h2_key("deep", "write_block"))
    assert is_h2(h2_key("assistant", "search_wiki", "query"))
    assert not is_h2("H2.other.tool")
    assert not is_h2("H2.deep.Bad")
