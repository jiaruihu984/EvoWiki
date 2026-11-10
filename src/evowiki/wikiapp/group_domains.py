"""The level above goals on the 知识图谱 (ruling 2026-11-04 「大类让模型分」): the model groups the top groups (goals, and the clusters of what
is on no goal) into broad domains such as 生物, 物理. One call, in the graph's background naming job (`group_naming`), when the set of top
groups changes; never on a read. The result is stored (`DomainsGrouped`) with the top groups it was made for; until a new one lands the last
one stands, for those of its groups that still exist. A failed call keeps it and is logged.

No level is invented: a domain holds at least two groups, a group alone stands as itself, and when every group falls in one domain there is
nothing to tell apart, so there is no domain level."""

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from evowiki.goals.language import Language, language_rule
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import LLMRequest
from evowiki.nodes import member_key
from evowiki.wikiapp.events import Domain
from evowiki.wikiapp.graph_out import groups_out, placed
from evowiki.wikiapp.group_places import top_groups
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import DomainOut, GroupOut

PURPOSE = "graph.group_naming"
PROMPT_VERSION = "domain-grouping-v2"
MAX_NAMES = 12            # a group as the model is shown it: its name and what it holds

DOMAIN_NAMING_GUIDE = """⟦g:domain_naming⟧每个领域用简明、具体的名词起名，让学习者一眼看出它涵盖什么。
描述用一句自然的话说明这个领域共同讨论的内容，不罗列成员，也不写学习任务或口号。⟦/g⟧"""


class DomainReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    summary: str = ""
    groups: list[int] = []


class DomainsReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    domains: list[DomainReply] = []


def tops_now(state: WikiState) -> tuple[list[GroupOut], list[GroupOut]]:
    """(the top groups, all groups) of the graph as served."""
    groups = groups_out(state, placed(state.graph))
    return top_groups(groups), groups


def needs_grouping(state: WikiState) -> bool:
    return bool(state.graph.nodes) and tuple(sorted(g.id for g in tops_now(state)[0])) != state.domains_for


def domain_request(tops: Sequence[GroupOut], groups: Sequence[GroupOut], names: dict[str, str], *, language: Language = "zh",
                   user_id: str = "") -> LLMRequest:
    def line(i: int, t: GroupOut) -> str:
        kids = [g.name for g in groups if g.level == 1 and g.parent == t.id] or [names[m] for m in t.members if m in names]
        return f"{i}. {t.name}：" + "、".join(kids[:MAX_NAMES])

    listing = "\n".join(line(i, t) for i, t in enumerate(tops, 1))
    prompt = f"""下面是一个学习者知识图谱里的大块，每行是一块的名字和它包含的内容。把属于同一个宽领域的大块归到一起，比如生物、物理、数学、计算机、生活技能。

{listing}

每个领域起一个名字，写一句描述。和别的大块都不同领域的，不用列。
{DOMAIN_NAMING_GUIDE}

{language_rule(language)}
只回复 JSON：{{"domains":[{{"name":"…","summary":"…","groups":[1,2]}}]}}"""
    return LLMRequest(purpose=PURPOSE, prompt_version=PROMPT_VERSION, subject=f"user:{user_id}" if user_id else "user:?",
                      system="你给一个学习者的知识图谱分宽领域。只回复 JSON。", prompt=prompt)


def domains_of(reply: Sequence[DomainReply], tops: Sequence[GroupOut]) -> tuple[Domain, ...]:
    """The reply's domains by group id; ValueError when it cannot be used (an unknown number, a group in two domains, an empty name). A domain
    of one group is that group; all groups in one domain is no level."""
    seen: set[int] = set()
    out: list[Domain] = []
    for d in reply:
        numbers = list(dict.fromkeys(d.groups))
        for n in numbers:
            if not 1 <= n <= len(tops):
                raise ValueError(f"the reply puts group {n} in a domain, which was not asked")
            if n in seen:
                raise ValueError(f"the reply puts group {n} in two domains")
        seen.update(numbers)
        if len(numbers) < 2:
            continue
        if not d.name.strip():
            raise ValueError("the reply gives a domain an empty name")
        kids = tuple(tops[n - 1].id for n in numbers)
        out.append(Domain(id=f"d-{member_key(kids)}", name=d.name.strip(), summary=d.summary.strip(), kids=kids))
    if len(out) == 1 and len(out[0].kids) == len(tops):
        return ()
    return tuple(out)


def group_domains(gateway: Gateway, state: WikiState, user_id: str = "") -> tuple[tuple[str, ...], tuple[Domain, ...]]:
    """(the top groups grouped, their domains) from one call; fewer than three groups have nothing to group. ValueError when the reply cannot
    be used (`domains_of`); gateway errors propagate."""
    tops, groups = tops_now(state)
    ids = tuple(sorted(g.id for g in tops))
    if len(tops) < 3:
        return ids, ()
    names = {r.id: r.name for r in state.graph.nodes}
    reply, _ = gateway.call("generator", domain_request(tops, groups, names, language=state.language, user_id=user_id), DomainsReply)
    return ids, domains_of(reply.domains, tops)


def domains_out(state: WikiState, groups: Sequence[GroupOut]) -> list[DomainOut]:
    """The stored domains, those of their groups that still stand; one left with a single group is that group."""
    tops = {g.id for g in top_groups(groups)}
    out = []
    for d in state.domains:
        kids = [k for k in d.kids if k in tops]
        if len(kids) > 1:
            out.append(DomainOut(id=d.id, name=d.name, summary=d.summary, kids=kids))
    return out
