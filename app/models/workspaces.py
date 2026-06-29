from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.models.common import ChangedFile, GatewayBaseModel, IdempotentRequest
from app.workspace.ids import WORKSPACE_ID_PATTERN


class PrepareWorkspaceBaseRequest(IdempotentRequest):
    mode: Literal["prepare_ref", "create_or_prepare_branch"] = Field(
        default="prepare_ref",
        description=(
            "Workspace preparation mode. 'prepare_ref' preserves the existing behavior. "
            "'create_or_prepare_branch' creates or continues a writable branch before preparing it."
        ),
    )
    branch: str | None = Field(default=None, description="Branch to prepare for read/write maintenance.")
    source_pr_number: int | None = Field(default=None, ge=1, description="Prepare from this PR head branch.")
    base_ref: str | None = Field(default=None, description="Read-only base branch/ref for investigation.")
    base_sha: str | None = Field(default=None, min_length=7, description="Exact commit SHA to branch from when mode is create_or_prepare_branch.")
    purpose_slug: str = Field(default="task", min_length=1, max_length=80, description="Branch-name slug used when create_or_prepare_branch needs to auto-generate a branch name.")
    continue_if_exists: bool = Field(default=True, description="Continue with an existing branch when create_or_prepare_branch finds one.")
    workspace_id: str | None = Field(default=None, min_length=3, max_length=80, pattern=WORKSPACE_ID_PATTERN)

    @model_validator(mode="after")
    def validate_prepare_target(self) -> PrepareWorkspaceBaseRequest:
        if self.base_sha and self.base_ref:
            raise ValueError("base_sha and base_ref are mutually exclusive")
        if self.mode == "create_or_prepare_branch":
            if self.source_pr_number is not None:
                raise ValueError("source_pr_number is not valid with create_or_prepare_branch")
            return self

        selected = [self.branch is not None, self.source_pr_number is not None, self.base_ref is not None]
        if sum(selected) != 1:
            raise ValueError("Provide exactly one of branch, source_pr_number, or base_ref unless mode is create_or_prepare_branch")
        if self.base_sha is not None:
            raise ValueError("base_sha is only valid with create_or_prepare_branch")
        return self


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
    branch_created: bool | None = None
    branch_continued: bool | None = None
    branch_already_exists: bool | None = None
    branch_base_ref: str | None = None
    branch_base_sha: str | None = None
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


class WorkspaceTreeEntry(GatewayBaseModel):
    path: str
    type: Literal["file", "dir"]
    depth: int
    bytes: int | None = None


class WorkspaceFileContent(GatewayBaseModel):
    path: str
    start_line: int
    end_line: int | None = None
    total_lines: int | None = None
    bytes: int | None = None
    sha256: str | None = None
    content: str = ""
    truncated: bool = False
    error: str | None = None


class WorkspaceReadFilesRequest(GatewayBaseModel):
    paths: list[str] = Field(min_length=1, max_length=50)
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, ge=1, le=5000)
    max_bytes_per_file: int | None = Field(default=None, ge=1)


class WorkspaceReadFilesResponse(GatewayBaseModel):
    workspace_id: str
    files: list[WorkspaceFileContent]
    truncated: bool = False


class WorkspaceSearchMatch(GatewayBaseModel):
    path: str
    line_number: int
    column: int | None = None
    line: str
    snippet: str | None = None


class WorkspaceSearchRequest(GatewayBaseModel):
    query: str = Field(min_length=1, max_length=500)
    regex: bool = False
    case_sensitive: bool = False
    paths: list[str] = Field(default_factory=lambda: ["."], min_length=1, max_length=50)
    context_lines: int = Field(default=2, ge=0, le=20)
    max_matches: int = Field(default=100, ge=1, le=1000)
    max_bytes: int | None = Field(default=None, ge=1024, description="Maximum serialized search response size in bytes.")


class WorkspaceSearchResponse(GatewayBaseModel):
    workspace_id: str
    query: str
    engine: Literal["ripgrep"]
    matches: list[WorkspaceSearchMatch]
    match_count: int
    truncated: bool = False


class WorkspaceInspectRequest(GatewayBaseModel):
    paths: list[str] = Field(default_factory=lambda: ["."], min_length=1, max_length=50)
    queries: list[str] = Field(default_factory=list, max_length=10)
    max_depth: int = Field(default=2, ge=1, le=10)
    max_tree_entries: int = Field(default=200, ge=1, le=5000)
    context_lines: int = Field(default=2, ge=0, le=20)
    max_search_matches: int = Field(default=50, ge=1, le=1000)
    max_read_files: int = Field(default=10, ge=0, le=50)
    max_file_lines: int = Field(default=120, ge=1, le=5000)
    max_bytes_per_file: int | None = Field(default=None, ge=1)
    max_bytes: int | None = Field(default=None, ge=1024, description="Maximum serialized inspect response size in bytes.")


class WorkspaceInspectSearchResult(GatewayBaseModel):
    query: str
    engine: Literal["ripgrep"]
    matches: list[WorkspaceSearchMatch]
    match_count: int
    truncated: bool = False


class WorkspaceInspectResponse(GatewayBaseModel):
    workspace_id: str
    tree: list[WorkspaceTreeEntry]
    tree_truncated: bool = False
    searches: list[WorkspaceInspectSearchResult] = Field(default_factory=list)
    files: list[WorkspaceFileContent] = Field(default_factory=list)
    truncated: bool = False


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


