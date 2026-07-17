from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    AUTH_FAILED = "AUTH_FAILED"
    REPO_NOT_ALLOWED = "REPO_NOT_ALLOWED"
    BRANCH_NOT_ALLOWED = "BRANCH_NOT_ALLOWED"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOO_MANY_FILES = "TOO_MANY_FILES"
    BINARY_FILE_NOT_ALLOWED = "BINARY_FILE_NOT_ALLOWED"
    DELETE_NOT_ALLOWED = "DELETE_NOT_ALLOWED"
    WORKFLOW_EDIT_NOT_ALLOWED = "WORKFLOW_EDIT_NOT_ALLOWED"
    BRANCH_HEAD_CHANGED = "BRANCH_HEAD_CHANGED"
    PR_ALREADY_EXISTS = "PR_ALREADY_EXISTS"
    CI_RUN_NOT_FOUND = "CI_RUN_NOT_FOUND"
    CI_LOG_NOT_READY = "CI_LOG_NOT_READY"
    GITHUB_RATE_LIMITED = "GITHUB_RATE_LIMITED"
    GITHUB_AUTH_FAILED = "GITHUB_AUTH_FAILED"
    GITHUB_NOT_FOUND = "GITHUB_NOT_FOUND"
    GITHUB_CONFLICT = "GITHUB_CONFLICT"
    GITHUB_ERROR = "GITHUB_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"

    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    WORKSPACE_BUSY = "WORKSPACE_BUSY"
    WORKSPACE_DIRTY = "WORKSPACE_DIRTY"
    WORKSPACE_HEAD_CHANGED = "WORKSPACE_HEAD_CHANGED"
    WORKSPACE_EXEC_FAILED = "WORKSPACE_EXEC_FAILED"
    WORKSPACE_TIMEOUT = "WORKSPACE_TIMEOUT"
    WORKSPACE_POLICY_VIOLATION = "WORKSPACE_POLICY_VIOLATION"
    WORKSPACE_STORAGE_LIMIT = "WORKSPACE_STORAGE_LIMIT"
    WORKSPACE_SCRIPT_REJECTED = "WORKSPACE_SCRIPT_REJECTED"
    WORKSPACE_NO_CHANGES = "WORKSPACE_NO_CHANGES"
    WORKSPACE_PUSH_FAILED = "WORKSPACE_PUSH_FAILED"
    WORKSPACE_PATCH_INVALID = "WORKSPACE_PATCH_INVALID"
    WORKSPACE_PATCH_CONTEXT_MISMATCH = "WORKSPACE_PATCH_CONTEXT_MISMATCH"
    WORKSPACE_PATCH_TOO_LARGE = "WORKSPACE_PATCH_TOO_LARGE"
    WORKSPACE_TOO_MANY_CHANGED_FILES = "WORKSPACE_TOO_MANY_CHANGED_FILES"
    WORKSPACE_DELETE_NOT_ALLOWED = "WORKSPACE_DELETE_NOT_ALLOWED"
    WORKSPACE_BINARY_NOT_ALLOWED = "WORKSPACE_BINARY_NOT_ALLOWED"
    WORKSPACE_WRITE_INVALID_PATH = "WORKSPACE_WRITE_INVALID_PATH"
    WORKSPACE_FILE_EXISTS = "WORKSPACE_FILE_EXISTS"
    WORKSPACE_FILE_NOT_FOUND = "WORKSPACE_FILE_NOT_FOUND"
    WORKSPACE_SHA_MISMATCH = "WORKSPACE_SHA_MISMATCH"
    WORKSPACE_CONTENT_TOO_LARGE = "WORKSPACE_CONTENT_TOO_LARGE"
    WORKSPACE_WRITE_FAILED = "WORKSPACE_WRITE_FAILED"


class ErrorResponse(BaseModel):
    error_code: ErrorCode | str = Field(..., examples=["WORKSPACE_HEAD_CHANGED"])
    message: str = Field(..., examples=["Remote branch head changed before commit."])
    suggestion: str | None = Field(default=None, examples=["Refresh the workspace and retry with the latest expected_head_sha."])
    details: dict[str, Any] = Field(default_factory=dict)


class ApiError(Exception):
    def __init__(
        self,
        error_code: ErrorCode | str,
        message: str,
        *,
        status_code: int = 400,
        suggestion: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = str(error_code)
        self.message = message
        self.status_code = status_code
        self.suggestion = suggestion
        self.details = details or {}

    def as_response(self) -> ErrorResponse:
        return ErrorResponse(error_code=self.error_code, message=self.message, suggestion=self.suggestion, details=self.details)


def _json_safe(value: Any) -> Any:
    """Convert nested error details into values accepted by JSONResponse.

    Pydantic validation errors may include the original exception in
    ``ctx.error``.  Returning ``exc.errors()`` directly therefore causes a
    second failure while serializing the error response, hiding the useful
    validation message from the caller.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump())
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Exception):
        return str(value)
    return str(value)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.as_response().model_dump())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        response = ErrorResponse(
            error_code=ErrorCode.VALIDATION_ERROR,
            message="Request validation failed.",
            suggestion="Check required fields, field types, and allowed enum values.",
            details={"errors": _json_safe(exc.errors())},
        )
        return JSONResponse(status_code=422, content=response.model_dump())

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        response = ErrorResponse(
            error_code=ErrorCode.GITHUB_ERROR,
            message="Unhandled server exception.",
            suggestion="Check server logs and retry the request.",
            details={"exception_type": type(exc).__name__, "error": str(exc)},
        )
        return JSONResponse(status_code=500, content=response.model_dump())
