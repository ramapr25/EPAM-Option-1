"""Pydantic request/response models: input validation and UTC serialisation."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import to_utc
from .models import ReservationStatus


class ResourceCreate(BaseModel):
    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    capacity: int = Field(default=1, ge=1)


class ResourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    kind: str
    capacity: int


class ReservationCreate(BaseModel):
    resource_id: str
    client_id: str = Field(min_length=1)
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return to_utc(v)

    @model_validator(mode="after")
    def _window(self) -> "ReservationCreate":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class ReservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    resource_id: str
    client_id: str
    start: datetime
    end: datetime
    status: ReservationStatus
    created_at: datetime
    expires_at: Optional[datetime]
    confirmed_at: Optional[datetime]
    cancelled_at: Optional[datetime]
