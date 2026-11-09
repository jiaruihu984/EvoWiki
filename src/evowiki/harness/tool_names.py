"""Every tool, and its arguments, that each agent"s runs can offer the model (design 2026-10-21
§3.1 H2): the names an H2 key may use. Static, so a pack is checked when it is written (and when it
is read) without importing an agent; tests/harness/test_tool_names.py keeps it equal to the tools
the agents really build. A run offers a subset (place_component only with components, the
assistant"s tools per turn), so an H2 key for a tool absent from one run is skipped there
(harness/apply.py), while a name outside this registry is never stored, except a forged tool's
(phase 3b P3)."""

import re
from collections.abc import Mapping
from types import MappingProxyType

TOOLS: Mapping[str, Mapping[str, tuple[str, ...]]] = MappingProxyType({
    "deep": {
        "read_k": ("concepts",),
        "read_page": ("page_id", "section_id"),
        "read_spec": ("exemplar", "offset", "part"),
        "list_sources": (),
        "read_source": ("offset", "page_from", "page_to", "precise", "section_id", "source_id"),
        "search_source": ("k", "query", "source_id"),
        "find_image": ("limit", "purpose", "query"),
        "draw_visual": ("block_id", "draw", "invisible", "photos", "revise", "section_id", "touch"),
        "place_image": ("alt", "candidate", "image_id", "section_id"),
        "write_section": ("after", "answer", "body", "cells", "cue", "depth", "heading", "role", "section_id", "simplifications"),
        "place_component": ("component_id", "config", "placement_id", "section_id", "why"),
        "read_ref": ("names",),
        "write_block": ("block", "block_id", "section_id", "why"),
        "render_check": ("interactive_id",),
        "finish": ("self_check", "series"),
    },
    "assistant": {
        "search_wiki": ("limit", "query"),
        "read_page": ("page_id", "section_id"),
        "read_k": ("concepts",),
        "web_search": ("limit", "query"),
        "fetch_url": ("url",),
        "run_python": ("code",),
        "propose_page_edit": ("answer", "cells", "cue", "heading", "kind", "page_id", "paragraphs", "removed", "section_id",
                              "text", "why"),
        "read_path": ("path_id",),
        "read_goals": (),
        "read_graph": ("node",),
        "read_reviews": (),
        "open_review": ("page", "this_page"),
        "read_review_digest": ("period",),
        "today_plan": (),
        "why": ("suggestion_id", "trigger_id"),
        "read_activity": ("days",),
        "read_usage": (),
        "read_rules": (),
        "split_page": ("page", "parts", "path_id", "why"),
        "merge_pages": ("pages", "title", "why"),
        "add_practice": ("covers", "kind", "page"),
        "illustrate_section": ("page", "request", "section_id"),
        "write_page": ("after", "covers", "node", "path_id", "title"),
        "cheat_sheet": ("pages", "path_id", "title"),
        "reorder_path": ("order", "path_id", "why"),
        "postpone": ("after", "pages", "path_id", "why"),
        "set_deadline": ("deadline", "goal_id"),
        "new_goal": ("again", "goal", "notes", "source_ids", "use", "words"),
        "edit_goal_blocks": ("limits", "ops"),
        "edit_reading": ("ops",),
        "set_goal_limits": ("asked", "goal_id", "limits"),
        "pause_goal": ("goal_id", "paused"),
        "split_node": ("node", "parts", "why", "words"),
        "edit_relation": ("dst", "kind", "op", "phrase", "src", "to", "why"),
        "mark_known": ("node",),
        "move_node": ("keep_old", "node", "parent", "why"),
        "make_cards": ("asked", "cards", "page"),
        "fix_card": ("answer", "asked", "card_id", "check", "cue", "opinion"),
        "drop_card": ("asked", "card_id"),
        "postpone_reviews": ("asked", "cells", "days", "goal_id", "scope"),
        "restyle_pages": ("item_id", "page_ids"),
        "remember_preference": ("applies", "domain", "pasted", "quote", "replaces", "text"),
        "read_memory_conflicts": (),
        "resolve_memory_conflict": ("conflict_id", "domain", "keep", "quote", "replace_with",
                                    "section"),
        "report_problem": ("problem", "quote"),
        "remember_about_me": ("applies", "quote", "replaces", "section", "text", "topic"),
        "bot_settings": ("asked", "changes"),
        "mark_source_use": ("asked", "source_id", "use"),
        "set_exemplar": ("asked", "page_id", "source_id"),
        "undo": ("action_id", "asked", "other_sessions"),
        "add_source": ("asked", "text", "title", "url"),
        "revise_page": ("page_id", "source_ids"),
        "list_sources": (),
        "read_source": ("offset", "page_from", "page_to", "precise", "section_id", "source_id"),
        "search_source": ("k", "query", "source_id"),
        "search_memory": ("k", "kinds", "page", "query", "since", "until"),
        "read_past_turn": ("anchor_id", "cursor", "memory_id", "message_id", "session_id", "version"),
        "forget_memory": ("keywords", "quote"),
        "cancel_card": ("action_id", "quote"),
        "forget_about_me": ("line", "quote"),
        # stage 3 (Skills, pending-op confirmation): built by the turn (assistant.turn_skills)
        "load_skill": ("skills",),
        "read_ref": ("path",),
        "offer": ("action_id", "args", "title", "tool"),
        "confirm": ("asked", "pending_id"),
        # stage 3 phase 2 (K and assessment): evidence and the diagnosis in chat
        "record_evidence": ("events",),
        "ask_check": ("pick_id", "quiz", "self_report"),
        "ask_rough": ("draft_id",),
        "skip_diagnosis": ("draft_id", "start_from"),
        "edit_path_blocks": ("asked", "goal_id", "ops"),
    },
    "bot": {
        "suggest": ("action", "title", "why"),
        "no_action": ("reason",),
    },
})


_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
RESERVED = frozenset({"call_tool", "find_tool", "request_tool"})   # forge's fixed tools


def is_builtin(agent: str, tool: str) -> bool:
    return tool in TOOLS.get(agent, {})


def known(agent: str, tool: str, arg: str | None) -> bool:
    """A built-in tool / argument of `agent`, or (P3, phase 3b) a forged tool's name by pattern:
    whether a forged tool exists is checked against the registry where the key is applied."""
    if is_builtin(agent, tool):
        return arg is None or arg in TOOLS[agent][tool]
    return (agent in ("deep", "assistant") and tool not in RESERVED
            and bool(_PATTERN.fullmatch(tool)) and (arg is None or bool(_PATTERN.fullmatch(arg))))
