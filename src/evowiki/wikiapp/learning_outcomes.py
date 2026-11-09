"""只读学习统计。描述已有证据，不参与 RSI 判断，也不把 K 当作客观正确率。"""

from collections.abc import Sequence
from datetime import datetime

from evowiki.belief.aspects import AspectEvent, Observe, PageRead
from evowiki.goals.contracts import Band
from evowiki.goals.review import _in_page_round
from evowiki.wikiapp.events import FollowupResolved, FollowupScheduled, WikiEvent
from evowiki.wikiapp.repo import Snapshot

BUCKETS = ("不足1天", "1-7天", "7-30天", "30天及以上")


def _rate(correct: int, total: int) -> dict:
    return {"correct": correct, "total": total,
            "rate": correct / total if total else None,
            "status": "有数据" if total else "没有数据"}


def learning_outcomes(snap: Snapshot, events: Sequence[AspectEvent],
                      wiki: Sequence[WikiEvent], *, now: datetime) -> dict:
    """按用户统计当前快照；events/wiki 必须属于该用户且与快照一致。

    间隔从同一格上一次客观答题、回想或学完开始算（半开区间 1/7/30 天）。
    只计 review: 的延迟作答，排除「考考我这页」和同刻作答。按格×作答计数，
    客观题与回想自评分列。首次作答缺少起点则不猜间隔。
    补讲指标只使用现有 learned 后续确认事件：这是“看讲解后自称学会”的代理，
    不代表已证明系统补讲造成进步；未确认单列，不进入正确率分母。
    K 分布只涵盖学完或测过的格子，是当前模型估计。
    """
    if now.tzinfo is None:
        raise ValueError("now 必须包含时区")
    counts = {key: {b: [0, 0] for b in BUCKETS}
              for key in ("delayed_review", "recall_self_report")}
    last = {}
    studied = set()
    unknown_interval = 0
    for event in sorted(events, key=lambda e: e.ts):
        if event.ts > now:
            continue
        if isinstance(event, PageRead):
            studied.add(event.cell)
            last[event.cell] = event.ts
        elif isinstance(event, Observe) and event.observation.kind in ("quiz", "check", "recall"):
            obs = event.observation
            studied.add(obs.cell)
            previous = last.get(obs.cell)
            if obs.evidence_ref.startswith("review:") and not _in_page_round(obs.evidence_ref):
                if previous is None:
                    unknown_interval += 1
                elif event.ts > previous:
                    days = (event.ts - previous).total_seconds() / 86400
                    bucket = BUCKETS[sum(days >= d for d in (1, 7, 30))]
                    key = "recall_self_report" if obs.kind == "recall" else "delayed_review"
                    count = counts[key][bucket]
                    count[0] += obs.polarity == "+"
                    count[1] += 1
            last[obs.cell] = event.ts
    learned = set()
    resolved = {}
    for event in sorted(wiki, key=lambda e: e.ts):
        if event.ts > now:
            continue
        body = event.body
        if isinstance(body, FollowupScheduled) and body.followup.choice == "learned":
            learned.add(body.followup.followup_id)
        elif isinstance(body, FollowupResolved) and body.followup_id in learned:
            resolved.setdefault(body.followup_id, body.outcome)
    reteach = _rate(sum(v == "confirmed" for v in resolved.values()), len(resolved))
    reteach.update(pending=len(learned - resolved.keys()),
                   definition="看讲解后自称学会，再做新确认题；不证明补讲的因果效果")
    distribution = dict.fromkeys((b.value for b in Band), 0)
    view = snap.kview(now)
    for cell in studied:
        distribution[view.cell(cell).band.value] += 1
    return {"user_id": snap.user_id, "as_of": now.isoformat(),
            **{key: {b: _rate(*n) for b, n in values.items()} for key, values in counts.items()},
            "review_without_interval": unknown_interval, "reteach": reteach,
            "k_distribution": distribution if studied else None,
            "k_status": "有数据（模型估计）" if studied else "没有数据"}
