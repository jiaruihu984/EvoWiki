"""Real leaks of the 2026-11-04 walk (assistant.plain_ids)."""

from evowiki.assistant.plain_ids import (
    has_internal_ids,
    id_tokens,
    leaked_ids,
    plain_ids,
    unquoted_ids,
)

NODES = {"c2": "网络表示与基础量", "c10": "连通分量", "c11": "巨分量", "n_5f17be5b7648": "DFS"}
SECTIONS = [("s1", "怎么数"), ("s2", "怎么写"), ("s3", "度分布")]
PAGES = {"pg_fb3b10156ba34a29bd1d": "巨分量"}


def _plain(text):
    return plain_ids(text, nodes=NODES, sections=SECTIONS, pages=PAGES)


def test_the_logged_leaks_read_as_names():
    assert _plain("对应概念 c11。") == "对应概念 「巨分量」。"
    assert _plain("「连通分量」（c10）挂在网络表示与基础量（c2）下面") == "「连通分量」挂在网络表示与基础量下面"
    assert _plain("原页的开头和第 s1–s3 段属于这里。") == "原页的开头和第 1 到第 3 节属于这里。"
    assert _plain("它在页面里是 s1 段。") == "它在页面里是 「怎么数」。"
    assert _plain("对应的知识点是 c11 巨分量。") == "对应的知识点是 巨分量。"
    assert _plain("下级：DFS（n_5f17be5b7648）。") == "下级：DFS。"
    assert _plain("放「数一数」（s2）。") == "放「数一数」。"
    assert _plain("页面 pg_fb3b10156ba34a29bd1d 里") == "页面 《巨分量》 里"


def test_what_is_not_an_id_of_his_stays():
    for text in ("[巨分量](#p:pg_fb3b10156ba34a29bd1d) 这一页", "`c10` 和 $s1$ 是变量", "设 c99 为常数，s9 同理",
                 "c10.term", "〔讲义 p.3｜3｜src1：「see c10」〕"):
        assert _plain(text) == text
    assert not has_internal_ids("[巨分量](#p:pg_fb3b10156ba34a29bd1d) `c10`")
    assert has_internal_ids("对应概念 c11")


KNOWN = {"s1", "s2", "s3", "c8"}
TOOLS = {"propose_page_edit", "load_skill", "confirm"}


def _left(text, literal=()):
    return leaked_ids(text, known=KNOWN, tools=TOOLS, literal=literal)


def test_the_baselines_leaks_are_found():
    # run 110500597858, edit-request-draft trial 3: quoted, and with no page on the session
    assert _left("我在小标题后写了「（段 id：s2）」「（段 id：s3）」，这些是给我自己看的编号") == ["s2", "s3"]
    # audit pages.md 待审核 6: in backticks
    assert _left("现在能在页面上 `s4` 那一段看到") == []        # not a section of this page
    assert _left("现在能在页面上 `s1` 那一段看到") == ["s1"]
    assert _left("调用 propose_page_edit 时出错，记录是 act_01811a07，页面 pg_fb3b10156ba3") == [
        "act_01811a07", "pg_fb3b10156ba3", "propose_page_edit"]
    assert _left("这条记忆（mem_5f17be5b76）和复习卡 rc_09ab12cd34，草稿 po_prop_d5c1bcd48c76") == [
        "mem_5f17be5b76", "po_prop_d5c1bcd48c76", "rc_09ab12cd34"]
    assert _left("学习路径 path-fbb60a98f22b 和目标 map-03dc73fe684c") == ["map-03dc73fe684c", "path-fbb60a98f22b"]


def test_literal_content_is_not_a_leak():
    # code he reads: the block, and the same name mentioned around it
    assert _left("```python\ns1 = 'abc'\nload_skill()\n```") == []
    assert _left("```python\ns1 = 'abc'\n```\n这里 s1 是字符串，`s1` 只读一次。") == []
    assert _left("设 $s1$ 为路径长度，$c8$ 同理。") == []
    assert _left("〔讲义 p.3：「see s1 and c8」〕") == []
    assert _left("[节点与边](#p:pg_fb3b10156ba3) 这一页") == []
    # his own words, the page's own text
    assert _left("你说的 s1 和 s2 是两个字符串。", literal=id_tokens("字符串 s1 和 s2 的最长公共子序列")) == []
    assert _left("页面写着 c8 电容接地。", literal=id_tokens("c8 电容接地，s9 断开")) == []
    # shapes that are nobody's id here
    assert _left("C8 烷烃，s9 开关，confirm 这个英文词，file_decade 变量") == []
    assert _left("x_m 和 coffee_trial 都不是内部名字") == []


def test_inline_code_holding_an_id_reads_as_its_name():
    assert unquoted_ids("在 `s1` 那一段，`s1 = 3` 不动", {"s1"}) == "在 s1 那一段，`s1 = 3` 不动"
    assert _plain(unquoted_ids("在 `s1` 那一段", {"s1"})) == "在 「怎么数」 那一段"
