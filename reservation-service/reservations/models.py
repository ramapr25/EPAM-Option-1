"""
Domain model: immutable records plus the overlap / slot-occupancy rules.

    held ──confirm──> confirmed
     │                    │
     ├──cancel──> cancelled <──cancel──┘
     └──(TTL elapses)──> expired
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class ReservationStatus(str, Enum):
    held = "held"
    confirmed = "confirmed"
    cancelled = "cancelled"
    expired = "expired"


@dataclass(frozen=True)
class Resource:
    id: str
    name: str
    kind: str
    capacity: int


@dataclass(frozen=True)
class Reservation:
    id: str
    resource_id: str
    client_id: str
    start: datetime
    end: datetime
    status: ReservationStatus
    created_at: datetime
    expires_at: Optional[datetime]  # only meaningful while status == held
    confirmed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None

    def is_expired_hold(self, now: datetime) -> bool:
        return (
            self.status == ReservationStatus.held
            and self.expires_at is not None
            and self.expires_at <= now
        )

    def occupies_slot(self, now: datetime) -> bool:
        """
        Does this reservation count against capacity right now?

        Confirmed reservations always do. Holds only do while their TTL has not
        elapsed -- this is the *real-time* half of expiry: even if the sweeper
        hasn't run yet, a stale hold never blocks a new booking.
        """
        if self.status == ReservationStatus.confirmed:
            return True
        if self.status == ReservationStatus.held:
            return not self.is_expired_hold(now)
        return False

    def overlaps(self, start: datetime, end: datetime) -> bool:
        # Half-open intervals [a, b) and [c, d) overlap iff a < d and c < b.
        # Touching windows (b == c) therefore do NOT collide.
        return self.start < end and start < self.end
