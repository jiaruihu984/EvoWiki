"""Chat-first (2026-10-22): the jobs of removed plugin buttons done from the assistant's chat, on
real Postgres with the real ledger + executors and a SCRIPTED LLM - nothing here calls a paid API.

- `set_exemplar` (设置 → 样板页 / 设为 EvoWiki 样板页): a ledger row executed at once, undoable;
- `read_review_digest` (本周回顾): a read, no row, no model call;
- the prompt asks which part before changing a page called too easy / too hard."""

from dataclasses import replace
from datetime import timedelta

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.assistant.loop import CORE_RULES, PROMPT_VERSION, SYSTEM
from evowiki.assistant.skills import library
from evowiki.assistant.tools.actions import build_action_tools
from evowiki.assistant.tools.read_more import ReadDeps, build_read_tools
from evowiki.bot.actions import NotDecidable
from evowiki.bot.settings import BotSettings
from evowiki.llm.prompt_steps import marked
from evowiki.sources import store as sources_store
from evowiki.sources.models import SectionRow, SourceRow
from evowiki.sources.reader import PooledSourceReader
from evowiki.store import bot as store
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.repo import PgRepo
from evowiki.wikiapp.review import grade_review
from evowiki.wikiapp.teach import add_exemplar_flow
from evowiki.wikiapp.wiki_schemas import ReviewGradeIn, VaultNoteExemplarIn
from tests.assistant.fakes import ungrounded
from tests.assistant.test_action_tools import ALL_ASK, Env
from tests.runtime.helpers import make_user
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import Ids as GoalIds
from tests.wikiapp.test_review_flow import LATER, organized_learned_page

NOTE = "笔记/范例.md"


def ctx(page_id=None):
    return ToolContext(user_id="u1", session_id="s1", page_id=page_id)


def add_note(pool, sid="note1", user="u1", status="ready"):
    """A vault note the plugin sent to the chat: an `md` material with heading sections."""
    row = SourceRow(source_id=sid, user_id=user, kind="md", title="我的范例", sha256=sid, bytes=1,
                    pages=1, status=status, error=None, unreadable_pages=(), toc_mode="headings",
                    progress={}, origin=NOTE, created_at=LATER, updated_at=LATER,
                    deleted_at=None)
    secs = [("前面的内容", 0, "开头一段。"), ("例子先行", 0, "先给例子。"), ("再讲公式", 1, "然后是公式。")]
    with pool.connection() as conn:
        sources_store.insert_source(conn, row, b"x", "text/markdown")
        sources_store.replace_sections(conn, user, sid, [SectionRow(
            section_id=f"{sid}-{i}", source_id=sid, parent_id=None, ord=i, depth=d, title=t,
            page_from=None, page_to=None, text=x, tokens=1, summary=None, read_by="text")
            for i, (t, d, x) in enumerate(secs)])


def setup(pool, settings: BotSettings = ALL_ASK) -> Env:
    """An organized, learned page (concepts c1, c2) at LATER; the materials reader on."""
    make_user(pool, "u1")
    ids, repo, s = GoalIds(), PgRepo(pool), Script()
    pid = organized_learned_page((repo, s, ids))
    with pool.connection() as conn:
        store.put_settings(conn, "u1", settings, LATER)
    e = Env(pool, repo, ids, {"s": s}, LATER)
    e.deps = replace(e.deps, sources=PooledSourceReader(pool, None))
    e.tools = {t.spec.name: t for t in ungrounded(build_action_tools(e.deps))}
    e.pid = pid
    return e


@pytest.fixture
def env(pool):
    return setup(pool)


def exemplars(e: Env):
    return tspec.effective_spec(e.state()).exemplars


# --- set_exemplar ------------------------------------------------------------------------------

def test_the_open_page_becomes_an_exemplar_at_once_and_undo_takes_it_out(env):
    res = env.tools["set_exemplar"].run({}, ctx(env.pid))
    assert res.error is None, res.content
    row = env.ledger.get("u1", res.action.action_id)
    assert (row.kind, row.scope, row.level, row.status) == ("exemplar", "teaching_params",
                                                            "auto_undo", "executed")
    assert row.content == {"kind": "wiki_page", "page_id": env.pid}
    assert res.action.title == "把「z 分数」设为样板页"
    assert not res.action.confirm and res.action.can_undo
    assert "已执行" in res.content and "可以在卡片上撤销" in res.content
    [ex] = exemplars(env)
    assert (ex.kind, ex.ref, ex.exemplar_id) == ("wiki_page", env.pid, row.undo["exemplar_id"])
    assert ex.text.startswith("# z 分数")

    done = env.run("undo", action_id=row.action_id)
    assert done.error is None and "已撤销" in done.content, done.content
    assert exemplars(env) == ()
    assert env.ledger.get("u1", row.action_id).status == "undone"
    with pytest.raises(NotDecidable):                         # undone: nothing more to undo
        env.ledger.undo("u1", row.action_id)


def test_a_note_sent_to_the_chat_becomes_an_exemplar_with_its_headings(env):
    add_note(env.pool)
    res = env.tools["set_exemplar"].run({"source_id": "note1"}, ctx())
    assert res.error is None, res.content
    assert res.action.title == "把「我的范例」设为样板页"
    [ex] = exemplars(env)
    assert (ex.kind, ex.ref, ex.title) == ("vault_note", NOTE, "我的范例")
    assert ex.text == "开头一段。\n\n# 例子先行\n\n先给例子。\n\n## 再讲公式\n\n然后是公式。"
    assert env.run("undo", action_id=res.action.action_id).error is None
    assert exemplars(env) == ()


def test_bad_exemplar_requests_are_tool_errors_and_write_nothing(env, pool):
    add_note(pool, "reading", status="reading")
    make_user(pool, "u2")
    add_note(pool, "theirs", user="u2")
    for args, c, says in [
        ({}, ctx(), "要说明是哪一页"),
        ({"page_id": "nope"}, ctx(), "没有这一页：nope"),
        ({"page_id": env.pid, "source_id": "note1"}, ctx(), "只能给一个"),
        ({"source_id": "theirs"}, ctx(), "没有这份笔记：theirs"),
        ({"source_id": "reading"}, ctx(), "还没读完"),
    ]:
        res = env.tools["set_exemplar"].run(args, c)
        assert res.error is not None and res.action is None, (args, res.content)
        assert says in res.content, res.content
    assert env.rows() == [] and exemplars(env) == ()


def test_the_same_page_twice_and_a_fourth_exemplar_are_refused(env):
    assert env.tools["set_exemplar"].run({}, ctx(env.pid)).error is None
    twice = env.tools["set_exemplar"].run({"page_id": env.pid}, ctx())
    assert twice.error is not None and "已经是样板页" in twice.content
    for i in range(2):
        add_exemplar_flow(env.repo, "u1", VaultNoteExemplarIn(
            kind="vault_note", ref=f"n{i}.md", title=f"笔记{i}", text="正文"), now=LATER,
            new_id=env.ids)
    add_note(env.pool)
    full = env.tools["set_exemplar"].run({"source_id": "note1"}, ctx())
    assert full.error is not None and "样板页最多 3 个" in full.content, full.content
    assert "「z 分数」" in full.content and len(exemplars(env)) == 3
    assert len(env.rows()) == 1


def test_undo_after_the_exemplar_was_removed_elsewhere_is_refused(env):
    from evowiki.wikiapp.teach import remove_exemplar_flow
    res = env.tools["set_exemplar"].run({}, ctx(env.pid))
    [ex] = exemplars(env)
    remove_exemplar_flow(env.repo, "u1", ex.exemplar_id, now=LATER, new_id=env.ids)
    out = env.run("undo", action_id=res.action.action_id)
    assert out.error is not None and "已经去掉了" in out.content


# --- read_review_digest (本周回顾) ---------------------------------------------------------------

def test_the_week_digest_is_read_with_no_row_and_no_model_call(env):
    grade_review(env.repo, None, "u1", ReviewGradeIn(cell="c1.interpret", grade="forgot"),
                 now=LATER, new_id=env.ids)
    reads = {t.spec.name: t for t in build_read_tools(
        ReadDeps(env.repo, lambda: LATER + timedelta(hours=1)))}
    tool = reads["read_review_digest"]
    assert tool.spec.effect == "read"
    res = tool.run({}, ctx())
    assert res.error is None, res.content
    lines = res.content.split("\n")
    assert lines[0].startswith("这一周（") and lines[0].endswith("的学习情况：")
    assert lines[1].startswith("进步了：")
    assert lines[2].startswith("退步了：") and "解读结果" in lines[2] and "忘了" in lines[2]
    assert lines[3].startswith("快忘了：")
    assert lines[4] == "复习：想起来了 0 次，有点模糊 0 次，没想起来 1 次"
    assert lines[5].startswith("今天：到期 ") and "已做 1 个" in lines[5]
    day = tool.run({"period": "day"}, ctx())
    assert day.content.startswith("今天（")
    assert tool.run({"period": "month"}, ctx()).error is not None
    assert env.rows() == []                     # a read: no ledger row (and nothing scripted:
    #                                             any model call would have failed the tool)


# --- prompt ------------------------------------------------------------------------------------

def _all_rules() -> str:
    """The general change rules and every Skill's rules and references (stage 3: ACTION_RULES /
    GOAL_RULES moved there verbatim)."""
    lib = library()
    return "\n".join([CORE_RULES, *(s.rules for s in lib.skills),
                      *(r.body for s in lib.skills for r in s.refs.values())])


ACTION_RULES = _all_rules()


def test_the_prompt_asks_which_part_first_and_names_the_new_tools():
    assert PROMPT_VERSION == "assistant@v80"
    guidance = marked(SYSTEM)["assistant"]              # the user's 回答原则 1–9 (assistant@v55)
    assert guidance.startswith("1. **理解用户意图**：先判断学习者是在询问知识")
    assert "只有确实无法判断他卡在哪里时才追问" in guidance
    assert "他自己说的话只有这一条消息最后「他问：」后面那段" in guidance
    assert "## 输出与操作" in SYSTEM and "## 输出与操作" not in guidance
    assert "每页的标题是点的名字" not in ACTION_RULES      # pages and points are independent
    assert "set_exemplar" in ACTION_RULES
    from evowiki.assistant.skills import CORE
    assert "read_review_digest" in CORE           # stage 3: a core read tool, its text is enough
    # chat-first blocks: a new goal is split in the chat, its blocks are changed by the tool
    # assistant@v23: look up first, nothing written before new_goal (the card's lead is the
    # blocks call's framing + caveat), the rest after it
    for rule in ("目标里的东西你说不准指的是什么（俗称、商品名、小众领域、要看地区或版本的）时，先调 web_search",
                 "放进 new_goal 的 notes",
                 "调 new_goal：goal 写他的原话；他为这个目标说的其他话（条件、会什么、不会什么）逐句一字不改地抄进 words",
                 "他已经说先不做的旧请求不抄",
                 "调用之前不写字：卡片前的话由分块那一步写好",
                 "在回答里用一两句话说为什么这样改（new_goal 除外）",
                 "卡片出来以后（工具结果之后）接着写，每样一句：主线（先学哪几块、哪块可以一起学、哪块留到后面）",
                 "最多一个会改变分块的问题，选项写在问句里",
                 "不在文字里把各块再列一遍（卡片上有），不排日程，不编学时、日期和数字，不讲过程",
                 "合并 / 不学 / 改名 / 加一块 / 调顺序 / 改分级）→ 调 edit_goal_blocks",
                 "他回答了分块时问的问题：答案要换排法（某几块提前、加一块）就调 edit_goal_blocks；不用改就不调，只回一句",
                 "合并时说清原来几块里的东西现在在哪一块",
                 "去掉一块时说按什么标准去掉", "改了对他基础或目的的假设时，把新的假设说出来",
                 "只说改了什么，不重列分块；不是非问不可就不再问"):
        assert rule in ACTION_RULES, rule
    for gone in ("调用之前只写一两句", "第一句直接说怎么改"):
        assert gone not in ACTION_RULES, gone
