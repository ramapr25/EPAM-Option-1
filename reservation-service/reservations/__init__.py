"""
Resource Reservation Service (prototype).

Clients reserve a limited shared resource (rooms, seats, equipment, charging
slots) for a half-open time window [start, end). A reservation starts life as
a *hold* that must be confirmed within HOLD_TTL_SECONDS, otherwise it expires.

Modules:
    config   tunables, clock, UTC helpers
    models   domain records and overlap rules
    store    Store: state, locking, conflict / idempotency / expiry logic
    sweeper  background thread that expires stale holds
    schemas  pydantic request/response models
    api      FastAPI routes and create_app()
"""

from .api import create_app
from .models import Reservation, ReservationStatus, Resource
from .store import Conflict, NotFound, Store

__all__ = [
    "create_app",
    "Conflict",
    "NotFound",
    "Reservation",
    "ReservationStatus",
    "Resource",
    "Store",
]
