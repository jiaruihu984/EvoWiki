"""What a reader did in an EvoUI block comes back as evidence (chatgpt-observations §7.5: the quiz
results ChatGPT never sees, EvoWiki keeps).

POST /wiki/assets/{asset_id}/results {answers?, state?, events?, progress?}: the block is the stored
`evoui` asset (never the client's copy), so everything is graded here against the block's own key.

- `answers` [{quiz, question, chosen}] (onQuizResult, v1): a question whose `cell` is a known K cell
  becomes `quiz` evidence (`+` right, `-` wrong — like an opening-quiz pick) with evidence_ref
  `block:{page}:{section}:{asset}:{quiz}.{question}:{id}` (the `answer_ref` the UI passes to
  POST /wiki/pages/{page}/checks/{section}/followup after a wrong answer); questions without a
  cell only count in the score. An answer to a question an item event of the same post also
  answers is dropped (the event wins: a host forwarding both is not counted twice).
- `events` (evoui.js onEvent, EvoUI v2 D3), each located by its node key (`deep.evoui.node_keys`):
  `item` events are graded — quiz by `correct` (multi: the same set), fill-blank by `correct`,
  sentence by `answer` (the word indices in blank order), flashcard by self-report (`"know"`
  right, `"missed"` wrong), app by the client's own `correct` (graded "client"; only items the
  app node declares). A graded item whose cell (question / card / node / app item `cell`) is a
  known K cell is `quiz` evidence with strength 1.0 (0.5 when a hint was used, 0.6 for app items)
  and evidence_ref `block:{page}:{section}:{asset}:{node}.{item}:{id}.{index}`; otherwise it only
  counts in the score. `summary`, `route` and `request` events go into the signal text (first, so
  only the trailing score / state part can be cut at 2000 characters; event lines that alone do
  not fit refuse the post with 422) (a route's
  `preference` too: no per-user preference store exists without a schema change). An event
  naming a node / item / option / route value the block lacks, or with the wrong item kind, is
  refused (422) before anything is written. An event already recorded (same event key) is
  skipped and reported `duplicate`.
- `progress` {revision, data}: stored when its revision is greater than the stored one
  (`block_progress`), after the commit.

A library component with an answer key (an `html` asset, `deep.components.graded_config`) takes `item`
events only: node `component`, kind `component`, `item` the config's item id, `response` the target id it
was put on, or null when the answer was shown first. Graded here against the stored config; a known
`cell` makes it `quiz` evidence at the strength of app items (the component answers at once and lets the
reader try again, so this says less than a question answered blind). Its event key has no attempt,
revision or session: one result per item, ever, so a retry or a reloaded page never counts and an item
whose answer was shown first never can.

One `block_result` signal (score + per-case tally + summaries / routes / requests + final state) on
the block's section, when there is anything new; the K evidence runs the one K-change pass on the
block's page. 0 LLM calls unless that pass patches the page."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from evowiki.deep.components import graded_config
from evowiki.deep.evoui import node_keys, quizzes
from evowiki.deep.store import AssetStore, StoredAsset
from evowiki.goals.contracts import Observation
from evowiki.wikiapp.block_progress import Progress, ProgressStore
from evowiki.wikiapp.block_visibility import guard_asset_write
from evowiki.wikiapp.kchange import k_changed
from evowiki.wikiapp.kwrite import Ids, apply_in_memory, observation_events
from evowiki.wikiapp.lineage import asset_artifact
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import Commit, Repo, Snapshot, WikiInvalid
from evowiki.wikiapp.signals import signal_event
from evowiki.wikiapp.wiki_schemas import (
    BlockAnswerOut,
    BlockCaseOut,
    BlockEventIn,
    BlockEventOut,
    BlockProgressOut,
    BlockResultsIn,
    BlockResultsOut,
)

BLOCK_ACTION = "block_result"
LABEL_MAX = 600
# node kind -> the item `kind` its events carry (D3)
ITEM_KINDS = {"quiz": "quiz", "flashcards": "flashcard", "fill-blank": "fill-blank",
              "sentence": "sentence", "app": "app", "component": "component"}
COMPONENT_NODE = "component"               # the one node of a graded library component
HINT_STRENGTH = 0.5
APP_STRENGTH = 0.6
FLASH_TEXT = {"know": "记得", "missed": "没记住"}
SIGNAL_TEXT_MAX = 2000                     # Signal.text


@dataclass(frozen=True)
class _Graded:
    correct: bool | None                   # None: an app item the app did not judge
    graded: Literal["server", "client"]
    cell: str | None
    strength: float
    text: str                              # note text (the response)
    label: str                             # note label (item, response, key)
    case: tuple[int, str] | None = None    # (quiz index, case id) of a diagnostic quiz question


def _label(q: dict[str, Any], chosen: int) -> str:
    """The note label: the question, the pick and the key (a follow-up quotes it)."""
    return (f"题目：{q['q']}；选了「{q['options'][chosen]}」；"
            f"正确答案「{q['options'][q['correct']]}」")[:LABEL_MAX]


def event_key(ev: BlockEventIn) -> str:
    """The dedup key (`block_event.event_key`): item `{node}:{item as JSON}:{attempt}:{revision}`,
    the others `{node}:{type}:{attempt}:{revision}` (a bare word never equals a JSON int or string;
    node keys have no colon), prefixed `{session}|` when the event names its render's session (attempt
    and revision restart in a render that did not restore progress, so without it a fresh answer could
    look like a resend; a key without the prefix never starts with hex then `|`). A library component's
    item: `{node}:{item as JSON}`, one result per item whatever render sent it."""
    if ev.kind == COMPONENT_NODE:
        return f"{ev.node}:{json.dumps(ev.item, ensure_ascii=False)}"
    if ev.type == "item":
        key = f"{ev.node}:{json.dumps(ev.item, ensure_ascii=False)}:{ev.attempt}:{ev.revision}"
    else:
        key = f"{ev.node}:{ev.type}:{ev.attempt or 0}:{ev.revision}"
    return f"{ev.session}|{key}" if ev.session else key


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _same(a: Any, b: Any) -> bool:
    """A route value equals the client's: numbers numerically (2.0 is 2, as in JavaScript), never a
    bool with a number (1 is not true), text only text ("1" is not 1); anything else JSON-equal."""
    num = lambda x: isinstance(x, int | float) and not isinstance(x, bool)
    if num(a) or num(b):
        return num(a) and num(b) and a == b
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True,
                                                                         ensure_ascii=False)


def _join(words: list[Any], idx: list[int]) -> str:
    return " / ".join(str(words[i]) for i in idx)


def _grade_item(ev: BlockEventIn, node: dict[str, Any], quiz_index: dict[int, int]) -> _Graded:
    """Grade one item event against its stored node; WikiInvalid when it does not fit."""
    kind = node.get("kind")
    if ITEM_KINDS.get(kind) != ev.kind:
        raise WikiInvalid(f"{ev.node} 不是 {ev.kind} 题")
    hint = HINT_STRENGTH if ev.hint_used else 1.0
    r = ev.response
    if kind == COMPONENT_NODE:
        return _grade_component(ev, node)
    if kind == "quiz":
        qs = node.get("questions") or []
        if not _is_int(ev.item) or ev.item >= len(qs):
            raise WikiInvalid(f"{ev.node} 没有第 {ev.item} 题")
        q = qs[ev.item]
        opts = q.get("options") or []
        if q.get("type") == "multi":
            if not (isinstance(r, list) and all(_is_int(i) and 0 <= i < len(opts) for i in r)
                    and len(set(r)) == len(r)):
                raise WikiInvalid(f"{ev.node} 第 {ev.item} 题没有这些选项：{r}")
            key = q.get("correct") or []
            correct = sorted(r) == sorted(key)
            text = _join(opts, sorted(r)) or "（没选）"
            label = (f"题目：{q.get('q', '')}；选了「{text}」；正确答案「{_join(opts, sorted(key))}」")
        else:
            if not (_is_int(r) and 0 <= r < len(opts)):
                raise WikiInvalid(f"{ev.node} 第 {ev.item} 题没有这个选项：{r}")
            correct = r == q.get("correct")
            text = str(opts[r])
            label = _label(q, r)
        case = q.get("case")
        return _Graded(correct, "server", q.get("cell"), hint, text, label[:LABEL_MAX],
                       (quiz_index[id(node)], case) if isinstance(case, str) else None)
    if kind == "flashcards":
        cards = node.get("cards") or []
        if not _is_int(ev.item) or ev.item >= len(cards):
            raise WikiInvalid(f"{ev.node} 没有第 {ev.item} 张卡")
        if not isinstance(r, str) or r not in FLASH_TEXT:
            raise WikiInvalid(f"卡片的回答只能是 know / missed：{r}")
        c = cards[ev.item]
        label = f"卡片：{c.get('front', '')}；自评「{FLASH_TEXT[r]}」；背面「{c.get('back', '')}」"
        return _Graded(r == "know", "server", c.get("cell"), hint, FLASH_TEXT[r], label[:LABEL_MAX])
    if kind in ("fill-blank", "sentence") and ev.item != 0:
        raise WikiInvalid(f"{ev.node} 只有第 0 题")
    if kind == "fill-blank":
        opts = node.get("options") or []
        if not (_is_int(r) and 0 <= r < len(opts)):
            raise WikiInvalid(f"{ev.node} 没有这个选项：{r}")
        lab = [o.get("label", "") if isinstance(o, dict) else str(o) for o in opts]
        key = node.get("correct")
        right = lab[key] if _is_int(key) and 0 <= key < len(lab) else ""
        label = f"填空：{node.get('sentence', '')}；选了「{lab[r]}」；正确答案「{right}」"
        return _Graded(r == key, "server", node.get("cell"), hint, lab[r] or "（空）",
                       label[:LABEL_MAX])
    if kind == "sentence":
        words = node.get("words") or []
        key = node.get("answer") or []
        if not (isinstance(r, list) and len(r) == len(key)
                and all(_is_int(i) and 0 <= i < len(words) for i in r)):
            raise WikiInvalid(f"{ev.node} 的回答要按空的顺序给出 {len(key)} 个词的序号：{r}")
        text = _join(words, r)
        label = (f"排句：{node.get('sentence', '')}；填了「{text}」；"
                 f"正确答案「{_join(words, [i for i in key if _is_int(i) and 0 <= i < len(words)])}」")
        return _Graded(list(r) == list(key), "server", node.get("cell"), hint, text or "（空）",
                       label[:LABEL_MAX])
    # app: only declared items; the app judges itself
    items = {it.get("id"): it for it in node.get("items") or [] if isinstance(it, dict)}
    if not isinstance(ev.item, str) or ev.item not in items:
        raise WikiInvalid(f"{ev.node} 没有声明这一项：{ev.item}")
    it = items[ev.item]
    text = json.dumps(r, ensure_ascii=False)[:200]
    verdict = "对" if ev.correct else "不对" if ev.correct is False else "未判"
    label = f"小程序：{it.get('label', ev.item)}；结果 {text}；程序判定「{verdict}」"
    return _Graded(ev.correct, "client", it.get("cell"), min(hint, APP_STRENGTH), text or "null",
                   label[:LABEL_MAX])


def _grade_component(ev: BlockEventIn, config: dict[str, Any]) -> _Graded:
    """One placement against the component's stored config; `response` null: the answer was shown first
    (no verdict, no evidence, and the key is spent)."""
    items = {i["id"]: i for i in config["items"]}
    targets = {t["id"]: t.get("label", t["id"]) for t in config["targets"]}
    r = ev.response
    if not isinstance(ev.item, str) or ev.item not in items:
        raise WikiInvalid(f"这个组件没有这一项：{ev.item}")
    if r is not None and (not isinstance(r, str) or r not in targets):
        raise WikiInvalid(f"这个组件没有这个位置：{r}")
    it = items[ev.item]
    if r is None:
        return _Graded(None, "server", None, APP_STRENGTH, "显示答案", "")
    label = (f"{config.get('title', '')}：「{it.get('label', ev.item)}」放进「{targets[r]}」；"
             f"正确答案「{targets.get(it['answer'], it['answer'])}」")
    return _Graded(r == it["answer"], "server", it.get("cell"), APP_STRENGTH, str(targets[r]),
                   label[:LABEL_MAX])


def _event_line(ev: BlockEventIn, node: dict[str, Any], g: _Graded | None) -> str | None:
    """The event's part of the signal text (None: item events that only count in the score)."""
    if ev.type == "item":
        if g is not None and g.correct is None and ev.kind != COMPONENT_NODE:
            return f"{ev.node} {ev.item}：{g.text}"
        return None
    if ev.type == "summary":
        parts = [f"{ev.node} 第 {ev.attempt} 轮完成"]
        if ev.total is not None and ev.right is not None:
            parts.append(f"{ev.right}/{ev.total}")
        if ev.missed:
            parts.append("没过 " + ",".join(str(m) for m in ev.missed[:20]))
        if ev.time_ms is not None:
            parts.append(f"用时 {round(ev.time_ms / 1000)} 秒")
        if ev.timer_ms is not None:                     # a timer node ran while this was done
            parts.append(f"计时 {round(ev.timer_ms / 1000)} 秒")
        if ev.hints_used:
            parts.append(f"看了 {ev.hints_used} 次提示")
        return " ".join(parts)
    if ev.type == "route":
        opt = next(o for o in node.get("options") or [] if _same(o.get("value"), ev.option))
        line = f"{ev.node} 选了「{opt.get('label', '')}」"
        if opt.get("next"):
            line += f"→{opt['next']}"
        if node.get("preference"):
            line += f"（偏好 {node['preference']} = {json.dumps(ev.option, ensure_ascii=False)}）"
        return line
    return f"{ev.node} 要更多（{ev.what}）"


def block_results(repo: Repo, assets: AssetStore, gw: CountingGateway | None, user_id: str,
                  asset_id: str, body: BlockResultsIn, *, now: datetime, new_id: Ids,
                  progress: ProgressStore | None = None) -> BlockResultsOut:
    """See the module doc. 404 unknown asset (or another user's); 422 not an evoui asset, or an
    answer / event naming a quiz / question / option / node / item the block does not have.
    `progress` is required when the body carries events or progress."""
    a = _asset(assets, user_id, asset_id)
    if (body.events or body.progress is not None) and progress is None:
        raise RuntimeError("block events / progress need a ProgressStore")
    page_id, sid = a.meta.page_id, a.meta.section_id
    if a.meta.kind == "html":
        config = graded_config(a.html)
        if config is None or body.progress is not None or any(e.type != "item" for e in body.events):
            raise WikiInvalid("这个交互不回传这样的结果")
        block: dict[str, Any] = {"title": a.meta.title}
        keys = {COMPONENT_NODE: {**config, "kind": COMPONENT_NODE}}
    else:
        block = _block_json(a)
        keys = node_keys(block)
    qz = quizzes(block.get("body"))
    quiz_index = {id(z): i for i, z in enumerate(qz)}

    # 1. validate and grade everything before anything is written (fail closed)
    graded: list[_Graded | None] = []
    covered: set[tuple[int, int]] = set()          # quiz questions an item event answers
    for ev in body.events:
        node = keys.get(ev.node)
        if node is None:
            raise WikiInvalid(f"块里没有这个交互节点：{ev.node}")
        g = None
        if ev.type == "item":
            g = _grade_item(ev, node, quiz_index)
            if node.get("kind") == "quiz":
                covered.add((quiz_index[id(node)], ev.item))  # type: ignore[arg-type]
        elif ev.type == "route":
            if node.get("kind") != "route":
                raise WikiInvalid(f"{ev.node} 不是选择路线")
            if not any(isinstance(o, dict) and _same(o.get("value"), ev.option)
                       for o in node.get("options") or []):
                raise WikiInvalid(f"{ev.node} 没有这个选项：{ev.option}")
            if ev.preference != node.get("preference"):
                raise WikiInvalid(f"{ev.node} 的偏好键不是 {ev.preference}")
        graded.append(g)
    picked: dict[tuple[int, int], int] = {}
    for ans in body.answers:                       # the last answer to a question wins
        if ans.quiz >= len(qz) or ans.question >= len(qz[ans.quiz]["questions"]):
            raise WikiInvalid(f"块里没有第 {ans.quiz} 组第 {ans.question} 题")
        if ans.chosen >= len(qz[ans.quiz]["questions"][ans.question]["options"]):
            raise WikiInvalid(f"第 {ans.quiz} 组第 {ans.question} 题没有这个选项：{ans.chosen}")
        if (ans.quiz, ans.question) not in covered:
            picked[(ans.quiz, ans.question)] = ans.chosen

    # 2. duplicates: recorded by an earlier post, or repeated within this one
    ekeys = [event_key(ev) for ev in body.events]
    seen = progress.seen(user_id, asset_id, set(ekeys)) if progress is not None and ekeys else set()
    fresh_keys: list[str] = []
    dup: list[bool] = []
    for k in ekeys:
        d = k in seen or k in fresh_keys
        dup.append(d)
        if not d:
            fresh_keys.append(k)

    snap = repo.load(user_id)
    rid = new_id("br")
    out: list[BlockAnswerOut] = []
    obs: list[tuple[Observation, str, str]] = []
    scored: list[tuple[bool, tuple[int, str] | None]] = []   # (correct, case) for right/total
    for (zi, qi), chosen in sorted(picked.items()):
        q = qz[zi]["questions"][qi]
        correct = chosen == q["correct"]
        cell = q.get("cell")
        ref = None
        if cell is not None and _known(snap, cell):
            ref = f"block:{page_id}:{sid}:{asset_id}:{zi}.{qi}:{rid}"
            obs.append((Observation(cell=cell, polarity="+" if correct else "-", strength=1.0,
                                    kind="quiz", evidence_ref=ref),
                        q["options"][chosen], _label(q, chosen)))
        out.append(BlockAnswerOut(quiz=zi, question=qi, correct=correct, case=q.get("case"),
                                  cell=cell, answer_ref=ref))
        scored.append((correct, (zi, q["case"]) if isinstance(q.get("case"), str) else None))

    ev_out: list[BlockEventOut] = []
    lines: list[str] = []
    for i, (ev, g) in enumerate(zip(body.events, graded, strict=True)):
        if dup[i]:
            ev_out.append(BlockEventOut(index=i, status="duplicate",
                                        correct=None if g is None else g.correct,
                                        graded=None if g is None else g.graded))
            continue
        line = _event_line(ev, keys[ev.node], g)
        if line is not None:
            lines.append(line)
        if g is None or g.correct is None:
            ev_out.append(BlockEventOut(index=i, status="signal",
                                        graded=None if g is None else g.graded))
            continue
        scored.append((g.correct, g.case))
        ref = None
        if g.cell is not None and _known(snap, g.cell):
            ref = f"block:{page_id}:{sid}:{asset_id}:{ev.node}.{ev.item}:{rid}.{i}"
            obs.append((Observation(cell=g.cell, polarity="+" if g.correct else "-",
                                    strength=g.strength, kind="quiz", evidence_ref=ref),
                        g.text, g.label))
        ev_out.append(BlockEventOut(index=i, status="k" if ref else "score", correct=g.correct,
                                    graded=g.graded, answer_ref=ref))

    cases: list[BlockCaseOut] = []
    for zi, z in enumerate(qz):
        for c in z.get("cases") or ():
            mine = [ok for ok, case in scored if case == (zi, c["id"])]
            if mine:
                cases.append(BlockCaseOut(case=c["id"], label=c["label"], advice=c.get("advice", ""),
                                          right=sum(mine), total=len(mine)))
    right, total = sum(ok for ok, _ in scored), len(scored)

    # the signal text: event lines first (they live only there), then score / cases / state, which
    # may be cut at the limit; event lines that do not fit refuse the post (fail closed)
    head = [f"交互结果《{block.get('title', '')}》", *lines]
    if len("；".join(head)) > SIGNAL_TEXT_MAX:
        raise WikiInvalid(f"一次回传的总结 / 选择 / 请求太多，记不下（超过 {SIGNAL_TEXT_MAX} 字），请分批发送")

    signal_id = None
    change = None
    if picked or body.state is not None or fresh_keys:
        notes = {o.evidence_ref: (text, label) for o, text, label in obs}
        aspect = observation_events([o for o, _, _ in obs], ts=now, new_id=new_id,
                                    note_text=lambda o: notes[o.evidence_ref][0],
                                    note_label=lambda o: notes[o.evidence_ref][1])
        wiki = []
        if aspect:
            k_after = apply_in_memory(snap, aspect)
            target = page_id if page_id in snap.state.pages else None
            kc = k_changed(snap, k_after, target, gw, skip=None, now=now,
                           new_id=new_id, action=BLOCK_ACTION)
            wiki += kc.wiki
            change = kc.change
        summary = list(head)
        if total:
            summary.append(f"测验答对 {right}/{total}")
            summary += [f"{c.label} {c.right}/{c.total}" for c in cases]
        if body.state is not None:
            summary.append("最终状态 " + json.dumps(body.state, ensure_ascii=False))
        signal = signal_event(snap.state, "block_result", now=now, new_id=new_id,
                              page_id=page_id, section_id=sid, text="；".join(summary)[:SIGNAL_TEXT_MAX],
                              outcome=(right / total) if total else None,
                              target=asset_artifact(user_id, asset_id))
        wiki.append(signal)
        extra = ([progress.events_writer(user_id, asset_id, fresh_keys, at=now)]
                 if progress is not None and fresh_keys else [])
        extra.insert(0, lambda conn: guard_asset_write(conn, user_id, asset_id))
        repo.commit(snap, Commit(aspect=aspect, wiki=wiki, extra=extra))
        sig = signal.body
        assert hasattr(sig, "signal")
        signal_id = sig.signal.signal_id

    prog_rev, stale = None, False
    if body.progress is not None:
        assert progress is not None
        prog_rev, stored = progress.put(user_id, asset_id,
                                        Progress(body.progress.revision, body.progress.data),
                                        at=now)
        stale = not stored
    return BlockResultsOut(asset_id=asset_id, page_id=page_id, section_id=sid, answers=out,
                           right=right, total=total, cases=cases, signal_id=signal_id,
                           change=change, events=ev_out, progress_revision=prog_rev,
                           progress_stale=stale)


def block_progress(assets: AssetStore, progress: ProgressStore, user_id: str,
                   asset_id: str) -> BlockProgressOut:
    """GET /wiki/assets/{asset_id}/progress: {revision, data}, or {revision: 0, data: None}.
    404 unknown asset, 422 not an evoui asset."""
    _block_json(_asset(assets, user_id, asset_id))
    p = progress.get(user_id, asset_id)
    return BlockProgressOut(revision=0) if p is None else BlockProgressOut(revision=p.revision,
                                                                           data=p.data)


def _asset(assets: AssetStore, user_id: str, asset_id: str) -> StoredAsset:
    a = assets.get(user_id, asset_id)
    if a is None:
        raise LookupError(f"没有这个交互：{asset_id}")
    return a


def _block_json(a: StoredAsset) -> dict[str, Any]:
    if a.meta.kind != "evoui":
        raise WikiInvalid("只有 EvoUI 块能回传结果")
    return json.loads(a.html)


def _known(snap: Snapshot, cell: str) -> bool:
    """A cell of a concept K knows (a block may name a cell that is gone, or a malformed one)."""
    try:
        snap.k.memory(cell)
    except (KeyError, ValueError):
        return False
    return True
