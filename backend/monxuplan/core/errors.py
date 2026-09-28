"""Domain errors with stable codes and user-facing messages.

Technical details (stack traces) are logged with an error id; users see a clear message, the code and
— where possible — a link to the problem (``context``).
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, code: str | None = None, context: dict[str, Any] | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.context = context or {}


class NotFound(DomainError):
    status_code = 404
    code = "NOT_FOUND"


class Forbidden(DomainError):
    status_code = 403
    code = "FORBIDDEN"


class Unauthorized(DomainError):
    status_code = 401
    code = "UNAUTHORIZED"


class Conflict(DomainError):
    status_code = 409
    code = "CONFLICT"


class ValidationFailed(DomainError):
    status_code = 422
    code = "VALIDATION_FAILED"


class PlanningBlocked(DomainError):
    status_code = 409
    code = "PLANNING_BLOCKED"


class RateLimited(DomainError):
    status_code = 429
    code = "RATE_LIMITED"
