"""Deterministic OpenAPI export per listener.

`python -m agents_history.api.openapi > openapi.json` documents the UI listener (read, delete and
settings routes); `--service` documents the service listener (the three GET read routes only:
the superleader never deletes). The ingest listener has one route, documented in the README.
FastAPI documents a validation error as its own `HTTPValidationError`; this service answers every
error with `{"error": {"code", "message"}}` (see `errors.py`), so the export replaces the
generated error responses with that shape and the statuses each route really returns. The probes
(`/healthz`, `/readyz`) are not part of the documents.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from agents_history.api.app import PREFIX, create_app, create_service_app
from agents_history.settings import Settings

ERROR_REF = {"$ref": "#/components/schemas/ErrorResponse"}

ERROR_SCHEMAS: dict[str, Any] = {
    "ErrorBody": {
        "type": "object",
        "title": "ErrorBody",
        "required": ["code", "message"],
        "properties": {
            "code": {"type": "string", "title": "Code"},
            "message": {"type": "string", "title": "Message"},
        },
    },
    "ErrorResponse": {
        "type": "object",
        "title": "ErrorResponse",
        "required": ["error"],
        "properties": {"error": {"$ref": "#/components/schemas/ErrorBody"}},
    },
}

UI_ERRORS = {403: "source address is not allowed on the UI listener (forbidden)"}
SERVICE_ERRORS = {401: "no or an unknown bearer service token (unauthorized)"}
ROUTE_ERRORS = {
    400: "before_id and after_id combined (bad_request)",
    404: "no such session (not_found)",
    422: "invalid request (invalid_request, query_too_short, invalid_regex, search_timeout)",
}

ROUTE_STATUSES: dict[tuple[str, str], tuple[int, ...]] = {
    ("get", "/sessions"): (422,),
    ("get", "/sessions/{session_id}"): (404, 422),
    ("get", "/sessions/{session_id}/records"): (400, 404, 422),
    ("delete", "/sessions/{session_id}"): (404, 422),
    ("get", "/settings"): (),
    ("put", "/settings"): (422,),
}


def document(service: bool = False) -> dict[str, Any]:
    """The OpenAPI document of the UI listener, or of the service listener with `service`."""
    settings = Settings(database_url="postgresql://openapi/export")
    if service:
        app = create_service_app(settings, pool=None, tokens=["openapi-export"])  # type: ignore[arg-type]
        guard = SERVICE_ERRORS
    else:
        app = create_app(settings, pool=None)  # type: ignore[arg-type]
        guard = UI_ERRORS
    errors = {**guard, **ROUTE_ERRORS}
    doc = app.openapi()
    doc["paths"] = {p: m for p, m in doc["paths"].items() if p.startswith(PREFIX)}
    for path, methods in doc["paths"].items():
        for method, operation in methods.items():
            responses = operation["responses"]
            for status in [s for s in responses if not s.startswith("2")]:
                del responses[status]
            statuses = (*guard, *ROUTE_STATUSES[(method, path.removeprefix(PREFIX))])
            for status in sorted(statuses):
                responses[str(status)] = {
                    "description": errors[status],
                    "content": {"application/json": {"schema": ERROR_REF}},
                }
    schemas = doc["components"]["schemas"]
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)
    schemas.update(ERROR_SCHEMAS)
    return doc


def export(service: bool = False) -> str:
    """The listener's OpenAPI document as JSON with sorted keys and a trailing newline."""
    return json.dumps(document(service), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(export(service="--service" in sys.argv[1:]), end="")
