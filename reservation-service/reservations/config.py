"""Tunables and the single clock / UTC helpers shared by every module."""

from __future__ import annotations

from datetime import datetime, timezone

# Read at hold-creation time (not import time) so tests can monkeypatch it.
HOLD_TTL_SECONDS: float = 120
# How often the background sweeper scans for expired holds. Read at startup.
SWEEP_INTERVAL_SECONDS: float = 1.0


def utcnow() -> datetime:
    """Single time source, so it can be swapped/frozen in tests."""
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """Normalise to aware UTC. Naive datetimes are interpreted as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
