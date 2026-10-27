"""
Store: in-memory, thread-safe. Swap for a DB-backed implementation later;
the API layer only depends on these public methods.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Optional

from . import config
from .models import Reservation, ReservationStatus, Resource


# Errors raised by the store (mapped to HTTP codes in the API layer).


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Store:
    def __init__(self) -> None:
        self._resources: dict[str, Resource] = {}
        self._reservations: dict[str, Reservation] = {}
        # (client_id, idempotency_key) -> reservation_id
        self._idempotency: dict[tuple[str, str], str] = {}

        # Short-lived lock protecting the dicts themselves (and lock creation).
        self._meta_lock = threading.Lock()
        # One lock per resource: serialises "check availability + insert" so two
        # concurrent requests can't both observe a free slot and both win it.
        # Requests for *different* resources still proceed in parallel.
        self._resource_locks: dict[str, threading.Lock] = {}
        # One lock per (client_id, idempotency_key): serialises concurrent
        # retries carrying the same key so exactly one of them creates.
        self._idem_locks: dict[tuple[str, str], threading.Lock] = {}

    # -- lock helpers ------------------------------------------------------- #

    def _resource_lock(self, resource_id: str) -> threading.Lock:
        with self._meta_lock:
            return self._resource_locks.setdefault(resource_id, threading.Lock())

    def _idem_lock(self, scope: tuple[str, str]) -> threading.Lock:
        with self._meta_lock:
            return self._idem_locks.setdefault(scope, threading.Lock())

    # -- lazy expiry -------------------------------------------------------- #

    def _materialize_expiry(self, r: Reservation, now: datetime) -> Reservation:
        """If r is a hold past its TTL, persist it as expired and return that."""
        if not r.is_expired_hold(now):
            return r
        with self._meta_lock:
            # Compare-and-set: only flip if the stored copy is still that stale
            # hold, so we never overwrite a confirm/cancel that won a race.
            cur = self._reservations.get(r.id)
            if cur is not None and cur.is_expired_hold(now):
                cur = replace(cur, status=ReservationStatus.expired)
                self._reservations[cur.id] = cur
            return cur if cur is not None else r

    # -- resources ---------------------------------------------------------- #

    def create_resource(self, name: str, kind: str, capacity: int) -> Resource:
        res = Resource(id=str(uuid.uuid4()), name=name, kind=kind, capacity=capacity)
        with self._meta_lock:
            self._resources[res.id] = res
        return res

    def list_resources(self) -> list[Resource]:
        with self._meta_lock:
            return list(self._resources.values())

    def get_resource(self, resource_id: str) -> Resource:
        with self._meta_lock:
            res = self._resources.get(resource_id)
        if res is None:
            raise NotFound(f"resource {resource_id} not found")
        return res

    # -- reservations ------------------------------------------------------- #

    def create_hold(
        self,
        resource_id: str,
        client_id: str,
        start: datetime,
        end: datetime,
        idempotency_key: Optional[str] = None,
    ) -> tuple[Reservation, bool]:
        """
        Create a held reservation. Returns (reservation, created) where
        created=False means an idempotent replay of an earlier request.

        Lock order is always idempotency-key lock -> resource lock, so there is
        no deadlock between the two.
        """
        if idempotency_key is None:
            return self._create_hold_locked(resource_id, client_id, start, end, None), True

        # IDEMPOTENCY: keys are scoped by client_id, so two clients that happen
        # to pick the same key never see each other's reservations.
        scope = (client_id, idempotency_key)
        with self._idem_lock(scope):
            with self._meta_lock:
                existing_id = self._idempotency.get(scope)
            if existing_id is not None:
                # Replay: return the original reservation (in its current state)
                # rather than creating a second one. We don't compare bodies in
                # the prototype -- see README for production behaviour.
                return self.get_reservation(existing_id), False

            # Only successful creates are recorded. If this attempt fails with
            # 404/409, a retry with the same key is evaluated afresh.
            r = self._create_hold_locked(resource_id, client_id, start, end, scope)
            return r, True

    def _create_hold_locked(
        self,
        resource_id: str,
        client_id: str,
        start: datetime,
        end: datetime,
        idem_scope: Optional[tuple[str, str]],
    ) -> Reservation:
        resource = self.get_resource(resource_id)

        with self._resource_lock(resource_id):
            now = config.utcnow()

            # CONFLICT DETECTION
            # Collect the reservations on this resource that (a) still occupy a
            # slot -- confirmed, or held and not yet past TTL -- and (b) overlap
            # the requested window. Expired holds are skipped here even if the
            # sweeper hasn't flipped them yet, so a slot reopens immediately.
            with self._meta_lock:
                candidates = [
                    r
                    for r in self._reservations.values()
                    if r.resource_id == resource_id
                    and r.occupies_slot(now)
                    and r.overlaps(start, end)
                ]

            # With capacity > 1, "number of overlapping bookings" over-counts:
            # e.g. [9,10) and [11,12) both overlap [9,12) but never coexist.
            # What matters is the peak concurrency inside the requested window.
            if _peak_concurrency(candidates, start, end) + 1 > resource.capacity:
                raise Conflict(
                    f"resource {resource_id} is fully booked for "
                    f"[{start.isoformat()}, {end.isoformat()})"
                )

            r = Reservation(
                id=str(uuid.uuid4()),
                resource_id=resource_id,
                client_id=client_id,
                start=start,
                end=end,
                status=ReservationStatus.held,
                created_at=now,
                # Looked up at call time so tests can monkeypatch the TTL.
                expires_at=now + timedelta(seconds=config.HOLD_TTL_SECONDS),
            )
            with self._meta_lock:
                self._reservations[r.id] = r
                if idem_scope is not None:
                    self._idempotency[idem_scope] = r.id
            return r

    def get_reservation(self, reservation_id: str) -> Reservation:
        with self._meta_lock:
            r = self._reservations.get(reservation_id)
        if r is None:
            raise NotFound(f"reservation {reservation_id} not found")
        return self._materialize_expiry(r, config.utcnow())

    def list_reservations(
        self,
        resource_id: Optional[str] = None,
        client_id: Optional[str] = None,
        status_: Optional[ReservationStatus] = None,
    ) -> list[Reservation]:
        now = config.utcnow()
        with self._meta_lock:
            items = list(self._reservations.values())
        # Materialise expiry first so status filtering is accurate in real time.
        items = [self._materialize_expiry(r, now) for r in items]
        return sorted(
            (
                r
                for r in items
                if (resource_id is None or r.resource_id == resource_id)
                and (client_id is None or r.client_id == client_id)
                and (status_ is None or r.status == status_)
            ),
            key=lambda r: (r.start, r.created_at),
        )

    def confirm(self, reservation_id: str) -> Reservation:
        r = self.get_reservation(reservation_id)
        # Take the resource lock so a confirm can't race with the expiry check
        # or a concurrent cancel on the same resource.
        with self._resource_lock(r.resource_id):
            r = self.get_reservation(reservation_id)  # re-read under lock
            if r.status == ReservationStatus.confirmed:
                return r  # confirming twice is harmless
            if r.status != ReservationStatus.held:
                # Includes holds whose TTL has elapsed (materialised as expired
                # by get_reservation), so confirming a stale hold is a 409.
                raise Conflict(f"cannot confirm reservation in status '{r.status.value}'")
            r = replace(
                r,
                status=ReservationStatus.confirmed,
                confirmed_at=config.utcnow(),
                expires_at=None,
            )
            with self._meta_lock:
                self._reservations[r.id] = r
            return r

    def cancel(self, reservation_id: str) -> Reservation:
        r = self.get_reservation(reservation_id)
        with self._resource_lock(r.resource_id):
            r = self.get_reservation(reservation_id)
            # CANCELLATION is idempotent: cancelling a reservation that is
            # already cancelled (or already expired -- it holds no slot) is a
            # no-op that returns the reservation unchanged.
            if r.status in (ReservationStatus.cancelled, ReservationStatus.expired):
                return r
            r = replace(
                r,
                status=ReservationStatus.cancelled,
                cancelled_at=config.utcnow(),
                expires_at=None,
            )
            with self._meta_lock:
                self._reservations[r.id] = r
            return r

    def sweep_expired(self) -> int:
        """
        Background half of expiry: flip every stale hold to 'expired'.
        Returns how many were flipped. Safe to call concurrently with requests.
        """
        now = config.utcnow()
        with self._meta_lock:
            stale = [r for r in self._reservations.values() if r.is_expired_hold(now)]
        count = 0
        for r in stale:
            with self._resource_lock(r.resource_id):
                with self._meta_lock:
                    cur = self._reservations.get(r.id)
                    # Re-check: it may have been confirmed/cancelled meanwhile.
                    if cur is not None and cur.is_expired_hold(now):
                        self._reservations[r.id] = replace(cur, status=ReservationStatus.expired)
                        count += 1
        return count


def _peak_concurrency(reservations: list[Reservation], start: datetime, end: datetime) -> int:
    """
    Max number of the given reservations active at any instant in [start, end).

    Sweep line over interval endpoints clipped to the window. Ends are
    processed before starts at the same instant, which is exactly half-open
    semantics: something ending at t and something starting at t never overlap.
    """
    events: list[tuple[datetime, int]] = []
    for r in reservations:
        events.append((max(r.start, start), +1))
        events.append((min(r.end, end), -1))
    events.sort(key=lambda e: (e[0], e[1]))  # -1 sorts before +1
    peak = cur = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak
