"""Response models of the HTTP API."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IngestOut(BaseModel):
    session_id: uuid.UUID
    records: int
    truncated: int
    replaced: bool


class MatchView(BaseModel):
    record_id: int
    kind: str
    ts: datetime | None
    snippet: str


class SessionSummary(BaseModel):
    id: int
    session_id: uuid.UUID
    slug: str
    preset_id: uuid.UUID | None
    preset_name: str | None
    created_by_service: str | None
    created_by_session: uuid.UUID | None
    created_at: datetime
    discarded_at: datetime
    record_counts: dict[str, int]
    bytes: int
    hits: int | None
    matches: list[MatchView]


class AgentView(BaseModel):
    id: int
    ordinal: int
    pod_name: str | None


class SessionDetail(SessionSummary):
    session: dict[str, Any]
    agents: list[AgentView]


class RecordView(BaseModel):
    id: int
    kind: str
    ts: datetime | None
    truncated: bool
    body: Any


class RecordsPage(BaseModel):
    records: list[RecordView]
    has_more_before: bool
    next_after_id: int | None


class RetentionView(BaseModel):
    retention_days: int
    updated_at: datetime
    last_sweep_at: datetime | None
    last_sweep_deleted: int | None


class RetentionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retention_days: int = Field(ge=1, le=3650)
