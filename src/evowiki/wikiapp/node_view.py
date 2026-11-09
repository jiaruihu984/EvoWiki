"""One node of the graph as a prompt shows it, no ids anywhere (the evidence lines a 细分
shows the model, wikiapp.node_split). Pure reads of a snapshot."""

from dataclasses import dataclass
from datetime import datetime

from evowiki.goals.contracts import Aspect, cell_id
from evowiki.goals.guardrails import ASPECT_ZH, BAND_ZH
from evowiki.wikiapp.repo import Snapshot

MAX_NOTES = 12


@dataclass(frozen=True)
class NodeView:
    name: str
    definition: str
    aspects: tuple[str, ...]          # Chinese aspect labels of its K cells
    cells: tuple[str, ...]            # 「术语 / 定义：确定会」
    notes: tuple[str, ...]            # evidence lines: quiz results, the learner's words


def node_view(snap: Snapshot, node_id: str, now: datetime) -> NodeView:
    """LookupError: no such node."""
    rec = snap.state.graph.node(node_id)
    if rec is None:
        raise LookupError(f"没有这个节点：{node_id}")
    k = snap.k
    aspects: tuple[Aspect, ...] = k.aspects(node_id) if node_id in k.concept_ids() else ()
    view = snap.kview(now)
    cells = tuple(f"{ASPECT_ZH[a]}：{BAND_ZH[view.cell(cell_id(node_id, a)).band]}"
                  for a in aspects)
    notes = [n.label or n.text for n in k.notes(node_id)] if aspects else []
    return NodeView(name=rec.name, definition=rec.definition,
                    aspects=tuple(ASPECT_ZH[a] for a in aspects), cells=cells,
                    notes=tuple(notes[-MAX_NOTES:]))
