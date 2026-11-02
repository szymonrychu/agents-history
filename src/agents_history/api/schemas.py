"""Response models of the HTTP API."""

import uuid

from pydantic import BaseModel


class IngestOut(BaseModel):
    session_id: uuid.UUID
    records: int
    truncated: int
    replaced: bool
