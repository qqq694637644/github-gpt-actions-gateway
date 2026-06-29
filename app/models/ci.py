from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from app.models.common import GatewayBaseModel, IdempotentRequest


class CIStep(GatewayBaseModel):
    name: str | None = None
    number: int | None = None
    status: str | None = None
    conclusion: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class FailedStep(CIStep):
    pass


class CIJob(GatewayBaseModel):
    job_id: int
    run_id: int | None = None
    name: str
    status: str | None = None
    conclusion: str | None = None
    html_url: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    runner_name: str | None = None
    steps: list[CIStep] = Field(default_factory=list)
    failed_steps: list[FailedStep] = Field(default_factory=list)


class CIRunSummary(GatewayBaseModel):
    run_id: int
    run_attempt: int | None = None
    workflow_id: int | str | None = None
    workflow_name: str | None = None
    event: str | None = None
    head_branch: str | None = None
    head_sha: str | None = None
    status: str | None = None
    conclusion: str | None = None
    run_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class CIRun(CIRunSummary):
    jobs: list[CIJob] | None = None


class CIStatusQueryRequest(GatewayBaseModel):
    commit_sha: str | None = None
    branch: str | None = None
    pr_number: int | None = Field(default=None, ge=1)
    workflow_id: str | None = None
    event: str | None = None
    created_after: str | None = None


class CIStatusResponse(GatewayBaseModel):
    matched_by: str
    status: str
    conclusion: str | None = None
    workflow_runs: list[CIRunSummary]
    warning: str | None = None


class GetCiRunRequest(GatewayBaseModel):
    run_id: int = Field(ge=1)
    include_jobs: bool = False


class GetCiRunResponse(GatewayBaseModel):
    run: CIRun


class GetCiJobsRequest(GatewayBaseModel):
    run_id: int = Field(ge=1)
    run_attempt: int | None = Field(default=None, ge=1)


class GetCiJobsResponse(GatewayBaseModel):
    run_id: int
    run_attempt: int | None = None
    jobs: list[CIJob]
    total_count: int


class DispatchWorkflowRequest(IdempotentRequest):
    workflow_id: str = Field(min_length=1, max_length=200)
    ref: str = Field(min_length=1, max_length=300)
    inputs: dict[str, Any] | None = None


class DispatchWorkflowResponse(GatewayBaseModel):
    workflow_id: str
    ref: str
    accepted: bool
    event: Literal["workflow_dispatch"] = "workflow_dispatch"
    created_after: str
    query_hint: dict[str, Any]
    warning: str | None = "GitHub workflow dispatch accepts the request but does not return the new run_id directly."


class RerunWorkflowRunRequest(IdempotentRequest):
    run_id: int = Field(ge=1)
    enable_debug_logging: bool = False


class RerunWorkflowRunResponse(GatewayBaseModel):
    run_id: int
    accepted: bool
    status: Literal["accepted", "queued"] | str = "accepted"
    query_hint: dict[str, Any]
    warning: str | None = "GitHub rerun APIs may not expose the new attempt immediately."


class RerunWorkflowJobRequest(IdempotentRequest):
    job_id: int = Field(ge=1)
    enable_debug_logging: bool = False


class RerunWorkflowJobResponse(GatewayBaseModel):
    job_id: int
    accepted: bool
    status: Literal["accepted", "queued"] | str = "accepted"
    query_hint: dict[str, Any]
    warning: str | None = "GitHub may take a few seconds to show the new job attempt."


class ActionCache(GatewayBaseModel):
    cache_id: int
    key: str | None = None
    ref: str | None = None
    version: str | None = None
    size_in_bytes: int | None = None
    created_at: str | None = None
    last_accessed_at: str | None = None


class ListCachesRequest(GatewayBaseModel):
    key: str | None = Field(default=None, max_length=512)
    ref: str | None = Field(default=None, max_length=300)
    sort: Literal["last_accessed_at", "created_at", "size_in_bytes"] = "last_accessed_at"
    direction: Literal["asc", "desc"] = "desc"
    max_results: int = Field(default=30, ge=1, le=100)


class ListCachesResponse(GatewayBaseModel):
    total_count: int
    caches: list[ActionCache]
    truncated: bool = False
    warning: str | None = None


class DeleteCacheRequest(IdempotentRequest):
    cache_id: int | None = Field(default=None, ge=1)
    key: str | None = Field(default=None, max_length=512)
    ref: str | None = Field(default=None, max_length=300)
    dry_run: bool = True
    confirm: bool = False
    expected_key: str | None = Field(default=None, max_length=512)
    expected_ref: str | None = Field(default=None, max_length=300)
    expected_size_in_bytes: int | None = Field(default=None, ge=0)
    max_delete: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def _require_one_selector(self) -> DeleteCacheRequest:
        if (self.cache_id is None) == (not self.key):
            raise ValueError("Provide exactly one of cache_id or key.")
        return self


class DeleteCacheResponse(GatewayBaseModel):
    deleted: bool
    dry_run: bool
    requested_count: int
    selected_count: int
    deleted_count: int
    requested_caches: list[ActionCache] = Field(default_factory=list)
    selected_caches: list[ActionCache] = Field(default_factory=list)
    warning: str | None = None


class FailedLogQueryRequest(GatewayBaseModel):
    run_id: int = Field(ge=1)
    run_attempt: int | None = Field(default=None, ge=1)
    job_id: int | None = Field(default=None, ge=1)
    max_lines: int | None = Field(default=None, ge=1)


class Annotation(GatewayBaseModel):
    line: int | None = None
    message: str


class FailedJobLog(GatewayBaseModel):
    job_id: int
    job_name: str
    failed_step: str | None = None
    error_summary: list[str]
    annotations: list[Annotation]
    log_excerpt: str
    last_lines: str
    truncated: bool = False


class FailedCILogResponse(GatewayBaseModel):
    run_id: int
    run_attempt: int | None = None
    failed_jobs: list[FailedJobLog]


class GetJobLogRequest(GatewayBaseModel):
    job_id: int = Field(ge=1)
    step_name: str | None = None
    max_lines: int | None = Field(default=None, ge=1)


class JobLogResponse(GatewayBaseModel):
    job_id: int
    step_name: str | None = None
    log_excerpt: str
    last_lines: str
    total_lines: int
    truncated: bool = False


class GetRunLogRequest(GatewayBaseModel):
    run_id: int = Field(ge=1)
    path_contains: str | None = None
    max_files: int = Field(default=20, ge=1, le=50)
    max_lines_per_file: int | None = Field(default=None, ge=1)


class RunLogFile(GatewayBaseModel):
    path: str
    name: str
    log_excerpt: str
    last_lines: str
    total_lines: int
    truncated: bool = False


class RunLogResponse(GatewayBaseModel):
    run_id: int
    files: list[RunLogFile]
    truncated: bool = False


class Artifact(GatewayBaseModel):
    artifact_id: int
    name: str
    size_in_bytes: int | None = None
    archive_download_url: str | None = None
    digest: str | None = None
    expired: bool | None = None
    created_at: str | None = None
    expires_at: str | None = None
    updated_at: str | None = None


class ListArtifactsRequest(GatewayBaseModel):
    run_id: int = Field(ge=1)
    max_results: int = Field(default=100, ge=1, le=100)


class ListArtifactsResponse(GatewayBaseModel):
    run_id: int
    artifacts: list[Artifact]
    total_count: int


class SyncRunArtifactsToWorkspaceRequest(GatewayBaseModel):
    run_id: int = Field(ge=1)


class SyncedRunArtifact(GatewayBaseModel):
    artifact_id: int
    name: str
    digest: str
    destination_dir: str
    file_count: int
    bytes_written: int


class SyncRunArtifactsToWorkspaceResponse(GatewayBaseModel):
    workspace_id: str
    run_id: int
    run_attempt: int | None = None
    target_dir: str
    manifest_path: str
    remote_fingerprint: str
    downloaded: bool
    skipped: bool
    gitignore_path: str
    gitignore_updated: bool
    artifacts: list[SyncedRunArtifact]
    total_count: int
    warning: str | None = None
