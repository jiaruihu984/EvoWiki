"""Product LLM purposes, information-flow rules and offline regression cases."""

from typing import NamedTuple


class Entry(NamedTuple):
    site: str
    rules: tuple[str, ...]
    offline: str


def _e(site: str, rules: tuple[str, ...], name: str) -> Entry:
    return Entry(site, rules, f"tests/llm_cases/test_{name}_cases.py")


INVENTORY: dict[str, Entry] = {
    # diagnosis
    "goals.intake": _e("src/evowiki/goals/intake.py", ("CREATE",), "intake"),
    # S2: entering a block splits it and estimates from his own words (one call, enter@v3); the
    # 细分 entrances (看不懂 / 答题有对有错 / asked to split, wikiapp/node_split.py) make the same
    # call: their cases are in test_split_cases.py / live/test_live_split.py
    "goals.enter": _e("src/evowiki/goals/intake.py", ("CREATE", "OWN_WORDS"), "intake"),
    "goals.page_fit": _e("src/evowiki/goals/page_fit.py", (), "page_fit"),
    "goals.rough_typed": _e("src/evowiki/assistant/tools/rough.py", (), "rough_typed"),
    "review.card_stale": _e("src/evowiki/bot/card_stale.py", (), "card_stale"),
    "goals.reading.unit": _e("src/evowiki/goals/reading_unit.py", ("CREATE",), "reading_unit"),
    "goals.reading.repair": _e("src/evowiki/goals/blocks.py", (), "intake"),
    # planning and pages
    "goals.path": _e("src/evowiki/goals/path.py", ("PATH",), "path"),
    # the writer outlines the page too (plan_page, stage 4): its check items follow the
    # organizer's rules (ASK, NO_VERIFIER, ITEM_FLAWS, CHECK_ALIGNED: goals.outline_context)
    "deep.write": _e("src/evowiki/deep/agent.py",
                     ("WRITER", "ASK", "NO_VERIFIER", "ITEM_FLAWS", "CHECK_ALIGNED"), "deep"),
    "wiki.feedback_understand": _e("src/evowiki/wikiapp/understand.py", ("OWN_WORDS",),
                                   "understand"),
    # the visualization step behind draw_visual: the decision per section, the drawing, and its
    # declarative route (block markup)
    "deep.visual": _e("src/evowiki/deep/visual.py", (), "visual"),
    "deep.visual.decide": _e("src/evowiki/deep/visual.py", (), "visual"),
    "deep.visual.recheck": _e("src/evowiki/deep/visual.py", (), "visual"),
    "deep.scene": _e("src/evowiki/deep/scene.py", (), "visual"),
    # graph
    # the graph's automatic groups that code cannot name (spec 2026-10-29 retire-curate §5)
    "graph.group_naming": _e("src/evowiki/wikiapp/graph_groups.py", ("GROUPS",), "group_naming"),
    # review
    "review.card_rewrite": _e("src/evowiki/wikiapp/review_cards.py",
                              ("ASK", "NO_VERIFIER", "ITEM_FLAWS", "FOLLOWUP_NEW"), "card_rewrite"),
    "review.followup_check": _e("src/evowiki/wikiapp/followup.py",
                                ("ASK", "NO_VERIFIER", "ITEM_FLAWS", "FOLLOWUP_NEW"), "followup"),
    # rule provenance contract §2 / decision 1: the rule guard's judge (not a harness step)
    "teach.rule_conflict": _e("src/evowiki/teach/rule_judge.py", (), "rule_judge"),
    # assistant
    "assistant.turn": _e("src/evowiki/assistant/loop.py", (), "assistant"),
    "assistant.compact": _e("src/evowiki/assistant/compact.py", (), "assistant"),
    # cross-session memory: what a finished conversation leaves, and rules that cannot both hold
    "assistant.consolidate": Entry("src/evowiki/memory/consolidate.py", (),
                                   "tests/memory/test_consolidate.py"),
    "memory.conflicts": Entry("src/evowiki/memory/conflicts.py", (), "tests/memory/test_conflicts.py"),
    "memory_anchor": Entry("src/evowiki/memory/anchors.py", (), "tests/memory/test_anchors.py"),
    "source.vision": Entry("src/evowiki/sources/claude_pages.py", (), "tests/sources/test_claude_pages.py"),
    # the conversation summary's fixed evaluator (protected: evaluation/**)
    "eval.compact.judge": _e("src/evowiki/evaluation/compact.py", (), "compact_eval"),
    # the page-writing evaluator's core (protected: evaluation/**; RSI-B's Gate judge metrics)
    "eval.page.judge": _e("src/evowiki/evaluation/page.py", (), "page_eval"),
    # materials (资料) import: material text is untrusted data, fenced in every prompt
    "sources.toc": _e("src/evowiki/sources/toc.py", (), "sources"),
    "sources.summary": _e("src/evowiki/sources/summary.py", (), "sources"),
    "sources.glossary": _e("src/evowiki/sources/glossary.py", (), "sources"),
    # a supplement material against the user's pages (materials phase 6 §7 D)
    "sources.match": _e("src/evowiki/sources/match.py", (), "sources"),
    # exam items (真题) extracted from an exam material (materials phase 5 F)
    "sources.exam": _e("src/evowiki/sources/exam.py", (), "sources"),
    # exam items attached to goal nodes (materials phase 5 F)
    "sources.exam_attach": _e("src/evowiki/sources/exam_attach.py", (), "sources"),
}

# Product code the inventory scan covers (pipeline/, extract/, runtime/ask+bridges, view/naming,
# simulator, experiments are the legacy content-graph reader and research code, not the wiki).
PRODUCT_DIRS = ("src/evowiki/goals", "src/evowiki/wikiapp", "src/evowiki/wiki",
                "src/evowiki/deep", "src/evowiki/assistant", "src/evowiki/teach",
                "src/evowiki/evaluation", "src/evowiki/memory", "src/evowiki/sources",
                "src/evowiki/bot")

# The separate verifier call is removed and must stay removed (rule NO_VERIFIER).
# The old per-cell item generator was unreachable from the product and was deleted.
# Track A is fitted statistically (teach.fit): no LLM proposes numbers.
# The page's outline / plan / domain calls are the writer's loop now (stage 4), and so is
# add_practice (a practice run of the writer). S1 「定目标」: the goal refiner and the split
# levels are gone (one S1 call makes a goal; a block is split one layer when it is entered).
# The graph groups were named by the curator (stage 5); the curator (wiki_curate) is retired
# (2026-10-29) and the naming is its own step graph.group_naming. A K change's outline patch is the page
# writer's (skill design A7 item 5, 2026-10-28): goals.patch is gone. The question observer
# (goals.ask) is the assistant's own record_evidence tool (stage 3 T9); the diagnosis item writer
# (goals.probe) is the assistant's ask_check (T11; the KST cases moved to
# tests/goals/test_probe_kst.py). RSI-B and B2 retired on 2026-10-29 (the harness loop is the one
# RSI round): their proposer, patch reviewer, Gate judge and B2 reflection are gone.
REMOVED_PURPOSES = ("goals.diagnostic.verify", "goals.diagnostic.generate", "rsi.propose_a",
                    "goals.organize", "deep.plan", "wiki.teach.domain", "wiki.practice",
                    "goals.intake.refine", "goals.intake.expand", "graph.groups", "goals.patch",
                    "goals.intake.gap", "goals.ask", "goals.probe", "graph.merge",
                    "wiki.teach.import", "graph.split", "wiki_curate", "rsi.b.propose", "rsi.b.review_patch",
                    "rsi.b.gate_judge", "rsi.b2.reflect")
