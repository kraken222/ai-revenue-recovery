"""SQLite round-trips `DateTime(timezone=True)` values as naive (it has no real
tzinfo-aware column type), while Postgres preserves tzinfo correctly. Every value that
came from the DB is therefore untrustworthy re: tzinfo, and every comparison against a
freshly created `now` needs both sides normalized the same way — otherwise Python
raises on offset-naive vs offset-aware comparisons the moment you're on SQLite.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
