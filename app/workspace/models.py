from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.errors import ApiError, ErrorCode


class WorkspaceMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    owner: str
    repo: str
    branch: str
    default_branch: str
    head_sha: str
    source_pr_number: int | None = None
    writable: bool
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def meta_path(workspace_dir: Path) -> Path:
    return workspace_dir / "meta.json"


def load_meta(workspace_dir: Path) -> WorkspaceMeta:
    path = meta_path(workspace_dir)
    try:
        return WorkspaceMeta.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        missing_fields = [".".join(str(part) for part in error.get("loc", ())) for error in exc.errors() if error.get("type") == "missing"]
        if "writable" in missing_fields:
            raise ApiError(
                ErrorCode.WORKSPACE_POLICY_VIOLATION,
                "Workspace metadata is missing required field 'writable'; recreate the workspace.",
                status_code=409,
                details={"workspace_dir": str(workspace_dir), "missing_fields": missing_fields},
            ) from exc
        raise ApiError(
            ErrorCode.WORKSPACE_POLICY_VIOLATION,
            "Workspace metadata is invalid; recreate the workspace.",
            status_code=409,
            details={"workspace_dir": str(workspace_dir), "validation_errors": exc.errors()},
        ) from exc


def save_meta(workspace_dir: Path, meta: WorkspaceMeta) -> None:
    meta.updated_at = datetime.now(UTC).isoformat()
    meta_path(workspace_dir).write_text(meta.model_dump_json(indent=2), encoding="utf-8")


class CommandResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    timed_out: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(slots=True)
class WorkspacePrepareStats:
    meta: WorkspaceMeta
    created: bool
    workspace_stage: str
    workspace_duration_ms: int
    total_duration_ms: int
