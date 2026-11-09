"""One-way, process-wide efficacy latch (Plan 4b-1 hard rule).

Once `lock_efficacy_for_process()` has been called, the transfer post-test scorer refuses every
call for the rest of the process. There is deliberately no unlock function: the manipulation
gate's entry point locks the process first, so nothing it runs can reach an efficacy outcome.
"""

_locked = False


def lock_efficacy_for_process() -> None:
    """Irreversibly forbid the transfer post-test in this process."""
    global _locked
    _locked = True


def efficacy_locked() -> bool:
    return _locked
