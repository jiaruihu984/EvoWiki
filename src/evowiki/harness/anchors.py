"""Fixed numbers of the harness-RSI loop (design 2026-10-21 §4–§8). Only the user changes this
file (same rule as teach/anchors.py); no improver output and no pack value reads or moves them."""

from evowiki.llm.prompt_steps import STEPS

MIN_CASES = 3                 # fewest failing sections worth a replay
MAX_PROBLEM_CASES = 5         # most failing sections the improver sees per round
SMOKE_CASES = 2               # sections the candidate pack writes in the round's smoke check
MAX_EXEMPLARS = 3             # most good sections shown next to the failing ones
WIN_MARGIN = 2                # judge wins a candidate needs over its baseline
LENGTH_RATIO = 1.3            # a replay longer than this × the baseline is a length veto
LAMBDA = 0.05                 # price of one unit of complexity (same role as RSI-B's gate had)
H4_MAX_RAISE = 1.5            # an H4 limit may rise at most this × its current value
IMPROVER_MAX_TOOL_CALLS = 60  # the improver's own tool budget
MAX_PROBES = 3                # most probe replays the improver may ask for (one section:
                              # the current pack and two candidates; 2026-10-28, was 9)
EVAL_SAMPLES = 2              # samples per case and pack in a round's evidence (`harness.evidence`)
AA_MIN_PAIRS = 12             # fewest pooled A/A pairs that can void a round (3 pairs cannot tell)
JUDGE_CALIBRATED = False      # the judge has no human labels yet: a passing change is only proposed
THIN_NOTICE_AFTER = 2         # runs with too little evidence before the user is told

# phase 3b: structural and code dims; only `deep` keys (IMPROVER_AGENTS), see diff.refuse
IMPROVER_DIMS = frozenset({"H1", "H2", "H3", "H4", "H5", "H7", "H8", "H9", "H10", "H11", "H12"})
IMPROVER_AGENTS = frozenset({"deep", "assistant"})   # assistant: only what a replay measures (diff.refuse)

# H1 steps the improver never reads as changeable nor writes (2026-10-28 audit, constraint 2): the
# RSI loop's own machinery. Judges and reviewers decide whether a change is better or allowed, so
# the loop that is judged must not rewrite them (no STEPS step is one since RSI-B's gate and
# reviewer retired on 2026-10-29; the harness judge and the page evaluator have no guidance marks).
# The user still edits them in Prompt 设置 (source "user"); only automatic edits are refused.
# The marks sit on the steps (llm/prompt_steps.Step.anchor): feedback is the evidence (the
# feedback reader makes the signals RSI learns from).
H1_JUDGE_STEPS = frozenset(s.key for s in STEPS if s.anchor == "judge")
H1_EVIDENCE_STEPS = frozenset(s.key for s in STEPS if s.anchor == "evidence")
H1_CLOSED_STEPS = H1_JUDGE_STEPS | H1_EVIDENCE_STEPS
