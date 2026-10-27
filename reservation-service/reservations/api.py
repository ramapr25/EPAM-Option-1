"""HTTP layer: routes, Store exceptions -> status codes, and the app factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response, status

from . import config
from .models import ReservationStatus
from .schemas import ReservationCreate, ReservationOut, ResourceCreate, ResourceOut
from .store import Conflict, NotFound, Store
from .sweeper import ExpirySweeper

# NOTE: handlers are plain `def`, so FastAPI runs them in a threadpool. That is
# what makes the per-resource threading.Lock meaningful (and avoids blocking
# the event loop while waiting on it).

router = APIRouter()


def get_store(request: Request) -> Store:
    return request.app.state.store


StoreDep = Annotated[Store, Depends(get_store)]


@router.post("/resources", response_model=ResourceOut, status_code=status.HTTP_201_CREATED)
def create_resource(body: ResourceCreate, store: StoreDep):
    return store.create_resource(body.name, body.kind, body.capacity)


@router.get("/resources", response_model=list[ResourceOut])
def list_resources(store: StoreDep):
    return store.list_resources()


@router.post("/reservations", response_model=ReservationOut, status_code=status.HTTP_201_CREATED)
def create_reservation(
    body: ReservationCreate,
    response: Response,
    store: StoreDep,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    try:
        r, created = store.create_hold(
            body.resource_id, body.client_id, body.start, body.end, idempotency_key
        )
    except NotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    except Conflict as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    if not created:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    return r


@router.get("/reservations", response_model=list[ReservationOut])
def list_reservations(
    store: StoreDep,
    resource_id: Optional[str] = Query(default=None),
    client_id: Optional[str] = Query(default=None),
    status_: Optional[ReservationStatus] = Query(default=None, alias="status"),
):
    return store.list_reservations(resource_id, client_id, status_)


@router.get("/reservations/{reservation_id}", response_model=ReservationOut)
def get_reservation(reservation_id: str, store: StoreDep):
    try:
        return store.get_reservation(reservation_id)
    except NotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


@router.post("/reservations/{reservation_id}/confirm", response_model=ReservationOut)
def confirm_reservation(reservation_id: str, store: StoreDep):
    try:
        return store.confirm(reservation_id)
    except NotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    except Conflict as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))


@router.delete("/reservations/{reservation_id}", response_model=ReservationOut)
def cancel_reservation(reservation_id: str, store: StoreDep):
    try:
        return store.cancel(reservation_id)
    except NotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


def create_app(store: Optional[Store] = None) -> FastAPI:
    """Build the app around a Store (a fresh in-memory one by default)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sweeper = ExpirySweeper(app.state.store, config.SWEEP_INTERVAL_SECONDS)
        sweeper.start()
        try:
            yield
        finally:
            sweeper.stop()

    app = FastAPI(title="Resource Reservation Service", version="0.1.0", lifespan=lifespan)
    app.state.store = store if store is not None else Store()
    app.include_router(router)
    return app
