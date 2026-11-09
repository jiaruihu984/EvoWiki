import pytest

from evowiki.wikiapp.guidelines import add_guideline, list_guidelines, remove_guideline
from evowiki.wikiapp.kwrite import page_spec
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.wiki_schemas import GuidelineIn, GuidelineOut
from tests.wikiapp.fake_writer import PLAN_PAGE as ORGANIZE
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_organize import page1, run, script_page


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def state(env):
    return env[0].load("u1").state


def add(env, **body) -> GuidelineOut:
    repo, _, ids = env
    return add_guideline(repo, "u1", GuidelineIn(**body), now=NOW, new_id=ids)


def test_add_list_remove_user_guideline(env):
    repo, _, ids = env
    g = add(env, text="  多用图  ")
    assert (g.text, g.source, g.evidence) == ("多用图", "user", None)
    assert list_guidelines(state(env)).items == [g]
    assert state(env).principles() == ("多用图",)
    remove_guideline(repo, "u1", g.guideline_id, now=NOW, new_id=ids)
    assert list_guidelines(state(env)).items == [] and state(env).principles() == ()
    with pytest.raises(LookupError):
        remove_guideline(repo, "u1", g.guideline_id, now=NOW, new_id=ids)


def test_added_guidelines_reach_the_organizer(env):
    repo, s, _ids = env
    pid = page1(env)
    add(env, text="先给一个具体例子再讲定义")
    add(env, text="多用图")
    lines = page_spec(state(env), repo.load("u1").k, pid).guidelines    # defaults, user items, params
    assert lines[-6:-4] == ("先给一个具体例子再讲定义", "多用图")
    script_page(s)
    run(env, pid)
    prompt = s.requests(ORGANIZE)[0].prompt
    assert "先给一个具体例子再讲定义" in prompt and "多用图" in prompt
