from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import audit_store, github_client, policy, workspace_manager
from app.auth.dependencies import require_auth
from app.config.settings import Settings, get_settings
from app.errors import ErrorResponse
from app.github.client import GitHubClient
from app.models.branches import CreateWorkBranchRequest, CreateWorkBranchResponse
from app.models.ci import (
    CIStatusQueryRequest,
    CIStatusResponse,
    DeleteCacheRequest,
    DeleteCacheResponse,
    DispatchWorkflowRequest,
    DispatchWorkflowResponse,
    FailedCILogResponse,
    FailedLogQueryRequest,
    GetCiJobsRequest,
    GetCiJobsResponse,
    GetCiRunRequest,
    GetCiRunResponse,
    GetJobLogRequest,
    GetRunLogRequest,
    JobLogResponse,
    ListArtifactsRequest,
    ListArtifactsResponse,
    ListCachesRequest,
    ListCachesResponse,
    RerunWorkflowJobRequest,
    RerunWorkflowJobResponse,
    RerunWorkflowRunRequest,
    RerunWorkflowRunResponse,
    RunLogResponse,
    SyncRunArtifactsToWorkspaceRequest,
    SyncRunArtifactsToWorkspaceResponse,
)
from app.models.pulls import (
    CommentPullRequestRequest,
    CommentPullRequestResponse,
    CreatePullRequestRequest,
    CreatePullRequestResponse,
    GetPullRequestRequest,
    GetPullRequestResponse,
    ListPullRequestsRequest,
    ListPullRequestsResponse,
    MergePullRequestRequest,
    MergePullRequestResponse,
    PullRequestFilesRequest,
    PullRequestFilesResponse,
    UpdatePullRequestRequest,
    UpdatePullRequestResponse,
)
from app.models.workspaces import (
    PrepareWorkspaceRequest,
    PrepareWorkspaceResponse,
    WorkspaceApplyPatchRequest,
    WorkspaceApplyPatchResponse,
    WorkspaceCommitAndPushRequest,
    WorkspaceCommitAndPushResponse,
    WorkspaceDiffRequest,
    WorkspaceDiffResponse,
    WorkspaceExecPwshRequest,
    WorkspaceExecPwshResponse,
    WorkspaceResetRequest,
    WorkspaceResetResponse,
    WorkspaceStatusRequest,
    WorkspaceStatusResponse,
    WorkspaceWriteFileRequest,
    WorkspaceWriteFileResponse,
)
from app.policy.rules import Policy
from app.services.branches import BranchService
from app.services.ci import CIService
from app.services.pulls import PullRequestService
from app.services.workspaces import WorkspaceService
from app.storage.audit import AuditStore
from app.workspace.manager import WorkspaceManager

_error_responses = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    408: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    507: {"model": ErrorResponse},
}

router = APIRouter(
    prefix="/repos/{owner}/{repo}",
    tags=["GPT Actions Gateway v2"],
    dependencies=[Depends(require_auth)],
    responses=_error_responses,
)


@router.post("/workspaces/prepare", operation_id="prepareWorkspace", summary="Prepare a backend Git workspace", response_model=PrepareWorkspaceResponse)
async def prepare_workspace(
    owner: str,
    repo: str,
    request: PrepareWorkspaceRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    manager: Annotated[WorkspaceManager, Depends(workspace_manager)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> PrepareWorkspaceResponse:
    return await WorkspaceService(github, pol, settings, manager, audit).prepare(owner, repo, request)


@router.post("/workspaces/{workspace_id}/exec-pwsh", operation_id="workspaceExecPwsh", summary="Run controlled PowerShell in a workspace", response_model=WorkspaceExecPwshResponse)
async def workspace_exec_pwsh(
    owner: str,
    repo: str,
    workspace_id: str,
    request: WorkspaceExecPwshRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    manager: Annotated[WorkspaceManager, Depends(workspace_manager)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> WorkspaceExecPwshResponse:
    return await WorkspaceService(github, pol, settings, manager, audit).exec_pwsh(owner, repo, workspace_id, request)


@router.post("/workspaces/{workspace_id}/status", operation_id="workspaceStatus", summary="Inspect workspace status", response_model=WorkspaceStatusResponse)
async def workspace_status(
    owner: str,
    repo: str,
    workspace_id: str,
    request: WorkspaceStatusRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    manager: Annotated[WorkspaceManager, Depends(workspace_manager)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> WorkspaceStatusResponse:
    return await WorkspaceService(github, pol, settings, manager, audit).status(owner, repo, workspace_id, request)


@router.post("/workspaces/{workspace_id}/diff", operation_id="workspaceDiff", summary="Read current workspace diff", response_model=WorkspaceDiffResponse)
async def workspace_diff(
    owner: str,
    repo: str,
    workspace_id: str,
    request: WorkspaceDiffRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    manager: Annotated[WorkspaceManager, Depends(workspace_manager)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> WorkspaceDiffResponse:
    return await WorkspaceService(github, pol, settings, manager, audit).diff(owner, repo, workspace_id, request)


@router.post("/workspaces/{workspace_id}/apply-patch", operation_id="workspaceApplyPatch", summary="Apply a controlled text patch inside a workspace", response_model=WorkspaceApplyPatchResponse)
async def workspace_apply_patch(
    owner: str,
    repo: str,
    workspace_id: str,
    request: WorkspaceApplyPatchRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    manager: Annotated[WorkspaceManager, Depends(workspace_manager)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> WorkspaceApplyPatchResponse:
    return await WorkspaceService(github, pol, settings, manager, audit).apply_patch(owner, repo, workspace_id, request)


@router.post("/workspaces/{workspace_id}/write-file", operation_id="workspaceWriteFile", summary="Write one UTF-8 text file inside a workspace", response_model=WorkspaceWriteFileResponse)
async def workspace_write_file(
    owner: str,
    repo: str,
    workspace_id: str,
    request: WorkspaceWriteFileRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    manager: Annotated[WorkspaceManager, Depends(workspace_manager)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> WorkspaceWriteFileResponse:
    return await WorkspaceService(github, pol, settings, manager, audit).write_file(owner, repo, workspace_id, request)


@router.post("/workspaces/{workspace_id}/commit-and-push", operation_id="workspaceCommitAndPush", summary="Commit and push workspace changes", response_model=WorkspaceCommitAndPushResponse)
async def workspace_commit_and_push(
    owner: str,
    repo: str,
    workspace_id: str,
    request: WorkspaceCommitAndPushRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    manager: Annotated[WorkspaceManager, Depends(workspace_manager)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> WorkspaceCommitAndPushResponse:
    return await WorkspaceService(github, pol, settings, manager, audit).commit_and_push(owner, repo, workspace_id, request)


@router.post("/workspaces/{workspace_id}/reset", operation_id="workspaceReset", summary="Reset workspace to remote head", response_model=WorkspaceResetResponse)
async def workspace_reset(
    owner: str,
    repo: str,
    workspace_id: str,
    request: WorkspaceResetRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    manager: Annotated[WorkspaceManager, Depends(workspace_manager)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> WorkspaceResetResponse:
    return await WorkspaceService(github, pol, settings, manager, audit).reset(owner, repo, workspace_id, request)


@router.post("/branches/create-work-branch", operation_id="createWorkBranch", summary="Create or continue a work branch", response_model=CreateWorkBranchResponse)
async def create_work_branch(
    owner: str,
    repo: str,
    request: CreateWorkBranchRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> CreateWorkBranchResponse:
    return await BranchService(github, pol, settings, audit).create_work_branch(owner, repo, request)


@router.post("/pulls/create", operation_id="createPullRequest", summary="Create or reuse an open pull request", response_model=CreatePullRequestResponse)
async def create_pull_request(owner: str, repo: str, request: CreatePullRequestRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> CreatePullRequestResponse:
    return await PullRequestService(github, pol).create_pull_request(owner, repo, request)


@router.post("/pulls/get", operation_id="getPullRequest", summary="Get pull request details", response_model=GetPullRequestResponse)
async def get_pull_request(owner: str, repo: str, request: GetPullRequestRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> GetPullRequestResponse:
    return await PullRequestService(github, pol).get_pull_request(owner, repo, request)


@router.post("/pulls/list", operation_id="listPullRequests", summary="List pull requests", response_model=ListPullRequestsResponse)
async def list_pull_requests(owner: str, repo: str, request: ListPullRequestsRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> ListPullRequestsResponse:
    return await PullRequestService(github, pol).list_pull_requests(owner, repo, request)


@router.post("/pulls/files", operation_id="getPullRequestFiles", summary="List pull request changed files", response_model=PullRequestFilesResponse)
async def get_pull_request_files(owner: str, repo: str, request: PullRequestFilesRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> PullRequestFilesResponse:
    return await PullRequestService(github, pol).get_pull_request_files(owner, repo, request)


@router.post("/pulls/update", operation_id="updatePullRequest", summary="Update pull request title/body/state/base", response_model=UpdatePullRequestResponse)
async def update_pull_request(owner: str, repo: str, request: UpdatePullRequestRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> UpdatePullRequestResponse:
    return await PullRequestService(github, pol).update_pull_request(owner, repo, request)


@router.post("/pulls/merge", operation_id="mergePullRequest", summary="Merge a pull request", response_model=MergePullRequestResponse)
async def merge_pull_request(owner: str, repo: str, request: MergePullRequestRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> MergePullRequestResponse:
    return await PullRequestService(github, pol).merge_pull_request(owner, repo, request)


@router.post("/pulls/comment", operation_id="commentPullRequest", summary="Comment on a pull request", response_model=CommentPullRequestResponse)
async def comment_pull_request(owner: str, repo: str, request: CommentPullRequestRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> CommentPullRequestResponse:
    return await PullRequestService(github, pol).comment_pull_request(owner, repo, request)


@router.post("/ci/status/query", operation_id="queryCiStatus", summary="Query GitHub Actions status", response_model=CIStatusResponse)
async def query_ci_status(
    owner: str,
    repo: str,
    request: CIStatusQueryRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CIStatusResponse:
    return await CIService(github, pol, settings).get_ci_status(owner, repo, request)


@router.post("/ci/workflows/dispatch", operation_id="dispatchWorkflow", summary="Manually trigger a workflow_dispatch workflow", response_model=DispatchWorkflowResponse)
async def dispatch_workflow(
    owner: str,
    repo: str,
    request: DispatchWorkflowRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> DispatchWorkflowResponse:
    return await CIService(github, pol, settings, audit).dispatch_workflow(owner, repo, request)


@router.post("/ci/failed-log/query", operation_id="queryFailedCiLog", summary="Read failed CI log summary", response_model=FailedCILogResponse)
async def query_failed_ci_log(owner: str, repo: str, request: FailedLogQueryRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> FailedCILogResponse:
    return await CIService(github, pol, settings).get_failed_ci_log(owner, repo, request)


@router.post("/ci/runs/get", operation_id="getCiRun", summary="Get a workflow run", response_model=GetCiRunResponse)
async def get_ci_run(owner: str, repo: str, request: GetCiRunRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> GetCiRunResponse:
    return await CIService(github, pol, settings).get_ci_run(owner, repo, request)


@router.post("/ci/runs/rerun", operation_id="rerunWorkflowRun", summary="Rerun an entire workflow run", response_model=RerunWorkflowRunResponse)
async def rerun_workflow_run(
    owner: str,
    repo: str,
    request: RerunWorkflowRunRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> RerunWorkflowRunResponse:
    return await CIService(github, pol, settings, audit).rerun_workflow_run(owner, repo, request)


@router.post("/ci/jobs/list", operation_id="getCiJobs", summary="List jobs for a workflow run", response_model=GetCiJobsResponse)
async def get_ci_jobs(owner: str, repo: str, request: GetCiJobsRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> GetCiJobsResponse:
    return await CIService(github, pol, settings).get_ci_jobs(owner, repo, request)


@router.post("/ci/jobs/rerun", operation_id="rerunWorkflowJob", summary="Rerun a single workflow job", response_model=RerunWorkflowJobResponse)
async def rerun_workflow_job(
    owner: str,
    repo: str,
    request: RerunWorkflowJobRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> RerunWorkflowJobResponse:
    return await CIService(github, pol, settings, audit).rerun_workflow_job(owner, repo, request)


@router.post("/ci/jobs/log", operation_id="getJobLog", summary="Read a workflow job log", response_model=JobLogResponse)
async def get_job_log(owner: str, repo: str, request: GetJobLogRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> JobLogResponse:
    return await CIService(github, pol, settings).get_job_log(owner, repo, request)


@router.post("/ci/runs/log", operation_id="getRunLog", summary="Read workflow run log archive text files", response_model=RunLogResponse)
async def get_run_log(owner: str, repo: str, request: GetRunLogRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> RunLogResponse:
    return await CIService(github, pol, settings).get_run_log(owner, repo, request)


@router.post("/ci/artifacts/list", operation_id="listArtifacts", summary="List workflow run artifacts", response_model=ListArtifactsResponse)
async def list_artifacts(owner: str, repo: str, request: ListArtifactsRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> ListArtifactsResponse:
    return await CIService(github, pol, settings).list_artifacts(owner, repo, request)


@router.post(
    "/workspaces/{workspace_id}/artifacts/sync-run",
    operation_id="syncRunArtifactsToWorkspace",
    summary="Sync workflow run artifacts into a workspace",
    response_model=SyncRunArtifactsToWorkspaceResponse,
)
async def sync_run_artifacts_to_workspace(
    owner: str,
    repo: str,
    workspace_id: str,
    request: SyncRunArtifactsToWorkspaceRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    manager: Annotated[WorkspaceManager, Depends(workspace_manager)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> SyncRunArtifactsToWorkspaceResponse:
    return await WorkspaceService(github, pol, settings, manager, audit).sync_run_artifacts_to_workspace(owner, repo, workspace_id, request)

@router.post("/ci/caches/list", operation_id="listCaches", summary="List GitHub Actions caches", response_model=ListCachesResponse)
async def list_caches(owner: str, repo: str, request: ListCachesRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> ListCachesResponse:
    return await CIService(github, pol, settings).list_caches(owner, repo, request)


@router.post("/ci/caches/delete", operation_id="deleteCache", summary="Delete a GitHub Actions cache by id or key", response_model=DeleteCacheResponse)
async def delete_cache(
    owner: str,
    repo: str,
    request: DeleteCacheRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> DeleteCacheResponse:
    return await CIService(github, pol, settings, audit).delete_cache(owner, repo, request)
