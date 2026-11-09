"""Adaptive diagnosis (big-goal design §Flow 2) by Knowledge Space Theory (goals/kst.py, a port of
the R package mycaas; design note the product specification).

The algorithm does all the deciding, the cell included; the assistant writes the question it picked
in the chat (stage 3 T11: `next_check` returns the pick, `ask_check` asks it; no model call here).

Items. Every node is an item of the knowledge structure: a region (a curator topic) is a
*mastery* item (a question only someone who really knows the field answers: 线性代数 → 奇异值分解
level), a concept / member is a *goal-use* item (a question on exactly the part the goal uses:
机器学习 → 矩阵乘法). The surmise relation (what mastering an item implies) is the prerequisite
map: a concept's prerequisites (`ProbeNode.prereqs`), a region's prerequisite regions (knowing
the field B fully implies knowing its prerequisite field A fully; members are not tied to other
regions), and (probe@v6) region → the members an *answered* mastery question presupposes
(`ProbeQuestion.presupposes`, written by the model with the item: the members one must know to
answer it). A region item does not sit above all its members: a right mastery answer is evidence
on the region item and, through the structure, on the members that question really presupposes,
not on the others (the KST reading: an item implies what solving it requires). `structure` builds
the relation from the nodes and the answered rounds, so it grows as mastery questions are
answered. States are the prerequisite-closed sets (kst.downsets). A region is decided *known* when
every leaf item under it is known (its member items, or theirs when a member has parts of its own:
its members are what the goal uses) and, once it was quizzed itself, its own item too (direct and
aggregate evidence both count, neither overrides the other); members a mastery question did not
presuppose stay uncertain and may be asked. A region without member items here (an older map's
region) is decided by its own item.

Node model (unified node graph, plan T8): the items are the goal subset's nodes, each once; a node
with parts among the items gets a mastery item, a leaf a goal-use item on its own cells (a
cell-less region keeps a mastery item) — `_State.mastery`. The prerequisites come from the graph
(goal.py `_probe_nodes`); part_of never enters the surmise relation.

Prior (probe@v5). The likelihood starts from kst's informative product prior, not the uniform
one: each item's initial mastery estimate `ProbeNode.p0` (goal.py: the intake gap identifier's
level + confidence through kst.level_p0); a concept without one falls back to what the description
said (`described`: polarity → kst.DESCRIBED_P0), then 1/2; a region without one takes the mean of
its member items' estimates (members without one at 1/2) when any member has one, else 1/2. The
description enters through this prior only (probe@v4's weak `updating` on the described polarity
is gone: it would count the description twice).

Evidence, all through mycaas's multiplicative `updating`, in this order: (a) fixed evidence — what
K had before this goal (`ProbeNode.k_prior`: known / to learn from its bands) and, in an entered
region, what the map's opening diagnosis already decided (`ProbeNode.decided`, goal.py) — at
kst.BETA_K / ETA_K; such a node is never asked, and a `decided` node keeps that decision as it is;
(b) every answered question of this draft: a self-report (know → right; partly / dont → wrong,
forgotten = to learn; unsure / 不知道 → no evidence) at kst.BETA_SELF / ETA_SELF (dont: at
kst.BETA_DONT / ETA_DONT, §Failed below) — a region's
self-report is about the whole field, so (probe@v6, the region no longer implying its members) it
counts on the region item and on each member item (the cells probe_observations writes it to; a
region without member items: on itself) — and a quiz (the key → right; another option or 不知道 →
wrong) at kst.BETA_QUIZ / ETA_QUIZ.

Failed below (user ruling 2026-10-24: 「我上面都说了我不会了，还在问什么」). An item whose
prerequisites or whose unverified dependencies (`ProbeNode.builds_on`: the model's `recommended`
edges and unverified prerequisites, goal.py) reach an item answered wrong / 不知道 / 没学过 is not
asked and is decided *teach* (source inferred), unless it was quizzed itself or has fixed evidence. This is a
failure-only, downward use of edges that never enter the surmise relation (ruling 2026-10-14: an
unverified prerequisite must not infer mastery): at worst a wrong edge costs one unasked question
and one more page, it never makes anything known. A self-report 「不会 / 没学过」 (means `dont`) is
as strong as a wrong quiz answer (kst.BETA_DONT): it resolves the block and its members *teach*
at once, so no focus question follows on it; 「熟悉」 / 「学过但忘了」 / 「说不清」 still get theirs.

Known only from answers (user ruling: key points are verified by actual answers, never by
self-assessment). Two likelihoods are kept: `a` (full evidence: prior + description + self-reports
+ K + the opening + quizzes; half-split and the reported probabilities) and `strong` (a uniform
start + K, the opening's decisions and quizzes only).

Resolved (probe@v6: mycaas items_discrimination in an asymmetric form, kst.discriminated). An item
is resolved *teach* when its full-evidence marginal is ≤ kst.SC_TEACH, resolved *known* when its
full-evidence and its strong-evidence marginals are both ≥ kst.SC_KNOWN and it is in the strong
state (`strong.state`, among tied states the smallest: what the answers imply through the
structure; with no strong evidence it is empty). The last condition is needed because the uniform
start of `strong` is not neutral per item on a prerequisite map (a bottom item is in most states:
c1 矩阵乘法 was at 98.5% in live run 4 before any answer), so a strong marginal alone could make
an unanswered item known. So the prior, the description or a self-report alone can resolve an
item *teach* (a wrong "teach" costs a page the learner reads quickly) but never *known*.

Rounds. A round is one pick (`CheckPick`): a self-report while a region the learner's description
says nothing about is left (no prior estimate on it or its members, no K evidence, nothing
described; one region per round, before any quiz) — cheap for the learner, and it steers half-split
where the prior cannot. Every later round is one quiz. The candidates are the *unresolved* items
that decide something (a concept / member; a region without member items) among the nodes not
quizzed in this draft and without fixed evidence, plus the mastery item of a region that still has
an unresolved member (allowed, never forced). Claims come first (`_State.unconfirmed`: a candidate
the full evidence puts in its most likely state but the strong evidence does not — regions first,
since one mastery answer confirms the region and the members it presupposes), else mycaas's
half-split over the candidates (marginal under `a` closest to 1/2; for a region's mastery item
the probability that every member item is known — `_State.p_known`: the region item itself is
tied to no member before an answer, so its own marginal says nothing about the field); a near-certain item is never
picked while an unresolved one is left. The round's `why` names the reason and the marginal. The
pick names the cell its answer is recorded on (`CheckPick.cell`, the quiz picker's informativeness
rule over the node's own cells for a mastery item, its cells for a goal-use item: high u, m near
1/2) — never "the first cell". The
diagnosis ends when no candidate is left: every item that decides something is resolved
(`RESOLVED_WHY`), or the unresolved ones were all asked already (each item at most once: mycaas
repetition = FALSE; `ALL_ASKED_WHY`). There is no count anywhere. probe@v5 stopped when the
strong state's probability passed kst.STOP_P (mycaas likelihood_maximization); on a whole map that
probability is a product over all items and stays below 0.5 when each of 26 items is ≈95% decided
(live run 4), so the loop asked near-certain items until the pool was empty. The final round
(done, no questions) carries the decision per node (`decisions`: known / teach — a resolved item
as resolved; one left unresolved is known only when it is in the most likely state of both
likelihoods, as in probe@v5 — the probability, the evidence that decided it: quiz / self_report /
k / opening — direct; inferred — through the structure from answered or K evidence on other items;
described — only the prior; none — nothing at all). The decisions steer this draft only (and go
to the map log): whether a block is known afterwards is read from K (regions.block_known), where
every answer already went (`probe_observations`). Every round carries the decisions as they stood
when it was asked.

The question (`answered_question`): the assistant's typed item on the pick, checked by the tool
(`assistant.tools.items`: gradable, plain text); a mastery quiz also lists the region's members it
presupposes (only that region's member ids are kept). A map too large to enumerate
(kst.StructureTooLarge; probe@v7) is diagnosed over its top level instead: the regions and the
nodes no listed region holds (`top_level`; a region then counts as one item, its members are left
to the region's own diagnosis). Only when that is still too large does it end at once with no
decisions (`TOO_LARGE_WHY`).

Old drafts (probe@v2/v3 rounds: quizzes on cells, several per round, `known` from the model) still
load and continue: their answers are mapped to nodes (a quiz to the concept node of its cell, else
the node covering the cell) and enter as evidence like new ones.

K observations: a self-report is typed `self` evidence (`goals.selfreport`: know → can_do, partly →
partly, forgot → studied, dont → never; unsure / 不知道 → none) on the cells the pick covers; a quiz answer goes
through `diagnostic.answer_observation` on the pick's cell; a mastery quiz on a region with no cells
of its own writes no observation (the region's decision is in the map log and the round). This
module never calls a model (tests/goals/test_isolation)."""

from collections.abc import Collection, Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evowiki.goals import kst
from evowiki.goals.contracts import CheckItem, KView, Observation, split_cell
from evowiki.goals.diagnostic import IDK, answer_observation
from evowiki.goals.observe import Dropped
from evowiki.goals.selfreport import MEANS_CLAIM, self_observations

Means = Literal["know", "partly", "forgot", "dont", "unsure"]
MEANS: tuple[Means, ...] = ("know", "partly", "forgot", "dont", "unsure")
Intent = Literal["mastery", "goal_use", "self_report"]
Source = Literal["quiz", "self_report", "described", "k", "inferred", "opening", "none"]
ALL_EVIDENCED_WHY = "这些都已经有证据了，不用再问"
ALL_ASKED_WHY = "能问的都已经问过了，还没定下来的按目前最可能的掌握情况下结论"
RESOLVED_WHY = ("已经足够确定：每个知识点会的概率都已不到 {teach:.0%}（要学），"
                "或超过 {known:.0%} 且有答题证据（会）")
TOO_LARGE_WHY = "知识地图太大，没法逐一列出所有掌握状态，这次不做摸底"
SELF_TITLE, MASTERY_TITLE, GOAL_USE_TITLE = "了解你的基础", "确认掌握程度", "聚焦关键知识"
SOURCE_LABEL: dict[str, str] = {"quiz": "小测", "self_report": "自评", "described": "描述",
                                "k": "已有证据", "inferred": "由前置关系推断",
                                "opening": "大目标开头的摸底", "none": "没有证据"}
# a decision's source, best first (a region: the best among it and its members)
SOURCE_ORDER: tuple[Source, ...] = ("quiz", "self_report", "k", "opening", "inferred",
                                    "described", "none")
ANSWERED_SOURCES = frozenset({"quiz", "self_report", "k", "opening"})
STRONG_SOURCES = frozenset({"quiz", "k", "opening"})
INTENT_LABEL: dict[str, str] = {"mastery": "区域掌握题", "goal_use": "目标用到的知识点",
                                "self_report": "自评"}


class ProbeNode(BaseModel):
    """One concept or region being diagnosed (an item). `cells`: every K cell it covers (a
    concept: its own; a region: its members' plus any of its own); `members`: a region's (id,
    name) pairs; `evidenced`: concept ids among the node itself and its members K had evidence
    for before this goal; `k_prior`: what that evidence says about the node itself (None: no
    evidence on it — it may be asked); `decided`: an entered region's member the map's opening
    diagnosis already decided (fixed: never asked, the decision kept); `p0`: the initial mastery
    estimate (kst.level_p0; None: no estimate — module doc)."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    cells: tuple[str, ...]
    scope: str = ""
    region: bool = False
    target: bool = False
    prereqs: tuple[str, ...] = ()
    members: tuple[tuple[str, str], ...] = ()
    evidenced: tuple[str, ...] = ()
    k_prior: Literal["known", "teach"] | None = None
    decided: Literal["known", "teach"] | None = None
    p0: float | None = Field(default=None, gt=0, lt=1)
    # what the node builds on without being verified (the model's `recommended` edges, unverified
    # prerequisites): used only downward on a failure (module doc §Failed below), never in the
    # surmise relation
    builds_on: tuple[str, ...] = ()

    @property
    def fixed(self) -> Literal["known", "teach"] | None:
        """Fixed evidence: the opening's decision, else K's."""
        return self.decided or self.k_prior

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidenced)

    @property
    def concepts(self) -> tuple[str, ...]:
        """The concept ids whose cells the node covers, in order."""
        return tuple(dict.fromkeys(split_cell(c)[0] for c in self.cells))

    def open_concepts(self) -> tuple[str, ...]:
        """Its concepts K had no evidence for."""
        return tuple(c for c in self.concepts if c not in self.evidenced)

    @property
    def own_cells(self) -> tuple[str, ...]:
        """The cells of the node's own id (a region usually has none: a topic is not a K
        concept)."""
        return tuple(c for c in self.cells if split_cell(c)[0] == self.id)


class ProbeQuestion(BaseModel):
    """`target`: a node id (self_report; a mastery quiz on a region without cells of its own) or
    a cell id (quiz). `answer` (quiz key) and `means` (self_report, aligned with options) stay on
    the server. `intent` / `node`: the item kind and the node the algorithm picked ("" in rounds
    from before probe@v4). `presupposes` (a mastery quiz, probe@v6): the region's member ids the
    question presupposes — once answered, the region item sits above exactly these (`structure`);
    () in older rounds and for every other kind."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["self_report", "quiz"]
    target: str
    stem: str = Field(min_length=1)
    options: tuple[str, ...] = Field(min_length=2)
    answer: int = -1
    means: tuple[Means, ...] = ()
    verified: bool = False
    intent: Intent | Literal[""] = ""
    node: str = ""
    presupposes: tuple[str, ...] = ()

    @property
    def cell(self) -> str:
        """The K cell a quiz measures ("" for a self-report and a cell-less mastery quiz)."""
        if self.kind != "quiz" or (self.intent == "mastery" and self.target == self.node):
            return ""
        return self.target

    def item(self) -> CheckItem:
        """The quiz as a CheckItem (a quiz with a cell only)."""
        return CheckItem(cell=self.cell, stem=self.stem, options=self.options,
                         answer=self.answer, verified=self.verified)


class NodeLevel(BaseModel):
    """The intake gap identifier's rating of one node from the learner's description (stored on
    the draft; goal.py turns it into `ProbeNode.p0`): level unlearned / beginner / intermediate /
    advanced (unknown is not stored), confidence low / medium / high."""

    model_config = ConfigDict(frozen=True)

    node: str
    level: str
    confidence: str = ""

    @property
    def p0(self) -> float | None:
        return kst.level_p0(self.level, self.confidence)


class NodeDecision(BaseModel):
    """The diagnosis's verdict on one node: in the most likely knowledge state (known: skip) or
    not (teach); `p` the probability that it is known; `source` the evidence that decided it."""

    model_config = ConfigDict(frozen=True)

    node: str
    name: str = ""
    decision: Literal["known", "teach"]
    p: float
    source: Source


class CheckPick(BaseModel):
    """What the next check asks (stage 3 T11): the item kind, the node, and the cell its answer is
    recorded on (`cell`: a quiz's; "" for a self-report and a mastery item on a region with no cell
    of its own); `covers`: the cells a self-report counts on (the node's, a region: its members').
    `pick_id` = `{draft_id}:{round}`, set by the caller. The rest is what the question is written
    from: the node's name, scope and (a region) members."""

    model_config = ConfigDict(frozen=True)

    pick_id: str = ""
    intent: Intent
    node: str
    name: str
    scope: str = ""
    members: tuple[tuple[str, str], ...] = ()
    cell: str = ""
    covers: tuple[str, ...] = ()


class ProbeRound(BaseModel):
    """One round. An open round carries its `pick` and no question until the assistant asked it
    (then `questions` = the one it wrote, `answers` = his answer, -1 = 不知道). `decisions`: the
    per-node verdicts when the round was made (the final ones on the done round)."""

    model_config = ConfigDict(frozen=True)

    questions: tuple[ProbeQuestion, ...] = ()
    pick: CheckPick | None = None
    done: bool
    why: str = ""
    title: str = ""
    answers: tuple[int, ...] | None = None
    dropped: tuple[Dropped, ...] = ()
    decisions: tuple[NodeDecision, ...] = ()


def answer_text(q: ProbeQuestion, a: int) -> str:
    """The chosen option's text, or 不知道."""
    return "不知道" if a == IDK else q.options[a]


# --- the assessment --------------------------------------------------------------------------

def _described(described: Mapping[str, str] | Sequence[str]) -> dict[str, str]:
    """concept id → polarity ("" when only the id is known: a draft from before probe@v4)."""
    if isinstance(described, Mapping):
        return dict(described)
    return dict.fromkeys(described, "")


def structure(nodes: Sequence[ProbeNode], rounds: Sequence["ProbeRound"] = ()
              ) -> tuple[tuple[str, ...], dict[str, set[str]]]:
    """The items and the surmise relation (item → its prerequisite items): concept and region
    prerequisites, and for every answered mastery question its region above the member items the
    question presupposes (module doc; a region does not sit above its other members)."""
    items = tuple(dict.fromkeys(n.id for n in nodes))
    ids = set(items)
    by_id = {n.id: n for n in nodes}
    pre: dict[str, set[str]] = {i: set() for i in items}
    for n in nodes:
        pre[n.id] |= {p for p in n.prereqs if p in ids and p != n.id}
    for r in rounds:
        if r.answers is None:
            continue
        for q in r.questions:
            n = by_id.get(q.node)
            if q.intent != "mastery" or n is None or not n.region:
                continue
            members = {m for m, _ in n.members}
            pre[n.id] |= {m for m in q.presupposes if m in members and m in ids and m != n.id}
    return items, pre


def top_level(nodes: Sequence[ProbeNode]) -> list[ProbeNode]:
    """The nodes no listed region holds as a member: the regions and the free concepts (what a
    map too large to enumerate is diagnosed over)."""
    inside = {m for n in nodes if n.region for m, _ in n.members if m != n.id}
    return [n for n in nodes if n.id not in inside]


def node_of(q: ProbeQuestion, by_id: Mapping[str, ProbeNode]) -> ProbeNode | None:
    """The node a question is about: its `node`; for older rounds a self-report's target, a
    quiz's concept node, else the node covering its cell."""
    if q.node in by_id:
        return by_id[q.node]
    if q.kind == "self_report" or q.target in by_id:
        return by_id.get(q.target)
    cid = split_cell(q.target)[0] if "." in q.target else q.target
    if cid in by_id:
        return by_id[cid]
    return next((n for n in by_id.values() if q.target in n.cells), None)


def _parts(n: ProbeNode, by_id: Mapping[str, ProbeNode]) -> list[str]:
    """The node's member items (its parts that are items here; never itself)."""
    return [m for m, _ in n.members if m in by_id and m != n.id]


def _below(n: ProbeNode, by_id: Mapping[str, ProbeNode]) -> tuple[list[str], list[str]]:
    """Every item under the node through its parts, transitively (breadth first, each once, never
    the node itself: a part_of cycle cannot loop), and the leaves among them — the items with no
    parts of their own here (what the node's parts finally consist of)."""
    below: list[str] = []
    leaves: list[str] = []
    seen = {n.id}
    todo = _parts(n, by_id)
    while todo:
        x = todo.pop(0)
        if x in seen:
            continue
        seen.add(x)
        below.append(x)
        sub = _parts(by_id[x], by_id)
        if sub:
            todo += sub
        else:
            leaves.append(x)
    return below, leaves


def prior_of(nodes: Sequence[ProbeNode], described: Mapping[str, str]) -> dict[str, float]:
    """item → its initial mastery estimate, for the items that have one (module doc: `p0`, else a
    concept's described polarity; a region without one: the mean over its leaf items — its member
    items, or theirs when a member has parts of its own — when any has one)."""
    by_id = {n.id: n for n in nodes}
    out: dict[str, float] = {}
    for n in nodes:
        p = n.p0 if n.p0 is not None else kst.DESCRIBED_P0.get(described.get(n.id, ""))
        if p is not None and (not n.region or n.p0 is not None):
            out[n.id] = p
    for n in nodes:
        if not n.region or n.id in out:
            continue
        members = _below(n, by_id)[1]
        if any(m in out for m in members):
            out[n.id] = sum(out.get(m, 0.5) for m in members) / len(members)
        elif not members and (p := kst.DESCRIBED_P0.get(described.get(n.id, ""))) is not None:
            out[n.id] = p
    return out


def _closure(items: Sequence[str], pre: Mapping[str, set[str]]) -> dict[str, set[str]]:
    """item → every item it implies (its prerequisites, transitively; a region: its members)."""
    out: dict[str, set[str]] = {}
    for q in items:
        seen: set[str] = set()
        todo = list(pre.get(q, ()))
        while todo:
            x = todo.pop()
            if x not in seen and x != q:
                seen.add(x)
                todo += pre.get(x, ())
        out[q] = seen
    return out


class _State:
    """The assessment after the prior and all evidence (`a`: picks and the probabilities), the one
    after the strong evidence only (`strong`: a uniform start + K, the opening's decisions and
    quizzes — what may be resolved known; module doc), what each node's evidence was,
    and what was asked."""

    def __init__(self, nodes: Sequence[ProbeNode], rounds: Sequence[ProbeRound],
                 described: Mapping[str, str]) -> None:
        self.nodes = list(nodes)
        self.by_id = {n.id: n for n in nodes}
        items, pre = structure(nodes, rounds)
        strong = kst.Assessment.build(items, pre)     # StructureTooLarge goes to the caller
        self.below = _closure(items, pre)
        self.prior = prior_of(nodes, described)
        a = strong.with_prior(self.prior)
        self.source: dict[str, Source] = {}          # direct evidence per item, the best one
        self.answered: list[tuple[str, bool]] = []   # (item, right?) of every answered / K evidence

        def note(node: str, src: Source, right: bool) -> None:
            old = self.source.get(node)
            if old is None or SOURCE_ORDER.index(src) < SOURCE_ORDER.index(old):
                self.source[node] = src
            self.answered.append((node, right))

        for n in nodes:
            if n.fixed is not None:
                r = int(n.fixed == "known")
                a = a.update(n.id, r, kst.BETA_K, kst.ETA_K)
                strong = strong.update(n.id, r, kst.BETA_K, kst.ETA_K)
                note(n.id, "opening" if n.decided is not None else "k", bool(r))
        self.quizzed: set[str] = set()
        self.failed: set[str] = set()                 # items answered wrong / 不知道 / 没学过
        self.quiz_asked = any(q.kind == "quiz" for r in rounds for q in r.questions)
        self.self_asked = {q.node or q.target for r in rounds for q in r.questions
                           if q.kind == "self_report"}
        for r in rounds:
            if r.answers is None:
                continue
            for q, ans in zip(r.questions, r.answers, strict=True):
                n = node_of(q, self.by_id)
                if n is None:
                    continue
                if q.kind == "quiz":
                    self.quizzed.add(n.id)
                    r_q = int(ans != IDK and ans == q.answer)
                    a = a.update(n.id, r_q, kst.BETA_QUIZ, kst.ETA_QUIZ)
                    strong = strong.update(n.id, r_q, kst.BETA_QUIZ, kst.ETA_QUIZ)
                    note(n.id, "quiz", bool(r_q))
                    if not r_q:
                        self.failed.add(n.id)
                elif ans != IDK and q.means[ans] != "unsure":
                    know, dont = q.means[ans] == "know", q.means[ans] == "dont"
                    beta, eta = (kst.BETA_DONT, kst.ETA_DONT) if dont else (kst.BETA_SELF, kst.ETA_SELF)
                    on = [n.id, *(_below(n, self.by_id)[0] if n.region else [])]
                    for x in on:
                        a = a.update(x, int(know), beta, eta)
                        note(x, "self_report", know)
                    if dont:
                        self.failed |= set(on)
        self.a, self.strong = a, strong
        self.pruned = self._failed_below(pre)
        self.described = described
        self.state = a.state()[0]
        self.verified = strong.state(rel_tol=kst.TIE_REL_TOL)[0]

    def _failed_below(self, pre: Mapping[str, set[str]]) -> set[str]:
        """Items that build on a failed one (module doc §Failed below): their prerequisites and
        what they build on (`builds_on`, unverified), transitively, reach an item answered wrong /
        不知道 / 没学过, and they were not quizzed themselves (nor have fixed evidence). Downward only: nothing here is ever
        evidence for *known*."""
        ids = set(self.by_id)
        on = {n.id: pre.get(n.id, set()) | {b for b in n.builds_on if b in ids and b != n.id}
              for n in self.nodes}
        closure = _closure(list(on), on)
        return {x for x, below in closure.items()
                if x not in self.quizzed and self.by_id[x].fixed is None and below & self.failed}

    def member_items(self, n: ProbeNode) -> list[str]:
        """The node's parts that are items here (a node with children inside the goal's
        subset)."""
        return _parts(n, self.by_id)

    def leaves(self, n: ProbeNode) -> list[str]:
        """The leaf items under the node (its member items, or theirs when a member has parts of
        its own): what its aggregate is decided from."""
        return _below(n, self.by_id)[1]

    def mastery(self, n: ProbeNode) -> bool:
        """The node's item is a mastery item: it has parts among the items (a node with children
        inside the goal's subset), or it is a region with no cell to record a goal-use answer on
        (an older map's topic). Every other node — a leaf — gets a goal-use item."""
        return bool(self.member_items(n)) or (n.region and not n.cells)

    def source_of(self, x: str) -> Source:
        """The evidence on item x: its own, else inferred through the structure from answered or K
        evidence on another item (right on one that implies x, wrong on one x implies), else the
        prior (described), else none."""
        if x in self.source:
            return self.source[x]
        if x in self.pruned:
            return "inferred"
        if any((right and x in self.below.get(y, ())) or (not right and y in self.below[x])
               for y, right in self.answered):
            return "inferred"
        return "described" if x in self.prior else "none"

    def status(self, x: str) -> kst.Resolved | None:
        """Item x resolved (module doc: kst.discriminated on the full and the strong marginal, and
        known only when the strong state holds it), or None."""
        if x in self.pruned:
            return "teach"
        res = kst.discriminated(self.a.marginal(x), self.strong.marginal(x))
        return None if res == "known" and x not in self.verified else res

    def decides(self, n: ProbeNode) -> bool:
        """Whether the node's own item decides something: a concept / member, or a region
        without member items (a region with members is decided from them)."""
        return not n.region or not self.member_items(n)

    def unresolved(self, *, askable: bool = True) -> list[str]:
        """Items that decide something and are not resolved, in node order: by default only the
        ones a quiz may still be about (the pool); askable=False — every such item without fixed
        evidence (also the ones already asked)."""
        pool = set(self.pool())
        return [n.id for n in self.nodes
                if n.fixed is None and self.decides(n) and (not askable or n.id in pool)
                and self.status(n.id) is None]

    def candidates(self) -> list[str]:
        """What the next quiz may be about, in node order: the unresolved pool items, plus the
        mastery item of a region (in the pool) with an unresolved member item."""
        open_ = set(self.unresolved())
        return [x for x in self.pool()
                if x in open_ or (self.by_id[x].region
                                  and any(m in open_ for m in self.leaves(self.by_id[x])))]

    def decision(self, x: str) -> bool:
        """Item x known: resolved known; unresolved — in the most likely state of both
        likelihoods (probe@v5's rule; the strong one: what answers imply)."""
        res = self.status(x)
        if res is not None:
            return res == "known"
        return x in self.state and x in self.verified

    def decisions(self) -> tuple[NodeDecision, ...]:
        """Per node: a leaf by its own item; a node with parts by its leaves (the aggregate: a
        right answer on the big node is not the whole field) and, once it was quizzed itself, by
        its own item too (its direct evidence: neither overrides the other, spec 2026-10-13 §2)."""
        out = []
        for n in self.nodes:
            members = self.leaves(n) if n.region else []
            ids = members or [n.id]
            if members and self.source.get(n.id) in STRONG_SOURCES:
                ids = [*members, n.id]
            p = self.a.prob_all(members) if members else self.a.marginal(n.id)
            if n.decided is not None:                 # the opening's decision, as it is
                out.append(NodeDecision(node=n.id, name=n.name, decision=n.decided,
                                        p=round(p, 4), source="opening"))
                continue
            known = all(self.decision(x) for x in ids)
            srcs = [self.source_of(x) for x in [n.id, *members]]
            if known:                                 # only strong evidence makes a node known
                srcs = [s if s in STRONG_SOURCES else "inferred" for s in srcs]
            src = min(srcs, key=SOURCE_ORDER.index)
            out.append(NodeDecision(node=n.id, name=n.name, decision="known" if known else "teach",
                                    p=round(p, 4), source=src))
        return tuple(out)

    def self_reportable(self) -> list[ProbeNode]:
        """The coarse self-reports, one region a round before any quiz: regions the description
        says nothing about (no prior estimate on them or their members, nothing described),
        without fixed evidence and not asked yet."""
        if self.quiz_asked:
            return []
        return [n for n in self.nodes
                if n.region and n.fixed is None and not n.has_evidence
                and n.id not in self.self_asked
                and n.id not in self.prior and not set(_below(n, self.by_id)[0]) & set(self.prior)
                and not set(n.concepts) & set(self.described) and n.id not in self.described]

    def unconfirmed(self, candidates: Sequence[str]) -> list[str]:
        """Claims to confirm (user ruling 2026-10-11: 对关键知识点用实际答题验证): candidates the
        full evidence (prior, description, self-reports) puts in the most likely state but the
        strong evidence does not. Regions first — one mastery answer confirms the region and the
        members it presupposes — then concepts, each group in half-split order (most uncertain
        first). Empty: plain half-split over the candidates."""
        state, verified = self.state, self.verified
        regions, concepts = [], []
        for x in candidates:
            n = self.by_id[x]
            ids = (self.leaves(n) or [n.id]) if n.region else [n.id]
            if all(i in state for i in ids) and not all(i in verified for i in ids):
                (regions if n.region else concepts).append(x)

        def ordered(xs: list[str]) -> list[str]:
            return sorted(xs, key=lambda x: (round(abs(2 * self.p_known(x) - 1), 4),
                                             xs.index(x)))
        return [*ordered(regions), *ordered(concepts)]

    def p_known(self, x: str) -> float:
        """What half-split weighs for item x under the full evidence: its marginal; a region with
        member items — P(every member item known), the field a mastery question tests (its own
        item decides nothing and, tied to no member before an answer, sits near 1/2 whatever the
        learner knows)."""
        n = self.by_id[x]
        members = self.leaves(n) if n.region else []
        return self.a.prob_all(members) if members else self.a.marginal(x)

    def half_split(self, candidates: Sequence[str]) -> str:
        """mycaas half_split (kst.half_split's rule: min |2·p − 1| at 4 decimals) over the
        candidates, weighing each by `p_known`; ties → a goal-use item before a mastery item (its
        answer decides an item directly; the mastery item is allowed, never forced), then the
        first in order."""
        score = {x: round(abs(2 * self.p_known(x) - 1), 4) for x in candidates}
        best = min(score.values())
        tied = [x for x in candidates if score[x] == best]
        return next((x for x in tied if not self.mastery(self.by_id[x])), tied[0])

    def pool(self) -> list[str]:
        """Nodes a quiz may still be about, in node order: not quizzed in this draft, no fixed
        evidence, and a goal-use item needs a cell to record the answer on."""
        return [n.id for n in self.nodes
                if n.fixed is None and n.id not in self.quizzed and (n.region or n.cells)]


# --- the pick and the question ------------------------------------------------------------

def _informative(cells: Sequence[str], k: KView) -> str:
    """The cell whose answer tells the most (the quiz picker's rule: high u, m near 1/2; ties: the
    first in order); "" when there is none."""
    best, score = "", -1.0
    for c in cells:
        try:
            b = k.cell(c)
            x = b.u + (0.5 - abs(b.m - 0.5))
        except LookupError:                    # a cell K has no belief on: nothing known yet
            x = 1.5
        if x > score + 1e-12:
            best, score = c, x
    return best


def _pick(st: "_State", n: ProbeNode, intent: Intent, k: KView) -> CheckPick:
    cell = "" if intent == "self_report" else _informative(
        n.own_cells if intent == "mastery" else n.cells, k)
    return CheckPick(intent=intent, node=n.id, name=n.name, scope=n.scope, members=n.members,
                     cell=cell, covers=n.cells if intent == "self_report" else ())


def answered_question(pick: CheckPick, item: CheckItem | None = None, *,
                      means: Sequence[Means] = (), stem: str = "",
                      options: Sequence[str] = (),
                      presupposes: Sequence[str] = ()) -> ProbeQuestion:
    """The round's question as the assistant wrote it on `pick`: a quiz (`item`, checked on the
    pick's cell by the caller; a cell-less mastery item carries the region id as its target) or a
    self-report (`stem`, `options`, `means` aligned). ValueError when it does not fit the pick."""
    if pick.intent == "self_report":
        if item is not None or len(means) != len(options) or len(options) < 2 \
                or any(m not in MEANS for m in means):
            raise ValueError("自评题要有 2 个以上选项，每个选项一个 means（know / partly / forgot / dont / unsure）")
        return ProbeQuestion(kind="self_report", target=pick.node, stem=stem, options=tuple(options),
                             means=tuple(means), intent="self_report", node=pick.node)
    if item is None or means:
        raise ValueError("这一题是小测：要有题干、选项和答案")
    if pick.cell and item.cell != pick.cell:
        raise ValueError(f"这道题要测的是「{pick.cell}」")
    members = {m for m, _ in pick.members}
    pre = tuple(dict.fromkeys(x for x in presupposes if x in members and x != pick.node)) \
        if pick.intent == "mastery" else ()
    return ProbeQuestion(kind="quiz", target=pick.cell or pick.node, stem=item.stem,
                         options=item.options, answer=item.answer, verified=True,
                         intent=pick.intent, node=pick.node, presupposes=pre)


def _final(st: _State, why: str, dropped: Sequence[Dropped] = ()) -> ProbeRound:
    return ProbeRound(questions=(), done=True, why=why, dropped=tuple(dropped),
                      decisions=st.decisions())


def next_round(*, nodes: Sequence[ProbeNode], k: KView, rounds: Sequence[ProbeRound] = (),
               described: Mapping[str, str] | Sequence[str] = (),
               pick_id: str = "", rough: Collection[str] = ()) -> ProbeRound:
    """The next round (see module doc): open with its pick (`pick_id` set on it), or done with
    the decisions. `rounds`: the answered rounds so far; `described`: concept id → polarity it gave
    evidence on (a plain sequence of ids: no polarity known, the ids only get no self-report). `k`
    picks the cell; what K says about a node enters through its `k_prior` / `evidenced`. `rough`:
    the blocks he marked 掌握 / 熟悉 on the 摸底 grid (ask_rough): he already said it, so they get
    no second self-report; their claim is checked with a quiz (the 细问). No model call."""
    desc = _described(described)
    try:
        st = _State(nodes, rounds, desc)
    except kst.StructureTooLarge:
        top = top_level(nodes)
        if len(top) == len(nodes):
            return ProbeRound(questions=(), done=True, why=TOO_LARGE_WHY)
        try:
            st = _State(top, rounds, desc)
        except kst.StructureTooLarge:
            return ProbeRound(questions=(), done=True, why=TOO_LARGE_WHY)
    nodes = st.nodes
    if all(n.fixed is not None for n in nodes):
        return _final(st, ALL_EVIDENCED_WHY)
    before = st.decisions()
    marked = set(rough)
    selfs = [n for n in st.self_reportable() if n.id not in marked]
    if selfs:
        intent: Intent = "self_report"
        n, title = selfs[0], SELF_TITLE
        why = "先粗问一下这一块学没学过（自评是弱证据，之后用小测确认）"
    else:
        candidates = st.candidates()
        if not candidates:
            return _final(st, ALL_ASKED_WHY if st.unresolved(askable=False) else
                          RESOLVED_WHY.format(teach=kst.SC_TEACH, known=kst.SC_KNOWN))
        claims = st.unconfirmed(candidates)
        pick = claims[0] if claims else st.half_split(candidates)
        n = st.by_id[pick]
        mastery = st.mastery(n)
        intent = "mastery" if mastery else "goal_use"
        title = MASTERY_TITLE if mastery else GOAL_USE_TITLE
        p = st.p_known(pick)
        if claims:
            why = f"他说会「{n.name}」（会的概率 {p:.0%}），但还没有答题证据：先用一道题确认"
        elif mastery:
            why = (f"「{n.name}」里还有没定下来的知识点，整个区域都会的概率 {p:.0%}，"
                   "最拿不准：问一道区域掌握题")
        else:
            why = (f"「{n.name}」在还没定下来的知识点里最拿不准（会的概率 {p:.0%}），"
                   "问一道最能分清的题")
    pick = _pick(st, n, intent, k).model_copy(update={"pick_id": pick_id})
    return ProbeRound(pick=pick, done=False, why=why, title=title, decisions=before)


# --- evidence --------------------------------------------------------------------------------

def failed_nodes(rounds: Sequence[ProbeRound]) -> set[str]:
    """The nodes answered wrong / 不知道 (a quiz) or 不会/没学过 (a self-report, means `dont`) in
    these rounds: what §Failed below starts from (goal.py: an entered block failed in the opening
    passes *teach* to every point under it)."""
    out: set[str] = set()
    for r in rounds:
        for q, a in zip(r.questions, r.answers or (), strict=False):
            wrong = q.kind == "quiz" and (a == IDK or a != q.answer)
            dont = q.kind == "self_report" and 0 <= a < len(q.means) and q.means[a] == "dont"
            if wrong or dont:
                out.add(q.node or q.target)
    return out


def check_answers(r: ProbeRound, answers: Sequence[int]) -> str | None:
    """Why `answers` do not fit the round's questions, or None."""
    if len(answers) != len(r.questions):
        return f"答案数量应为 {len(r.questions)}，收到 {len(answers)}"
    for i, (q, a) in enumerate(zip(r.questions, answers, strict=True)):
        if a != IDK and not 0 <= a < len(q.options):
            return f"第 {i + 1} 题的答案必须是 0–{len(q.options) - 1} 或 -1"
    return None


def probe_observations(r: ProbeRound, answers: Sequence[int], *, draft_id: str, round_index: int,
                       cells: Mapping[str, Sequence[str]]) -> tuple[Observation, ...]:
    """Answers (aligned with `r.questions`, already checked) → observations. self_report: know /
    partly / forgot / dont → the self-report table's can_do / partly / studied / never (`goals.selfreport`) on the
    cells the target covers (`cells`: node id → its cells; a region: its members' cells), quote =
    the chosen option; unsure or 不知道 → nothing. quiz: `answer_observation(kind="quiz")` on its cell (a mastery quiz on a region
    without cells of its own: nothing). evidence_ref `probe:{draft}:{round}:{i}` for a quiz,
    `probeself:{draft}:{round}:{i}` for a self-report (the prefix tells a note's kind)."""
    out: list[Observation] = []
    for i, (q, a) in enumerate(zip(r.questions, answers, strict=True)):
        if q.kind == "quiz":
            if not q.cell:
                continue
            o = answer_observation(q.item(), a, kind="quiz",
                                   evidence_ref=f"probe:{draft_id}:{round_index}:{i}")
            if o is not None:
                out.append(o)
            continue
        if a == IDK or (claim := MEANS_CLAIM.get(q.means[a])) is None:
            continue
        out += self_observations(claim, cells.get(q.target, ()), quote=q.options[a],
                                 evidence_ref=f"probeself:{draft_id}:{round_index}:{i}")
    return tuple(out)


def question_line(round_index: int, q: ProbeQuestion, a: int, names: Mapping[str, str]) -> str:
    """One map-log line for an answered question: round, kind, target node, the result."""
    node = q.node or (q.target if q.kind == "self_report" else split_cell(q.target)[0]
                      if "." in q.target else q.target)
    intent = q.intent or ("self_report" if q.kind == "self_report" else "goal_use")
    if q.kind == "self_report":
        result = "说不知道" if a == IDK else f"选了「{q.options[a]}」（{q.means[a]}）"
    else:
        result = "不知道" if a == IDK else ("答对" if a == q.answer else "答错")
    return (f"摸底第 {round_index} 轮 {intent}（{INTENT_LABEL[intent]}）"
            f"{node} {names.get(node, node)}：{result}")


def decision_lines(r: ProbeRound) -> list[str]:
    """Map-log lines for the done round: why it ended, then per node the decision and source."""
    out = [f"摸底结束：{r.why}"] if r.why else []
    out += [f"摸底结论 {d.node} {d.name}：{'会' if d.decision == 'known' else '要学'}"
            f"（会的概率 {d.p:.0%}，依据：{SOURCE_LABEL[d.source]}）" for d in r.decisions]
    return out
