"""The judge's cache survives what broke it: a shared file written by several runs at once."""
import json

from evowiki.llm.models import ModelRef
from tests.llm_cases.evalset.judge import Judge, Verdict, append_line, load_cache, own_segment


def test_a_line_that_does_not_read_is_skipped_and_the_rest_are_kept(tmp_path):
    cache = tmp_path / "judge-cache.jsonl"
    good = json.dumps({"key": "a", "yes": True, "reason": "r"}, ensure_ascii=False).encode()
    cache.write_bytes(good + b"\n{\"key\": \"b\", \"reason\": \"\xbf\xe6\"}\nnot json\n")
    assert set(load_cache(cache)) == {"a"}


def test_each_judge_writes_its_own_file_and_all_of_them_are_read(tmp_path):
    cache = tmp_path / "judge-cache.jsonl"
    one, two = own_segment(cache), own_segment(cache)
    assert one != two and not cache.exists()
    append_line(one, {"key": "a", "yes": True, "reason": "中文"})
    append_line(two, {"key": "b", "yes": False, "reason": "也是中文"})
    got = load_cache(cache)
    assert got["a"]["reason"] == "中文" and got["b"]["yes"] is False


def test_language_has_its_own_prompt_and_cache_for_identical_question_and_text(tmp_path):
    requests = []
    class Gateway:
        def call(self, role, request, schema, **kwargs):
            requests.append(request)
            return Verdict(reason="evidence", yes=True), None
    judge = Judge.__new__(Judge)
    judge.ref = ModelRef(provider="deepseek", model="fake-judge")
    judge.gateway, judge.cache, judge.asked = Gateway(), {}, 0
    judge.seconds = 0.0
    judge.own_cache = own_segment(tmp_path / "cache.jsonl")
    judge.ask("criterion", "answer", language="zh")
    judge.ask("criterion", "answer", language="en")
    judge.ask("criterion", "answer", language="en")
    assert len(requests) == 2 and judge.asked == 2 and len(judge.cache) == 2
    assert "English answer" in requests[1].system
    assert requests[0].subject != requests[1].subject
