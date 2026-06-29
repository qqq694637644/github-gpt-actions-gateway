from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class GatewayBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class IdempotentRequest(GatewayBaseModel):
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)


class ChangedFile(GatewayBaseModel):
    path: str
    operation: Literal["added", "modified", "deleted", "renamed", "untracked", "conflicted"] | str
    status: str | None = None
    previous_path: str | None = None
    additions: int = 0
    deletions: int = 0


class ErrorExample(BaseModel):
    error_code: str = "WORKSPACE_HEAD_CHANGED"
    message: str = "Remote branch head changed before commit."
    suggestion: str | None = "Refresh the workspace and retry with the latest expected_head_sha."
    details: dict[str, Any] = {"expected_head_sha": "abc", "actual_head_sha": "def"}
