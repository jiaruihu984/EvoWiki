from tests.llm_cases.evalset import spec
from tests.llm_cases.evalset.seal import _chunks


def test_sealing_splits_every_case_file_back_into_the_same_text():
    for f in spec.CASES.glob("*.yaml"):
        text = f.read_text(encoding="utf-8")
        head, chunks = _chunks(text)
        assert head + "".join(chunks) == text
        assert len(chunks) == sum(1 for ln in text.splitlines() if ln.startswith("- id:")), f.name


def test_each_case_text_parses_alone_and_variants_of_a_failure_share_a_unit():
    import yaml
    for f in spec.CASES.glob("*.yaml"):
        for ch in _chunks(f.read_text(encoding="utf-8"))[1]:
            assert yaml.safe_load(ch)[0]["id"]     # no alias to another case: a chunk moves alone
    a = {"topic": "渗流", "source": {"ref": "决定清单 3 变体：另一页"}}
    b = {"topic": "渗流", "source": {"ref": "决定清单 3"}}
    assert spec.unit_of(a) == spec.unit_of(b)
