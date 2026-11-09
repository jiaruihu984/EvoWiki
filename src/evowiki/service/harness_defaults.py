"""What each harness key is when the pack sets nothing (design 2026-10-21 §3.1), for display next
to the user's value. Every default is read from the constant of the module that uses it — never
copied — so a changed constant shows up here; harness/keys.py holds the bounds, and a test checks
that no bound sits below today's default."""

from evowiki.agent.compact import CompactPolicy
from evowiki.assistant import loop as assistant_loop
from evowiki.deep import agent as deep_agent
from evowiki.harness import keys as hk
from evowiki.harness.keys import KEYS
from evowiki.harness.pack import Value
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import tier_for
from evowiki.service.prompt_defaults import default_texts


def _tier(group: str, purpose: str) -> str:
    return tier_for(Account(user_id="-", group=group), purpose, None)  # type: ignore[arg-type]


def defaults() -> dict[str, Value]:
    """Key name → default, for every key in KEYS except H2 (tool descriptions are not listed in
    phase 1) and the H8 component note / preset keys (their names depend on the library).
    Raises KeyError when a key has no default: a new key must be listed here.

    Structural keys: H3 tools are on (True); H9 / H11 steps, H8 EvoUI examples are empty; H8 spec
    parts are what the writer reads today; H10 tiers are today's tier (deep: the primary writer
    chain; assistant / Bot: the purpose or group default). H10 thinking is None: the provider's
    own thinking setting applies, so it has no single value to show."""
    compact = CompactPolicy()
    ctx = assistant_loop.CONTEXT_DEFAULTS
    texts = default_texts()
    known: dict[str, Value] = {
        "H4.deep.max_tool_calls": deep_agent.MAX_TOOL_CALLS,
        "H4.deep.max_rewrites": deep_agent.MAX_REWRITES,
        "H4.deep.max_nudges": deep_agent.MAX_NUDGES,
        "H4.deep.max_truncated": deep_agent.MAX_TRUNCATED,
        "H4.assistant.max_tool_calls": assistant_loop.MAX_TOOL_CALLS,
        # the Bot has no model loop any more (the planner is gone), but harness/keys.py still
        # lists the three Bot keys, so each needs a shown default: the last values the planner had
        "H4.bot.max_tool_calls": 4,
        "H4.bot.max_nudges": 1,
        "H5.deep.keep_reads": deep_agent.KEEP_READS,
        "H5.deep.stub_over": deep_agent.STUB_OVER,
        "H5.assistant.context_budget": ctx.context_budget,
        "H5.assistant.selection_max": ctx.selection_max,
        "H5.assistant.paragraph_max": ctx.paragraph_max,
        "H5.assistant.top_k_pages": ctx.top_k_pages,
        "H5.assistant.read_keep_calls": assistant_loop.READ_KEEP_CALLS,
        "H5.assistant.read_stub_over": assistant_loop.READ_STUB_OVER,
        "H6.history_budget": compact.history_budget,
        "H6.keep_turns": compact.keep_turns,
        "H6.keep_budget": compact.keep_budget,
        "H6.hard_cap": compact.hard_cap,
        "H6.summary_max_tokens": compact.summary_max_tokens,
        # the forge coders' caps (forge/limits.py), mirrored as keys.FORGE_*; tests/harness/
        # test_forging.py keeps the two equal
        "H4.forge.max_tool_calls": hk.FORGE_CAP,
        "H4.forge.patch.max_tool_calls": hk.FORGE_PATCH_CAP,
        # dead knobs (deep.plan is gone, LLM-call consolidation stage 4): harness/keys.py still
        # defines them and keys need a shown default; nothing reads them any more
        "H8.deep.spec.plan": ["items"],
        "H8.deep.spec.write": list(deep_agent.WRITE_SPEC_PARTS),
        "H8.deep.evoui_examples": [],
        "H9.deep.before_write": [],
        "H9.assistant.before_answer": [],
        "H11.deep.after_write": [],
        "H10.deep.plan.tier": "deep",
        "H10.deep.write.tier": "deep",
        "H10.assistant.turn.tier": _tier("assistant", assistant_loop.PURPOSE),
        "H10.bot.plan.tier": _tier("proactive", "bot_plan"),
        # one key, one shown default: the tool level's. Without a pack value a code patch is
        # written on the deep tier (apply.forge_tier: None -> round._patch_step uses deep), so
        # this "good" is the default of forged tools only
        "H10.forge.code.tier": "good",
    }
    out: dict[str, Value] = {}
    for k in KEYS:
        if k.dim == "H1":
            out[k.name] = texts[k.name.removeprefix("H1.")]
        elif k.dim == "H3":
            out[k.name] = True
        elif k.dim == "H10" and k.name.endswith(".thinking"):
            out[k.name] = None
        else:
            out[k.name] = known[k.name]
    return out
