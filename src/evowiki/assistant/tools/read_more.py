"""The assistant's read tools beyond search / read_page / read_k (assistant-actions spec §1.1):
`read_path`, `read_goals`, `read_graph`, `read_reviews`, `read_review_digest`, `why`,
`read_activity`, `read_usage`, `today_plan`, `read_rules`. They read the wiki (a snapshot per call), the Bot tables and the usage
tables; none writes, none touches the ledger, none calls a model. A tool that read one goal or point
names it in `ToolResult.summary` (the chat's tool line: 读了「学会 PCA」的学习路径)."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent.tool_helpers import clip, fail, run_guarded, spec
from evowiki.agent.tools import Source, ToolContext, ToolResult
from evowiki.goals.guardrails import ASPECT_ZH, BAND_WORD
from evowiki.sources.exam_store import exam_counts, frequent_nodes
from evowiki.wikiapp.graph_out import evidence_lines
from evowiki.wikiapp.paths import path_cards, path_items
from evowiki.wikiapp.repo import Repo
from evowiki.wikiapp.review import card_id, review_digest, review_page, review_today, sources
from evowiki.wikiapp.teachspec import effective_spec

CHARS = 4000
STATUS_ZH = {"not_started": "没开始", "learning": "在学", "done": "学完"}


@dataclass
class ReadDeps:
    """`pool`: Postgres (the Bot, usage tables); without it `why`, `read_activity`, `read_usage` and
    the suggestion part of `today_plan` are left out / empty."""

    repo: Repo
    clock: Callable[[], datetime]
    pool: Any = None
    timezone: str = "Asia/Shanghai"      # used only without a pool; else the user's Bot settings

    def tz(self, user_id: str) -> str:
        """The user's Bot-settings time zone (days of the learning streak)."""
        if self.pool is None:
            return self.timezone
        from evowiki.store import bot as store
        with self.pool.connection() as conn:
            return store.get_settings(conn, user_id).timezone

    def exam_concepts(self, user_id: str) -> frozenset[str]:
        """The frequently examined nodes (常考); none without a pool."""
        if self.pool is None:
            return frozenset()
        with self.pool.connection() as conn:
            return frequent_nodes(exam_counts(conn, user_id))


class _A(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Tool:
    def __init__(self, deps: ReadDeps, name: str, description: str, args: type[BaseModel],
                 body: Callable[[Any, ToolContext, ReadDeps], ToolResult]):
        self._d, self._args, self._body = deps, args, body
        self.spec = spec(name, description, args, "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(self._args, args, lambda a: self._body(a, ctx, self._d))


def _page_state(rec, page_id) -> str:
    if page_id is None:
        return "还没开始"
    return "写好了" if rec is not None and rec.has_body else "还没写好（排队或正在写）"


# ---- read_path / read_goals ----

class ReadPathArgs(_A):
    path_id: str | None = Field(default=None, description="省略=只有一条路径时读它，否则列出所有路径")


def read_path(a: ReadPathArgs, ctx: ToolContext, d: ReadDeps) -> ToolResult:
    snap = d.repo.load(ctx.user_id)
    state, now = snap.state, d.clock()
    pid = a.path_id
    if pid is None:
        if len(state.paths) == 1:
            pid = next(iter(state.paths))
        else:
            cards = path_cards(state, snap.k, now)
            if not cards:
                return ToolResult(content="还没有学习路径。")
            return ToolResult(content="学习路径（用 path_id 再读某一条）：\n" + "\n".join(
                f"- {c.path_id}「{c.goal}」{c.done}/{c.pages} 页学完" for c in cards))
    rec = state.paths.get(pid)
    if rec is None:
        return fail(f"没有这条学习路径：{pid}")
    lines = [f"路径 {pid}「{rec.path.goal}」，按现在的顺序："]
    for n, it in enumerate(path_items(state, rec, snap.k, now), 1):
        pg = state.pages.get(it.page_id) if it.page_id else None
        marks = [STATUS_ZH.get(it.status, it.status), _page_state(pg, it.page_id)]
        if it.mine:
            marks.append("你加的")
        if it.skipped:
            marks.append("已跳过")
        if it.skippable:
            marks.append("可跳")
        nodes = f" 点：{'、'.join(it.node_ids)}" if it.node_ids else ""
        page = f" 页：{it.page_id}" if it.page_id else ""
        lines.append(f"{n}. [{it.key}] {it.title}（{'，'.join(marks)}）{page}{nodes}")
    return ToolResult(content=clip("\n".join(lines), CHARS), summary=rec.path.goal[:200])


def _next_title(snap, path_id: str, now: datetime) -> str | None:
    """The first page of the path he has not finished (written or not), as the path shows it.
    `next_index` is another thing: the next page to start writing, None once all are written."""
    rows = path_items(snap.state, snap.state.paths[path_id], snap.k, now)
    return next((r.title for r in rows if r.status != "done" and not r.skipped), None)


def _diagnosis_lines(snap: Any, now: datetime) -> list[str]:
    """Goals whose opening diagnosis he left open: what is left, and how it comes back."""
    from evowiki.wikiapp.goal import _open_draft, rough_blocks
    out = []
    for mid, m in snap.state.maps.items():
        draft = _open_draft(snap.state, mid, None)
        found = rough_blocks(snap, draft.draft_id, now) if draft is not None else None
        if found is None:
            continue
        left = "、".join(n for _, n in found[1])
        out.append(f"- 目标「{m.title or m.goal}」摸底还没做完（draft_id：{found[0]}" + (f"，没答的块：{left}" if left else "") + "）。"
                   "他明说接着学这个目标才调 ask_rough（draft_id 用这个）；只说开始吧、没说哪个目标，就问他接着学哪个。摸底没做完不写这个目标的页。")
    return out


def read_goals(a: BaseModel, ctx: ToolContext, d: ReadDeps) -> ToolResult:
    snap = d.repo.load(ctx.user_id)
    now = d.clock()
    lines = []
    for c in path_cards(snap.state, snap.k, now):
        nxt = _next_title(snap, c.path_id, now)
        lines.append(f"- {c.path_id}「{c.goal}」{c.done}/{c.pages} 页学完"
                     + (f"，下一页「{nxt}」" if nxt else "，都学完了"))
    # a goal map with his conditions for it (the goal record, S1): set_goal_limits changes them. Its
    # blocks in order: 「最后那一块」 has no other way to be found out (edit_path_blocks takes the name)
    maps = [f"- 大目标 {mid}「{m.goal}」" + (f"（条件：{'；'.join(m.limits)}）" if m.limits else "")
            + (f"，块（按顺序）：{'、'.join(blocks)}" if (blocks := [n.name for n in m.children(None) if n.kind == "region"]) else "")
            for mid, m in snap.state.maps.items() if mid not in snap.state.paths]
    if not lines and not maps:
        return ToolResult(content="还没有学习目标。")
    return ToolResult(content=clip("目标和进度：\n" + "\n".join(lines + maps + _diagnosis_lines(snap, now)), CHARS))


# ---- read_graph ----

class ReadGraphArgs(_A):
    node: str = Field(min_length=1, description="点的 id 或名字（名字可以是片段，会列出匹配的）")


def read_graph(a: ReadGraphArgs, ctx: ToolContext, d: ReadDeps) -> ToolResult:
    snap = d.repo.load(ctx.user_id)
    graph, now = snap.state.graph, d.clock()
    node = graph.node(a.node)
    if node is None:
        hits = [n for n in graph.nodes if a.node.casefold() in n.name.casefold()]
        if not hits:
            return fail(f"图谱里没有叫「{a.node}」的点")
        if len(hits) > 1:
            return ToolResult(content="有几个点匹配（用 id 再读）：\n" + "\n".join(
                f"- {n.id}「{n.name}」" for n in hits[:20]))
        node = hits[0]
    name = {n.id: n.name for n in graph.nodes}
    kv = snap.kview(now)
    bands = [f"{ASPECT_ZH[x]}{BAND_WORD[kv.cell(f'{node.id}.{x}').band]}" for x in kv.aspects(node.id)] \
        if node.id in snap.k.concept_ids() else []
    rels = [r for r in graph.relations if r.src == node.id or r.dst == node.id]

    def ref(i: str) -> str:
        return f"{i}「{name.get(i, i)}」"
    lines = [f"点 {ref(node.id)}：{node.definition or '（没有定义）'}"]
    if parents := graph.parents(node.id):
        lines.append("上级：" + "、".join(ref(p) for p in parents))
    if kids := graph.children(node.id):
        lines.append("子点：" + "、".join(ref(c) for c in kids))
    pre = [r for r in rels if r.kind == "prerequisite" and r.dst == node.id]
    if pre:
        lines.append("前置：" + "；".join(
            f"{ref(r.src)}" + (f"（{r.phrase}）" if r.phrase else "")
            + ("" if r.verified else "（未核实）") for r in pre))
    after = [r for r in rels if r.kind == "prerequisite" and r.src == node.id]
    if after:
        lines.append("它是这些点的前置：" + "、".join(ref(r.dst) for r in after))
    if bands:
        lines.append("他学到：" + "、".join(bands))
    if ev := evidence_lines(snap.k, node.id)[:6]:
        lines.append("证据：" + "；".join(ev))
    return ToolResult(content=clip("\n".join(lines), CHARS), summary=node.name[:200])


# ---- read_reviews / open_review / today_plan ----

def _review_cooldown(ctx: ToolContext, d: ReadDeps) -> list[str]:
    """The sentence about a cooling 今日复习 card, as a list line; none without one or a pool."""
    if d.pool is None:
        return []
    from evowiki.bot.suggestions import review_cooldown_line
    with d.pool.connection() as conn:
        line = review_cooldown_line(conn, ctx.user_id, now=d.clock())
    return [] if line is None else [f"- 复习提醒：{line}"]


NOTHING_TO_REVIEW = "现在没有要复习的卡"
PAGE_ROUND_MIN = 3     # 「考考我这页」 with fewer cards than this makes more first
REVIEW_COUNTS = "只有他在复习卡上的提交才记入复习；聊天里答题只是讨论，不记入。"


class OpenReviewArgs(_A):
    this_page: bool = Field(default=False, description="「考考我这页」：考当前打开的这一页的卡，不管到没到期")
    page: str | None = Field(default=None, description="「考考我某一页」：那一页的 id；都不给=今天到期的卡")


def _round_text(out) -> str:
    head = (f"复习卡已经显示在这段对话里，共 {len(out.items) + len(out.confirms)} 张，题目和答案都在卡上，"
            f"他在卡上一张张交。{REVIEW_COUNTS}回答只说一句引导的话，不复述题目、不给提示。这轮的题：")
    lines = [head]
    lines += [f"- 确认题：{c.check.stem}" for c in out.confirms]
    lines += [f"- 「{it.page_title}」{it.heading}："
              + (f"确认题：{it.check.stem}" if it.check is not None else f"回想卡：{it.cue}")
              for it in out.items]
    return clip("\n".join(lines), CHARS)


def _page_short(snap, page_id: str, have: int) -> str | None:
    """Why this page's round cannot start yet: too few cards, and the sections that could have one."""
    rec = snap.state.pages[page_id]
    if rec.page is None or not rec.has_body:
        return f"《{rec.title}》还没写好，没有可考的内容"
    if have >= PAGE_ROUND_MIN:
        return None
    texts = rec.page.texts       # a cue without an answer is no card (`sources_from_page`)
    free = [s for s in rec.page.outline.sections if s.cells and s.id in texts and texts[s.id].body.strip()
            and not (texts[s.id].cue.strip() and texts[s.id].answer.strip())]
    if not free:
        return None if have else f"《{rec.title}》没有能做复习卡的段落"
    sections = "\n".join(f"- {s.id}「{s.heading}」" for s in free[:PAGE_ROUND_MIN + 3])
    return (f"《{rec.title}》（page_id={page_id}）现在有 {have} 张复习卡，不够考。先用 read_page 读这页，用 make_cards 给下面的段"
            f"各做一张回想卡（cue + answer，问正文里具体的点），再调一次 open_review。可以做卡的段：\n{sections}")


def open_review(a: OpenReviewArgs, ctx: ToolContext, d: ReadDeps) -> ToolResult:
    """Whether there is a round to open: the plugin draws the review card in the chat on this call's
    ok end (`assistant/controller.ts`, the same state and grade route as 今天); a page's round names
    the page in `sources`. The questions are here so he can talk about one; the answers are not."""
    page = ctx.page_id if a.this_page else a.page
    if a.this_page and page is None:
        return fail("现在没有打开的页面：问他考哪一页，或者不给 this_page 开今天到期的卡")
    if page is not None:
        snap = d.repo.load(ctx.user_id)
        if page not in snap.state.pages or page in snap.state.trashed_pages:
            return fail(f"没有这个页面：{page}")
        out = review_page(d.repo, ctx.user_id, page, now=d.clock(), timezone=d.tz(ctx.user_id),
                          exam_concepts=d.exam_concepts(ctx.user_id))
        if (why := _page_short(snap, page, len(out.items))) is not None:
            return fail(why)
        title = snap.state.pages[page].title
        return ToolResult(content=_round_text(out), sources=(Source(title=title, page_id=page),))
    out = review_today(d.repo, ctx.user_id, now=d.clock(), timezone=d.tz(ctx.user_id),
                       exam_concepts=d.exam_concepts(ctx.user_id))
    if len(out.items) + len(out.confirms) == 0:
        return fail(NOTHING_TO_REVIEW)
    return ToolResult(content=_round_text(out))


class ReadReviewsArgs(_A):
    pass


def read_reviews(a: BaseModel, ctx: ToolContext, d: ReadDeps) -> ToolResult:
    out = review_today(d.repo, ctx.user_id, now=d.clock(), timezone=d.tz(ctx.user_id),
                       exam_concepts=d.exam_concepts(ctx.user_id))
    lines = [f"今天到期 {out.due} 个，已做 {out.done_today}，每天最多 {out.limit}。要复习的卡："
             if out.items or out.confirms
             else f"现在没有要复习的（到期 {out.due} 个，今天已做 {out.done_today}）。"]
    lines += _review_cooldown(ctx, d)
    lines.append(f"（{REVIEW_COUNTS}他要现在复习用 open_review。）")
    for it in out.items:
        lines.append(f"- {it.card_id}「{it.page_title}」{it.heading}：{it.concept_name}·{it.aspect}"
                     f"（{BAND_WORD[it.band]}）"
                     + (f"，确认题：{it.check.stem}" if it.check is not None else f"，回想卡：{it.cue}（答案：{it.answer}）"))
    lines += [f"- 确认题 {c.check.stem}" for c in out.confirms]
    shown = {it.card_id for it in out.items}
    state = d.repo.load(ctx.user_id).state
    rest = [f"- {cid}「{state.pages[s.page_id].title}」{s.heading}：{kind}：{text}"
            for s in sources(state)
            for cid, kind, text in ((card_id(s.page_id, s.section_id, "cue"), "回想卡", s.cue),
                                    (card_id(s.page_id, s.section_id, "check"), "确认题",
                                     s.check.stem if s.check is not None else ""))
            if text and cid not in shown]
    if rest:                  # any card can be fixed or dropped, not only the ones in today's batch
        lines += ["其余的卡（还没到期，或排在今天之后）：", *rest]
    return ToolResult(content=clip("\n".join(lines), CHARS))


class DigestArgs(_A):
    period: Literal["week", "day"] = Field(default="week", description="week=这一周，day=今天")


DIGEST_MAX = 12          # cells listed per part (the rest is counted)
_PERIOD_ZH = {"week": "这一周", "day": "今天"}


def _cells(items: Sequence[Any], line: Callable[[Any], str]) -> str:
    shown_ = "、".join(line(x) for x in items[:DIGEST_MAX])
    return shown_ + (f" 等 {len(items)} 个" if len(items) > DIGEST_MAX else "")


def read_review_digest(a: DigestArgs, ctx: ToolContext, d: ReadDeps) -> ToolResult:
    """「这周学得怎么样」(the palette's 本周回顾 moved into the chat): `wikiapp.review.review_digest`,
    the same read as GET /wiki/review/digest. Reads only, no model call."""
    tz = d.tz(ctx.user_id)
    out = review_digest(d.repo, ctx.user_id, a.period, now=d.clock(), timezone=tz)
    try:
        zone: Any = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC

    def name(x) -> str:
        return f"{x.concept_name}·{ASPECT_ZH[x.aspect]}"
    start, end = out.start.astimezone(zone), out.end.astimezone(zone)
    lines = [f"{_PERIOD_ZH[a.period]}（{start:%m-%d} 到 {end:%m-%d}）的学习情况："]
    lines.append("进步了：" + (_cells(out.progressed, lambda b: (
        f"{name(b)}（现在：{BAND_WORD[b.to]}）")) or "没有"))
    lines.append("退步了：" + (_cells(out.slipped, lambda b: (
        f"{name(b)}（现在：{BAND_WORD[b.to]}{'，忘了' if b.forgot else ''}）"))
        or "没有"))
    lines.append("快忘了：" + (_cells(out.fading, lambda f: (
        f"{name(f)}（" + ("该复习了" if f.days_to_threshold <= 0
                         else f"大约 {f.days_to_threshold:.0f} 天后") + "）")) or "没有"))
    r = out.reviews
    lines.append(f"复习：想起来了 {r.recalled} 次，有点模糊 {r.fuzzy} 次，没想起来 {r.forgot} 次")
    t = out.today
    lines.append(f"今天：到期 {t.due} 个，已做 {t.done_today} 个，每天最多 {t.limit} 个"
                 + (f"；连续学了 {t.streak_days} 天" if t.streak_days else ""))
    return ToolResult(content=clip("\n".join(lines), CHARS))


class TodayPlanArgs(_A):
    pass


def today_plan(a: BaseModel, ctx: ToolContext, d: ReadDeps) -> ToolResult:
    """「我今天该学什么」: the open task cards, the due reviews and each path's next page, as one
    list. Reads only (the cards are not marked shown)."""
    snap = d.repo.load(ctx.user_id)
    now = d.clock()
    lines: list[str] = []
    if d.pool is not None:
        from evowiki.store import bot as store
        with d.pool.connection() as conn:
            cards = store.open_suggestions(conn, ctx.user_id, placement="today", page_id=None,
                                           now=now)
        lines += [f"- 任务：{c.title}" + (f"（{c.why}）" if c.why else "") for c in cards]
    rev = review_today(d.repo, ctx.user_id, now=now, timezone=d.tz(ctx.user_id),
                       exam_concepts=d.exam_concepts(ctx.user_id))
    if rev.items:
        lines.append(f"- 复习：今天到期 {rev.due} 个，先做最紧的 {len(rev.items)} 张")
    lines += _review_cooldown(ctx, d)
    for c in path_cards(snap.state, snap.k, now):
        nxt = _next_title(snap, c.path_id, now)
        if nxt is not None:
            lines.append(f"- 学：「{c.goal}」的下一页「{nxt}」")
    lines += _diagnosis_lines(snap, now)
    if not lines:
        return ToolResult(content="今天没有要做的事。")
    return ToolResult(content=clip("今天可以做：\n" + "\n".join(lines), CHARS))


# ---- why / read_activity / read_usage (Bot + usage tables) ----

class WhyArgs(_A):
    suggestion_id: str | None = Field(default=None, description="Bot 的某张卡（任务卡）的 id")
    trigger_id: str | None = Field(default=None, description="Bot 的某次触发的 id")


def why(a: WhyArgs, ctx: ToolContext, d: ReadDeps) -> ToolResult:
    if d.pool is None:
        return fail("现在查不了 Bot 的记录")
    from evowiki.bot.why import why as bot_why
    try:
        out = bot_why(d.pool, ctx.user_id, suggestion_id=a.suggestion_id, trigger_id=a.trigger_id)
    except (ValueError, LookupError) as e:
        return fail(str(e))
    lines = []
    if out.trigger is not None:
        lines.append(f"起因：{out.trigger.reason}")
    if out.gate is not None:
        lines.append(f"要不要提：{out.gate.outcome}" + (f"（{out.gate.reason}）" if out.gate.reason else ""))
    for o in out.outputs:
        lines.append(f"结果：{o.kind}「{o.title}」{o.status}")
    return ToolResult(content=clip("\n".join(lines) or "没有更多记录。", CHARS))


class ActivityArgs(_A):
    days: int = Field(default=7, ge=1, le=90)


def read_activity(a: ActivityArgs, ctx: ToolContext, d: ReadDeps) -> ToolResult:
    if d.pool is None:
        return fail("现在查不了 Bot 的记录")
    from evowiki.bot.why import recent
    from evowiki.store import bot as store
    now = d.clock()
    rows = recent(d.pool, ctx.user_id, now=now, days=a.days)
    with d.pool.connection() as conn:
        prefs = store.preference_rows(conn, ctx.user_id, now - timedelta(days=a.days))
    if not rows and not prefs:
        return ToolResult(content=f"最近 {a.days} 天 Bot 没有做什么。")
    tz = ZoneInfo(d.tz(ctx.user_id))
    lines = [f"- {at.astimezone(tz):%m-%d %H:%M} {summary}（{why}）" for at, summary, why in prefs]
    lines += _review_cooldown(ctx, d)
    lines += [f"- {r.at.astimezone(tz):%m-%d %H:%M} {r.title}（{r.result}，${r.cost_usd:.4f}，trigger_id={r.trigger_id}）" for r in rows]
    return ToolResult(content=clip("\n".join(lines), CHARS))


class UsageArgs(_A):
    pass


def read_usage(a: BaseModel, ctx: ToolContext, d: ReadDeps) -> ToolResult:
    if d.pool is None:
        return fail("现在查不了用量")
    from evowiki.store.usage import (
        day_start,
        get_settings,
        month_start,
        spend_by_group,
        usage_tz,
    )
    now, tz = d.clock(), usage_tz()
    lines = []
    with d.pool.connection() as conn:
        for label, since in (("今天", day_start(now, tz)), ("本月", month_start(now, tz))):
            groups = spend_by_group(conn, ctx.user_id, since)
            total = sum(g.cost_usd for g in groups.values())
            parts = "，".join(f"{k} ${g.cost_usd:.4f}" for k, g in groups.items() if g.cost_usd)
            lines.append(f"{label}：${total:.4f}" + (f"（{parts}）" if parts else ""))
        s = get_settings(conn, ctx.user_id)
    lim = [f"{n}上限 ${v:.2f}" for n, v in (("每日", s.daily_limit_usd), ("每月", s.monthly_limit_usd))
           if v is not None]
    return ToolResult(content="花费（美元，这里没有汇率）" + "；".join(lines) + ("。" + "，".join(lim) if lim else "。没设上限。"))


# ---- read_rules ----

SCOPE_ZH = {"any": "所有页面", "concept": "讲概念时", "exercise": "做练习时", "quick": "快速版"}
AUTHORITY_ZH = {"user": "你定的", "rsi": "自动改进可以调整", "pending": "等你确认"}


class RulesArgs(_A):
    pass


def read_rules(a: BaseModel, ctx: ToolContext, d: ReadDeps) -> ToolResult:
    """The teaching rules in force (the spec's items): text, where it applies, where it came from."""
    spec = effective_spec(d.repo.load(ctx.user_id).state)
    if not spec.items:
        return ToolResult(content="还没有讲解规则。")
    lines = [f"讲解规则（第 {spec.version} 版）："]
    for i in spec.items:
        where = SCOPE_ZH.get(i.scope, i.scope) + (f"，只用在「{i.domain}」" if i.domain else "")
        lines.append(f"- {i.item_id}：{i.text}（{where}；{AUTHORITY_ZH[i.authority]}）")
    return ToolResult(content=clip("\n".join(lines), CHARS))


def build_read_tools(deps: ReadDeps) -> list[Any]:
    t = lambda *x: _Tool(deps, *x)
    return [
        t("read_path", "读某个目标的学习路径：每一行的 key、标题、状态（读过 / 写好 / 还没写）、"
          "你加的、已跳过。改路径之前先读它，行 key 要用在 reorder_path / postpone / split_page 里。",
          ReadPathArgs, read_path),
        t("read_goals", "列出用户的学习目标和进度（path_id、几页学完了、下一页）。", ReadReviewsArgs,
          read_goals),
        t("read_graph", "读知识图谱里一个点：定义、上级、子点、前置（带一句话）、掌握档位、证据。"
          "改图谱之前先读它。", ReadGraphArgs, read_graph),
        t("read_reviews", "今天到期的复习卡和其余所有复习卡（card_id、哪一页哪一段、问题）。改或删卡之前先读。",
          ReadReviewsArgs, read_reviews),
        t("open_review", "他要现在复习（「帮我复习」「开始复习」）或要考考他（「考考我」「出道题考我」）：在这段对话里显示复习卡，"
          "他在卡上一张张交，题目不写在回答里。「考考我这页」给 this_page，「考考我某一页」给 page，这一页的卡不管到没到期都出。"
          "开始复习也会恢复「今日复习」卡的提醒。", OpenReviewArgs, open_review),
        t("read_review_digest", "「这周学得怎么样」「最近有什么进步」：这一周（或今天）哪些点进步了、"
          "哪些退步了、哪些快忘了、复习了几次。只读，不改任何东西。", DigestArgs, read_review_digest),
        t("today_plan", "「我今天该学什么」：把今天的任务卡、到期复习、各路径的下一页排成一份清单；"
          "「为什么没提醒我复习」也看这里。",
          TodayPlanArgs, today_plan),
        t("why", "查 Bot 为什么提了某张卡 / 做了某件事（给 suggestion_id 或 trigger_id）。", WhyArgs, why),
        t("read_activity", "Bot 最近做了什么、花了多少，以及他点过的「少提醒」。", ActivityArgs,
          read_activity),
        t("read_usage", "今天 / 本月的用量（美元）和上限。", UsageArgs, read_usage),
        t("read_rules", "列出用户现在的讲解规则（每条的内容、适用范围、来源）。加或改规则之前先读。",
          RulesArgs, read_rules),
    ]
