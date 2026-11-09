"""Scripting the goal flow for wikiapp / service tests: the diagnosis's questions (stage 3 T11: the
KST diagnosis picks the node and the cell, the ASSISTANT writes the question in the chat with
ask_check — here a scripted assistant) and the path model's answer.

- `question_of(r)`: the question the scripted assistant asks on round `r`'s pick: the next item
  pushed for PROBE on the latest Script (`push_round`; the one for the picked node, else the
  first) or the default (`write_item`'s: a self-report with SELF_OPTIONS; a quiz on the pick's
  cell, key "对" before the shuffle; a mastery quiz presupposes every member). Cached per pick, so
  a test reads the same question it then answers.
- `answer_round(repo, gw, uid, draft_id, answers, …)`: what the chat's ask_check does with his
  answer (`goal.answer_check` on the open pick, `answers[0]`).
- `push_no_question(s)`: the diagnosis asks nothing (tests/conftest.py's `_no_question` seam makes
  that draft's diagnosis end at once): the closest thing to "nothing to ask"."""

import re
from collections.abc import Callable, Sequence

from evowiki.goals.contracts import CheckItem
from evowiki.goals.diagnostic import IDK, _shuffled
from evowiki.goals.probe import CheckPick, ProbeQuestion, ProbeRound, answered_question
from evowiki.wikiapp.events import GoalMapSaved, WikiEvent
from evowiki.wikiapp.goal import NO_PROBE_LINE
from evowiki.wikiapp.repo import Commit

PROBE = "goals.probe"      # the scripted assistant's item queue on a Script (no model purpose)

Quiz = tuple[str, str, Sequence[str], int]              # cell, stem, options, key
SELF_OPTIONS = ["学过，还会", "学过，忘得差不多了", "没学过", "说不清"]
SELF_MEANS = ["know", "forgot", "dont", "unsure"]
QUIZ_OPTIONS = ["对", "错一", "错二", "错三"]


def shuffled(cell: str, stem: str, options: Sequence[str], answer: int) -> CheckItem:
    return _shuffled(CheckItem(cell=cell, stem=stem, options=tuple(options), answer=answer))


def task_members(prompt: str) -> list[str]:
    """The member ids the task's mastery line lists (「（成员：m1 矩阵、m2 矩阵乘法）」)."""
    for line in prompt.splitlines():
        if line.startswith("- mastery：") and "（成员：" in line:
            listed = line.split("（成员：")[1].split("）")[0]
            return [m.split(" ")[0] for m in listed.split("、") if m.strip()]
    return []


def task_lines(prompt: str) -> list[tuple[str, str, list[str]]]:
    """The probe prompt's task: (intent, node id, cells listed) per item to write."""
    out = []
    pattern = r"^- (self_report|mastery|quiz)：(?:区域|知识点) (\S+) (.*)$"
    for kind, node, rest in re.findall(pattern, prompt, flags=re.MULTILINE):
        cells = rest.split("格子：")[1].split("、") if "格子：" in rest else []
        out.append((kind, node, [c.strip() for c in cells]))
    return out


def write_item(prompt: str) -> dict:
    """A fake model writing exactly the picked item(s)."""
    qs = []
    for kind, node, cells in task_lines(prompt):
        if kind == "self_report":
            qs.append({"kind": "self_report", "target": node, "stem": f"学过{node}吗？",
                       "options": SELF_OPTIONS, "means": SELF_MEANS})
        else:
            q = {"kind": "quiz", "target": cells[0] if cells else node,
                 "stem": f"{node} 的{'掌握' if kind == 'mastery' else '用法'}题",
                 "options": QUIZ_OPTIONS, "answer": 0}
            if kind == "mastery":                 # a question that presupposes every member
                q["presupposes"] = task_members(prompt)
            qs.append(q)
    return {"questions": qs}


def auto_probe(s) -> None:
    """The default questions (`question_of`); kept so older tests read the same."""
    s.always(PROBE, write_item)


_ASKED: dict[str, ProbeQuestion] = {}


def pick_raw(pick: CheckPick, out: dict | None) -> dict | None:
    """The item of a pushed output (`{"questions": [...]}`, the old probe model's shape) for
    `pick`: the one about its node (or a cell it covers), else the first of its kind."""
    qs = (out or {}).get("questions") or []
    kind = "self_report" if pick.intent == "self_report" else "quiz"
    mine = [q for q in qs if (q.get("kind") == "self_report") == (kind == "self_report")
            and (q.get("target") in (pick.node, pick.cell, *pick.covers)
                 or str(q.get("target", "")).split(".")[0] == pick.node)]
    same = [q for q in qs if (q.get("kind") == "self_report") == (kind == "self_report")]
    return (mine or same or [None])[0]


def build_question(pick: CheckPick, raw: dict | None = None) -> ProbeQuestion:
    """The scripted assistant's question on `pick`: `raw` (an item as the old probe model wrote
    it) or the default (write_item's)."""
    if pick.intent == "self_report":
        raw = raw or {"stem": f"学过{pick.node}吗？", "options": SELF_OPTIONS, "means": SELF_MEANS}
        return answered_question(pick, stem=raw["stem"], options=[str(o) for o in raw["options"]],
                                 means=raw["means"])
    mastery = pick.intent == "mastery"
    raw = raw or {"stem": f"{pick.node} 的{'掌握' if mastery else '用法'}题",
                  "options": QUIZ_OPTIONS, "answer": 0,
                  "presupposes": [m for m, _ in pick.members] if mastery else []}
    pre = raw.get("presupposes")
    pre = [pre] if isinstance(pre, str) else pre if isinstance(pre, list) else []
    item = shuffled(pick.cell or f"{pick.node}.term", raw["stem"],
                    [str(o) for o in raw["options"]], raw["answer"])
    return answered_question(pick, item, presupposes=[str(x) for x in pre])


def asked(r: ProbeRound, out: dict | None = None) -> ProbeRound:
    """An open round with the scripted assistant's question on it (`out`: a pushed output)."""
    if r.done or r.pick is None:
        return r
    return r.model_copy(update={"questions": (build_question(r.pick, pick_raw(r.pick, out)),)})


def _pushed(pick: CheckPick) -> dict | None:
    """The pushed item for `pick`: from the first output that has questions; a self-report takes
    its item and leaves the output's other self-reports for the next picks (the old round of
    several self-reports is asked one pick after another); a quiz takes the whole output (a bank:
    the one for the picked node is kept)."""
    import json

    from tests.wikiapp.script import Script
    sc = Script.latest
    queue = sc._queues.get(PROBE) if sc is not None else None
    for at, raw in enumerate(queue or ()):
        out = json.loads(raw)
        if not out.get("questions"):
            continue
        item = pick_raw(pick, out)
        del queue[at]
        rest = [q for q in out["questions"] if q is not item]
        if pick.intent == "self_report" and any(q.get("kind") == "self_report" for q in rest):
            queue.insert(at, json.dumps({"questions": rest}, ensure_ascii=False))
        return item
    return None


def question_of(r: ProbeRound) -> ProbeQuestion:
    """The scripted assistant's question on `r`'s pick (see the module doc)."""
    pick = r.pick
    assert pick is not None and not r.done, r
    if pick.pick_id not in _ASKED:
        _ASKED[pick.pick_id] = build_question(pick, _pushed(pick))
    return _ASKED[pick.pick_id]


def open_question(res) -> ProbeQuestion:
    """The question of an open DraftResult's round."""
    return question_of(res.draft.rounds[-1])


def answer_round(repo, gw, user_id: str, draft_id: str, answers, *, now, new_id):
    """His answers, as ask_check records each (one question a round): `answers` in order, one
    per pick while the draft stays open (several = the old multi-question round, now asked one
    pick after another). The result carries every answered question."""
    import dataclasses

    from evowiki.wikiapp.goal import answer_check
    answered: list = []
    res = None
    for a in answers:
        d = repo.load(user_id).state.drafts.get(draft_id)
        r = d.rounds[-1] if d is not None and d.rounds else None
        if r is None or r.done or r.pick is None:      # closed / unknown: answer_check refuses it
            none = ProbeQuestion(kind="quiz", target="x.term", stem="x", options=("a", "b"))
            return answer_check(repo, gw, user_id, draft_id, "", none, a, now=now,
                                new_id=new_id)
        res = answer_check(repo, gw, user_id, draft_id, r.pick.pick_id, question_of(r), a,
                           now=now, new_id=new_id)
        answered += res.answered
        if not res.open:
            break
    assert res is not None, answers
    return dataclasses.replace(res, answered=tuple(answered))


def push_round(s, quizzes: Sequence[Quiz] = (), *, selfs: Sequence[dict] = (),
               **_ignored) -> list[CheckItem]:
    """One scripted output (the old probe model's shape): self-report questions first, then the
    quizzes; the scripted assistant asks the items for the picks in turn (`question_of`; old
    keywords such as `done`, `known`, `title` are accepted and ignored). Returns the quizzes as
    they are shuffled (diagnostic._shuffled), so a test knows each question's key."""
    s.push(PROBE, {"questions": [*selfs, *({"kind": "quiz", "target": c, "stem": st,
                                           "options": list(o), "answer": a}
                                          for c, st, o, a in quizzes)]})
    return [shuffled(*q) for q in quizzes]


NO_QUESTION = {"no_question": True}


def opening_answered(repo, map_id: str, *, now, new_id) -> None:
    """The goal's opening as if answered to its end, not skipped: `stop_draft` also records 不用摸底,
    and then no later point of the goal is diagnosed."""
    snap = repo.load("u1")
    m = snap.state.maps[map_id]
    record = m.model_copy(update={"log": tuple(x for x in m.log if x != NO_PROBE_LINE)})
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=new_id("wev"), ts=now,
                                             body=GoalMapSaved(record=record, action="edited"))]))


def push_no_question(s) -> None:
    """The next diagnosis asks nothing (tests/conftest.py `_no_question`)."""
    s.push(PROBE, NO_QUESTION)


def right(q) -> int:
    """Every quiz right, every self-report 「学过，还会」 (know)."""
    return q.answer if q.kind == "quiz" else q.means.index("know")


def wrong(q) -> int:
    """Every quiz wrong, every self-report 「学过，忘得差不多了」 (forgot)."""
    if q.kind == "quiz":
        return (q.answer + 1) % len(q.options)
    return q.means.index("forgot") if "forgot" in q.means else 0


def idk(q) -> int:
    return IDK


def answer_all(res, answer: Callable[[object], int], step: Callable) -> tuple[object, list]:
    """Answer every open round with `answer(question)` through `step(draft_id, answers)` until
    the draft finishes; returns the last result and the answered rounds."""
    rounds = []
    while res.open:
        r = res.draft.rounds[-1]
        q = question_of(r)
        rounds.append(r.model_copy(update={"questions": (q,)}))
        res = step(res.draft.draft_id, [answer(q)])
    return res, rounds


def push_one_page(s, concepts: Sequence[str], objective: str = "学完这页你能说清它") -> None:
    """The path model keeps the goal on one page."""
    s.push("goals.path", {"pages": [{"concepts": list(concepts), "objective": objective}]})


def push_pages(s, *pages: Sequence[str]) -> None:
    s.push("goals.path", {"pages": [{"concepts": list(p), "objective": f"学完这页你能用{p[0]}"}
                                    for p in pages]})


def plan_and_make(repo, gw, user_id: str, goal: str, words: Sequence[str] = (), *, now, new_id,
                  sources=(), use="learn", mode="follow", extra=(), on_commit=None):
    """A goal as the chat makes it: S1 (`plan_goal_blocks`, one call: the assistant's new_goal)
    and then the map of its blocks as they are (`create_goal_from_blocks`: the learner's 确认 of
    the card, unedited). `calls` counts S1 too."""
    import dataclasses

    from evowiki.wikiapp.goal import create_goal_from_blocks, plan_goal_blocks
    before = gw.calls
    res = plan_goal_blocks(repo.load(user_id), gw, user_id, goal, words, now=now, sources=sources,
                           use=use, mode=mode)
    spent = gw.calls - before
    out = create_goal_from_blocks(
        repo, gw, user_id, res.goal, res.blocks, now=now, new_id=new_id, words=res.words,
        limits=res.limits, beginner=res.beginner_quote, title=res.title, sources=sources,
        source_use=use, material_use=mode, extra=extra, on_commit=on_commit)
    return dataclasses.replace(out, calls=out.calls + spent)


def chat_goal(client, uid: str, goal: str, source_ids=(), *, words=(), use=None) -> dict:
    """A goal as the chat makes it, on the app's services (the old POST /wiki/goals is gone): S1
    over the attached materials (`plan_goal_blocks`, one call: the assistant's new_goal) and the
    learner's 确认 of the card as it is (`create_goal_from_blocks_with_sources`). `use`: how the
    goal uses its materials (reference / follow; None: §2.5's default). The result as the goal
    routes answer it (GoalDraftOut JSON, its map attached)."""
    from evowiki.service.routes_wiki import _draft_out
    from evowiki.wikiapp.goal import plan_goal_blocks
    from evowiki.wikiapp.goal_sources import (
        create_goal_from_blocks_with_sources,
        load_materials,
        material_use,
    )
    from evowiki.wikiapp.llm import require
    from evowiki.wikiapp.repo import PgRepo
    svc = client.app.state.services
    repo, gw = PgRepo(svc.pool), require(svc.gateway, uid)
    materials = load_materials(svc.pool, uid, source_ids)
    decided = material_use(goal, use, materials)
    mode = decided.use if decided is not None and decided.use in ("reference", "follow") else "follow"
    plan = plan_goal_blocks(repo.load(uid), gw, uid, goal, words, now=svc.clock(),
                            sources=[m.toc for m in materials], mode=mode)
    res = create_goal_from_blocks_with_sources(
        repo, svc.pool, gw, uid, plan.goal, plan.blocks, source_ids, runs=svc.sources,
        title=plan.title, words=plan.words, limits=plan.limits, beginner=plan.beginner_quote,
        use=mode if materials else None, now=svc.clock(), new_id=svc.new_id)
    return _draft_out(svc, uid, res).model_dump(mode="json")


def first_page(client, uid: str, goal: str, *, words=(), answer=None) -> tuple[str, list[dict]]:
    """A goal's first page through the product's own routes, as a learner who always takes the
    first block: the goal as the chat makes it (`chat_goal`), each diagnosis round answered by
    `answer(round_out) -> choices` (None: 「跳过」), then the map's first node entered — a block is
    split one level and its first part entered in turn, until a point is opened and its path
    planned — and the path's first page started (POST …/next). Returns (page_id, every round as
    {"questions", "answers"}). For scripts that need a page per goal (live runs, persona checks)."""
    h = {"X-EvoWiki-User": uid}
    rounds: list[dict] = []

    def ok(r):
        assert r.status_code in (200, 201), r.text
        return r.json()

    def settle(d: dict) -> dict:
        while d.get("draft_id"):
            if answer is None:
                body = {"skip": True}
            else:
                choices = list(answer(d))
                rounds.append({"questions": d["questions"], "answers": choices})
                body = {"answers": choices}
            d = ok(client.post(f"/wiki/goals/{d['draft_id']}/answers", headers=h, json=body))
        return d

    d = settle(chat_goal(client, uid, goal, words=words))
    node = None
    for _ in range(8):                   # a block is split one level per entry
        if d.get("path_id"):
            page = ok(client.post(f"/wiki/paths/{d['path_id']}/next", headers=h))["page_id"]
            return page, rounds
        if d.get("page_id"):
            return d["page_id"], rounds
        m = d["map"]
        kids = [n for n in m["nodes"] if n["parent"] == node]
        if not kids:
            raise RuntimeError(f"「{goal}」: nothing to enter under {node}")
        node = kids[0]["id"]
        d = settle(ok(client.post(f"/wiki/maps/{m['map_id']}/regions/{node}/enter", headers=h)))
    raise RuntimeError(f"「{goal}」: no page after 8 entries")
