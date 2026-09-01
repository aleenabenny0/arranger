"""API error helpers."""

from __future__ import annotations

from fastapi import HTTPException

from arranger.render import RenderError


def bad_request(error: str, detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": error, "detail": detail})


def not_found(resource: str, record_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": "not_found", "detail": f"{resource} '{record_id}' was not found"},
    )


def domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, RenderError):
        return bad_request("invalid_plan", str(exc))
    if isinstance(exc, ValueError):
        return bad_request("invalid_input", str(exc))
    return HTTPException(
        status_code=500,
        detail={"error": "internal_error", "detail": str(exc)},
    )
