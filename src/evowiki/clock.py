from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


class SystemClock:
    """Wall clock in UTC. Runtime code takes time only from an injected Clock."""

    def __call__(self) -> datetime:
        return datetime.now(UTC)
