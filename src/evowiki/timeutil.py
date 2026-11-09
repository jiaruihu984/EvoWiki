from datetime import datetime


def days_between(a: datetime, b: datetime) -> float:
    """Return (b - a) in fractional days. Both datetimes must be timezone-aware."""
    if a.tzinfo is None or b.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return (b - a).total_seconds() / 86400.0
