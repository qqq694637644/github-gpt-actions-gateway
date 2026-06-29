from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.models.common import ChangedFile, GatewayBaseModel, IdempotentRequest
from app.workspace.ids import WORKSPACE_ID_PATTERN


class PrepareWorkspaceBaseRequest(IdempotentRequest):
    branch: str | None = Field(default=None, description="Branch to prepare for read/write maintenance.")
    source_pr_number: int | None = Field(default=None, ge=1, description="Prepare from this PR head branch.")
    base_ref: str | None = Field(default=None, description="Read-only base branch/ref for investigation.")
    workspace_id: str | None = Field(default=None, min_length=3, max_length=80, pattern=WORKSPACE_ID_PATTERN)


class PrepareWorkspaceRequest(PrepareWorkspaceBaseRequest):
    refresh: bool = True
    clean: bool = False




class WorkspacePrepareDiagnostics(GatewayBaseModel):
    mirror_stage: Literal["clone", "fetch", "reuse", "skip"]
    mirror_duration_ms: int
    mirror_pack_bytes: int
    mirror_pack_files: int
    workspace_stage: Literal["clone", "reuse", "skip"]
    workspace_duration_ms: int
    total_duration_ms: int


class PrepareWorkspaceResponse(GatewayBaseModel):
    workspace_id: str
    owner: str
    repo: str
    branch: str
    source_pr_number: int | None = None
    head_sha: str
    default_branch: str
    created: bool
    refreshed: bool
    diagnostics: WorkspacePrepareDiagnostics



class WorkspaceExecPwshRequest(GatewayBaseModel):
    script: str = Field(min_length=1, max_length=20000)
    timeout_seconds: int | None = Field(default=None, ge=1)
    max_output_bytes: int | None = Field(default=None, ge=1)
    allow_network: bool = False
    plain_output: bool = Field(default=False, description="Opt in to plain assistant-facing output by setting PSStyle and stripping ANSI escapes.")
    utf8_output: bool = Field(default=True, description="Use UTF-8 PowerShell console/output defaults before running the script.")


class WorkspaceExecPwshResponse(GatewayBaseModel):
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    duration_ms: int


class WorkspaceStatusRequest(GatewayBaseModel):
    refresh: bool = False


class WorkspaceStatusResponse(GatewayBaseModel):
    workspace_id: str
    branch: str
    head_sha: str
    remote_head_sha: str | None = None
    dirty: bool
    ahead: int = 0
    behind: int = 0
    changed_files: list[ChangedFile]
    untracked_files: list[str]
    conflicts: list[str]


class WorkspaceDiffRequest(GatewayBaseModel):
    paths: list[str] = Field(default_factory=lambda: ["."], min_length=1, max_length=50)
    stat_only: bool = False
    max_bytes: int | None = Field(default=None, ge=1)


class WorkspaceDiffResponse(GatewayBaseModel):
    workspace_id: str
    diff: str
    diff_stat: str
    truncated: bool


class WorkspaceApplyPatchRequest(GatewayBaseModel):
    patch: str = Field(min_length=1)
    dry_run: bool = False
    allow_delete: bool = False
    max_changed_files: int | None = Field(default=None, ge=1)
    max_patch_bytes: int | None = Field(default=None, ge=1)


class WorkspaceApplyPatchResponse(GatewayBaseModel):
    applied: bool
    dry_run: bool
    changed_files: list[ChangedFile]
    diff_stat: str


class WorkspaceWriteFileRequest(GatewayBaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str
    mode: Literal["create_only", "overwrite", "overwrite_if_sha256_matches"] = "create_only"
    encoding: Literal["utf-8"] = "utf-8"
    line_ending: Literal["preserve", "lf", "crlf"] = "preserve"
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    dry_run: bool = False
    max_bytes: int | None = Field(default=None, ge=1)


class WorkspaceWriteFileResponse(GatewayBaseModel):
    written: bool
    dry_run: bool
    path: str
    operation: Literal["added", "modified", "unchanged"] | str
    previous_sha256: str | None
    new_sha256: str
    bytes: int
    changed_files: list[ChangedFile]
    diff_stat: str


class WorkspaceCommitAndPushRequest(IdempotentRequest):
    branch: str
    expected_head_sha: str = Field(min_length=7)
    commit_message: str = Field(min_length=1, max_length=300)
    paths: list[str] = Field(default_factory=lambda: ["."], min_length=1, max_length=50)
    dry_run: bool = False


class WorkspaceCommitAndPushResponse(GatewayBaseModel):
    previous_head_sha: str
    new_head_sha: str
    commit_sha: str | None = None
    commit_url: str | None = None
    changed_files: list[ChangedFile]
    diff_stat: str
    pushed: bool
    dry_run: bool


class WorkspaceResetRequest(GatewayBaseModel):
    branch: str
    target: Literal["remote_head"] = "remote_head"
    clean_untracked: bool = True


class WorkspaceResetResponse(GatewayBaseModel):
    workspace_id: str
    branch: str
    head_sha: str
    removed_untracked_files: list[str]
