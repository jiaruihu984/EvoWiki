"""Process drain state shared by HTTP admission and background workflows."""

import os
import threading

DRAINED = threading.Event()
if os.environ.get("EVOWIKI_START_DRAINED") == "1":
    DRAINED.set()


def drained() -> bool:
    """True while the supervisor has stopped admission for a restart."""
    return DRAINED.is_set()
