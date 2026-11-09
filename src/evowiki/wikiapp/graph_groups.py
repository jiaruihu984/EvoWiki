"""Names for the graph's automatic groups (spec 2026-10-29 retire-curate §5): settled by code when
it can (`settle`), else one model call, the prompt step `graph_group_naming` (purpose
`graph.group_naming`), soon after a graph change (`wikiapp.group_naming`); never on a read. The
call only names and describes: it creates no concept and never changes the graph.

The groups are those of the points on no goal (`goal_groups.outside_goals`; a goal's blocks have their real names), from
`nodes.cluster` (they change as the graph grows); a name is stored per member set (`GroupsNamed`, keyed by
`nodes.member_key`). Before the call, code settles what it can: `to_name` skips a group whose member set already has a name,
or whose members are >= 0.6 Jaccard-similar to a named group of the same level (it inherits that name,
`graph_out.inherited_name`); `settle` names a one-member group after its member, and a group that
is (nearly) the children of one part_of parent after that parent (not a parent much wider than the
group: 「数学基础」 over a group of z-score pieces is not its name). The rest are shown to the model
(`asks`) by the NAMES of their members (never ids) with the relations among them; a level-2 group
by the names and summaries of its level-1 groups (or 「第 n 组」 when that group is named in the same
call). The model answers by group number; `group_labels` maps the numbers back to member sets.

The names are stored against the graph as it is when they are committed (`names_for`): every group
of that graph without a name takes the exact or inherited name from the stored names and the new
ones. A group none fits keeps its fallback name (`graph_out`) until it is named. Until then GET
/wiki/graph serves the fallback names; no read makes a model call (D12)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from evowiki.goals.language import Language, language_rule
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import LLMRequest
from evowiki.nodes import Group, NodeGraph, cluster, member_key
from evowiki.wikiapp.events import GroupName, GroupsNamed, WikiEvent
from evowiki.wikiapp.goal_groups import outside_goals
from evowiki.wikiapp.graph_out import inherited_name, jaccard
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.state import WikiState

PURPOSE = "graph.group_naming"
PROMPT_VERSION = "group-naming-v1"
STEP = "graph_group_naming"
PARENT_JACCARD = 0.8      # a group is its parent's when it is (nearly) that parent's children

REL_LINE = {
    "part_of": "「{a}」是「{b}」的一部分",
    "prerequisite": "先学「{a}」再学「{b}」",
    "recommended": "学「{b}」之前建议先看「{a}」",
    "kind_of": "「{a}」是一种「{b}」",
    "related": "「{a}」和「{b}」相关",
}


class GroupLabel(BaseModel):
    """A name for one of the graph's automatic groups: its member ids and level, the name and its
    one-line summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    members: tuple[str, ...]
    level: int
    name: str
    summary: str = ""


@dataclass(frozen=True)
class GroupAsk:
    """A graph group to name: its member ids, its level (2: a big group of small ones) and how the
    prompt shows it (its members' names and relations, or its small groups)."""

    members: tuple[str, ...]
    level: int
    body: str


class GroupNameReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    n: int
    name: str
    summary: str = ""


class GroupNamesReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    groups: list[GroupNameReply] = []


def _name_record(g: Group, name: str, summary: str) -> GroupName:
    return GroupName(key=member_key(g.members), name=name, summary=summary, level=g.level,
                     members=tuple(sorted(g.members)))


def to_name(state: WikiState) -> tuple[list[Group], tuple[Group, ...]]:
    """(the groups with no name that inherit none, all groups). A level-2 group with the same
    members as a level-1 group is that group (one name per member set: `state.groups` is keyed
    by it), so it is listed once."""
    groups = cluster(outside_goals(state))
    unnamed: list[Group] = []
    seen: set[str] = set()
    for g in groups:
        key = member_key(g.members)
        if key in state.groups or key in seen:
            continue
        seen.add(key)
        if inherited_name(g.members, g.level, state.groups) is None:
            unnamed.append(g)
    return unnamed, groups


def _label(g: Group, name: str, summary: str) -> GroupLabel:
    return GroupLabel(members=tuple(g.members), level=g.level, name=name, summary=summary)


def _parent_of(graph: NodeGraph, members: Sequence[str]) -> str | None:
    """The one part_of parent the group is (nearly) the children of: every member but the parent
    itself sits under it, and the group and its children overlap >= PARENT_JACCARD."""
    inside = set(members)
    for p in sorted({p for m in members for p in graph.parents(m)}):
        rest = inside - {p}
        if not rest or not all(p in graph.parents(m) for m in rest):
            continue
        if jaccard(sorted(rest), graph.children(p)) >= PARENT_JACCARD:
            return p
    return None


def settle(state: WikiState, unnamed: Sequence[Group]) -> tuple[list[GroupLabel], list[Group]]:
    """(the names code gives, the groups left for the model): a one-member group is named after its
    member, a group that is its parent's children after that parent (name and definition)."""
    graph = state.graph
    labels: list[GroupLabel] = []
    rest: list[Group] = []
    for g in unnamed:
        node = graph.node(g.members[0]) if len(g.members) == 1 else None
        parent = _parent_of(graph, g.members) if node is None and g.level == 1 else None
        rec = node or (graph.node(parent) if parent is not None else None)
        if rec is not None and rec.name.strip():
            labels.append(_label(g, rec.name.strip(), rec.definition.strip()))
        else:
            rest.append(g)
    return labels, rest


def _node_name(graph: NodeGraph, node_id: str) -> str:
    rec = graph.node(node_id)
    return rec.name if rec is not None else "（未命名）"


def _relation_lines(graph: NodeGraph, members: Sequence[str]) -> list[str]:
    inside = set(members)
    out = []
    for r in graph.relations:
        if r.src in inside and r.dst in inside:
            line = REL_LINE[r.kind].format(a=_node_name(graph, r.src), b=_node_name(graph, r.dst))
            plain = line.replace("「", "").replace("」", "")
            out.append(line + (f"（{r.phrase}）" if r.phrase and r.phrase not in plain else ""))
    return out


def _pool(state: WikiState, labels: Sequence[GroupLabel]) -> dict[str, GroupName]:
    """The stored names (they win) plus `labels` as names, by member key."""
    return {**{member_key(x.members): GroupName(key=member_key(x.members), name=x.name,
                                                summary=x.summary, level=x.level,
                                                members=tuple(sorted(x.members)))
               for x in labels}, **state.groups}


def asks(state: WikiState, unnamed: Sequence[Group], groups: Sequence[Group] = (),
         settled: Sequence[GroupLabel] = ()) -> list[GroupAsk]:
    """`unnamed` as the model is shown them, numbered 1.. in the given order. `groups`: all
    groups (a level-2 group shows its level-1 groups from here); `settled`: the names code gave
    in this round (`settle`), shown like stored ones."""
    graph = state.graph
    known_names = _pool(state, settled)
    number = {g.id: i for i, g in enumerate(unnamed, 1)}
    out = []
    for g in unnamed:
        if g.level == 2:
            lines = []
            for k in (k for k in groups if k.parent == g.id):
                if k.id in number:
                    lines.append(f"- 第 {number[k.id]} 组（这次一起起名）")
                    continue
                known = inherited_name(k.members, k.level, known_names)
                if known is not None:
                    lines.append(f"- {known.name}：{known.summary}" if known.summary
                                 else f"- {known.name}")
                else:
                    lines.append("- " + "、".join(_node_name(graph, m) for m in k.members))
            body = "包含的小组：\n" + "\n".join(lines)
        else:
            body = "知识点：" + "、".join(_node_name(graph, m) for m in g.members)
            rels = _relation_lines(graph, g.members)
            if rels:
                body += "\n它们之间的关系：\n" + "\n".join(f"- {x}" for x in rels)
        out.append(GroupAsk(members=tuple(g.members), level=g.level, body=body))
    return out


SYSTEM = "你给一个学习者的知识图谱里自动分出来的组起名字。只回复 JSON。"
ZH_RULE = "用中文写，不要中英文夹杂；常用缩写（如 PCA）可以保留。"


def groups_block(groups: Sequence[GroupAsk]) -> str:
    return "\n\n".join(f"第 {i} 组（{'大块' if g.level == 2 else '小组'}）\n{g.body}"
                       for i, g in enumerate(groups, 1))


def naming_request(groups: Sequence[GroupAsk], *, language: Language = "zh",
                   user_id: str = "") -> LLMRequest:
    """The one naming call for `groups` (spec §5). `subject` is the user: the call log (and its
    cache) is the user's own."""
    lang = ZH_RULE if language == "zh" else language_rule(language)
    prompt = f"""下面是知识图谱里自动分出来、还没有名字的组。每组起一个名字，写一句描述。

⟦g:{STEP}⟧- 名字：名词短语，一般 2–8 个字，概括这组知识共同讲的东西，例如「数据标准化」「矩阵运算」。不要用「学会」「理解」「我要」开头，不要写成一句任务，也不要只是把成员名连起来。
- 描述：一句话，写这组知识之间怎么联系，或者学完它们能解决什么问题；不要把成员名再列一遍。
  例：成员是均值、标准差、z 分数、正态分布时，不要写「本组包括均值、标准差、z 分数和正态分布」，要写「理解如何将数据标准化，并利用正态分布计算概率」。
- 只根据下面给出的知识点和它们的关系来写，不要加入组里没有的知识。⟦/g⟧

{groups_block(groups)}

{lang}
每组都要回答，用组号 n 对应。只回复 JSON：{{"groups":[{{"n":1,"name":"…","summary":"…"}}]}}"""
    return LLMRequest(purpose=PURPOSE, prompt_version=PROMPT_VERSION,
                      subject=f"user:{user_id}" if user_id else "user:?",
                      system=SYSTEM, prompt=prompt)


def group_labels(reply: Sequence[GroupNameReply],
                 groups: Sequence[GroupAsk]) -> tuple[GroupLabel, ...]:
    """The reply's names mapped back to the groups asked; ValueError when they cannot be used (an
    unknown or repeated number, an empty name, a group left out)."""
    seen: dict[int, GroupNameReply] = {}
    for item in reply:
        if not 1 <= item.n <= len(groups):
            raise ValueError(f"the reply names group {item.n}, which was not asked")
        if item.n in seen:
            raise ValueError(f"the reply names group {item.n} twice")
        if not item.name.strip():
            raise ValueError(f"the reply gives group {item.n} an empty name")
        seen[item.n] = item
    missing = sorted(set(range(1, len(groups) + 1)) - set(seen))
    if missing:
        raise ValueError(f"the reply leaves out group(s) {missing}")
    return tuple(GroupLabel(members=g.members, level=g.level, name=seen[n].name.strip(),
                            summary=seen[n].summary.strip())
                 for n, g in enumerate(groups, 1))


def name_groups(gateway: Gateway, groups: Sequence[GroupAsk], *, language: Language = "zh",
                user_id: str = "") -> tuple[GroupLabel, ...]:
    """The names of `groups` from one call. ValueError when the names cannot be used
    (`group_labels`); gateway errors propagate."""
    if not groups:
        return ()
    reply, _ = gateway.call("generator", naming_request(groups, language=language,
                                                        user_id=user_id), GroupNamesReply)
    return group_labels(reply.groups, groups)


def names_for(state: WikiState, labels: Sequence[GroupLabel]) -> list[GroupName]:
    """The names to store for `state`'s groups that have none: the exact or inherited name from
    the stored names (they win) and `labels` (the new ones, for the groups of the graph they were
    made for). A group none fits is left out."""
    pool = _pool(state, labels)
    out: list[GroupName] = []
    seen: set[str] = set()
    for g in cluster(outside_goals(state)):
        key = member_key(g.members)
        if key in state.groups or key in seen:
            continue
        seen.add(key)
        known = inherited_name(g.members, g.level, pool)
        if known is not None:
            out.append(_name_record(g, known.name, known.summary))
    return out


def named_event(state: WikiState, labels: Sequence[GroupLabel], *, now: datetime,
                new_id: Ids) -> list[WikiEvent]:
    """The `GroupsNamed` event of `names_for` (none when nothing gets a name)."""
    names = names_for(state, labels)
    return [WikiEvent(event_id=new_id("wev"), ts=now, body=GroupsNamed(names=tuple(names)))
            ] if names else []
