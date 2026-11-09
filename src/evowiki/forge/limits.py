"""Anchor constants of the forge. Only the user changes this file; no code path writes it."""
from dataclasses import dataclass

from evowiki import paths


@dataclass(frozen=True)
class SandboxLimits:
    wall_s: float
    fuel: int
    memory_bytes: int
    output_bytes: int


L1 = SandboxLimits(wall_s=10.0, fuel=2_000_000_000, memory_bytes=256 << 20, output_bytes=64 << 10)
WS_MAX_FILES = 200
WS_MAX_BYTES = 1 << 20
WS_ALLOWED = (r"^[a-z0-9_]+\.py$", r"^schema\.json$", r"^examples\.json$", r"^README\.md$")
CODER_MAX_TOOL_CALLS = 60          # hard ceiling; the pack's H4.forge.* may only lower it (phase 2)
CODER_MAX_NUDGES = 2
GREP_TIMEOUT_S = 0.5               # per line; a model-written regex must not hang the host (spec §10)
# Repeated-work detector (AWM / ASI; plan P2 T9): read fan-out in successful traces.
DETECT_FANOUT_MIN = 3              # distinct values of one argument of one read tool in one run
DETECT_RUNS_MIN = 3                # in this many distinct finished runs of the same user and agent
DETECT_WINDOW_DAYS = 14            # runs started within this many days
DETECT_SAMPLES_MAX = 6             # newest sample runs turned into examples
DETECT_RESULT_CHARS = 8000         # = host RPC result clip (T5): a longer result can't be an example
ROUND_MAX_PROPOSALS = 2            # propose calls (each one coder run) per forge round
FIRST_CLASS_MAX = 8                # forged tools offered first-class in a run tool list
FIND_TOOL_K = 5                    # find_tool returns the top-k by description similarity
REUSE_SIM = 0.88                   # nearest existing tool at or above this: reuse, do not forge
REQUEST_TOOL_MAX_PER_RUN = 1       # request_tool forgings per agent run (page write / chat turn)
HOLDOUT_MIN_EXAMPLES = 2          # fewer examples than this: nothing is held out (LATM validation)
HOLDOUT_DIVISOR = 3               # hold out max(1, n // HOLDOUT_DIVISOR) examples
# Host read-only RPC (plan P2 T5): the agent's own read tools a forged tool may call, per agent.
HOST_READ_TOOLS = {"deep": ("read_k", "read_page"),
                   "assistant": ("read_k", "read_page", "search_wiki"),
                   "bot": ()}
HOST_MAX_CALLS = 8                 # host calls per tool run (=> at most 9 guest executions)
HOST_RESULT_CHARS = 8000           # one host result as the guest sees it
HOST_ARGS_CHARS = 2000             # one host call's arguments, as JSON
HOST_TOTAL_WALL_S = 30.0           # all executions of one tool run together

# --- L2 code patches (phase 3) -------------------------------------------------------------
L2 = SandboxLimits(wall_s=600.0, fuel=0, memory_bytes=2 << 30, output_bytes=64 << 10)  # fuel unused
L2_MAX_PROCESSES = 16              # Job Object active-process cap (pytest + git + ruff, no servers)
L2_CMD_WALL_S = 600.0              # one run_cmd / one SWE pytest run
L2_MAX_TOOL_CALLS = 200            # coder ceiling; H4 forge.patch.max_tool_calls may only lower it
L2_MAX_CHANGED_FILES = 12          # a patch touching more files is refused (reviewability)
L2_MAX_DIFF_LINES = 600            # added + removed lines
L2_P2P_MAX_FILES = 60              # selected PASS_TO_PASS test files (import graph); over -> the nearest ones
JUNIT_MAX_BYTES = 5 << 20
SMOKE_TESTS = (                    # always in PASS_TO_PASS (fast, cover app import + both agents)
    "tests/service/test_routers_registered.py",
    "tests/agent/test_durable.py",
    "tests/deep/test_writer.py",
    "tests/assistant/test_loop.py",
    "tests/deep/test_render.py",
)
LIVE_BRANCH = "rsi/live"
LIVE_ROOT = str(paths.LIVE_ROOT)
FORGE_ROOT = str(paths.FORGE_ROOT)
FORGE_AUTHOR = "EvoWiki forge <forge@evowiki.local>"
IDLE_WAIT_S = 600                  # supervisor: max wait for in_flight == 0, then next poll
SUPERVISE_POLL_S = 30
CANARY_S = 60                      # /health + smoke within this many seconds of a restart
REPLAY_MAX_CALLS = 40              # model calls one replay child may ask the parent for
REPLAY_WALL_S = 1800.0
