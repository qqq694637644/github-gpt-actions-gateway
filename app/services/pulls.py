from __future__ import annotations

from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
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
    PullRequestFile,
    PullRequestFilesRequest,
    PullRequestFilesResponse,
    PullRequestInfo,
    UpdatePullRequestRequest,
    UpdatePullRequestResponse,
)
from app.policy.rules import Policy


class PullRequestService:
    def __init__(self, github: GitHubClient, policy: Policy) -> None:
        self.github = github
        self.policy = policy

    async def create_pull_request(self, owner: str, repo: str, request: CreatePullRequestRequest) -> CreatePullRequestResponse:
        self.policy.assert_repo_allowed(owner, repo)
        self.policy.assert_write_branch_allowed(request.head_branch)
        existing = await self.github.list_pull_requests(owner, repo, head=f"{owner}:{request.head_branch}", base=request.base_branch, state="open", per_page=10)
        if existing:
            pr = existing[0]
            return self._create_response(pr, already_exists=True)
        pr = await self.github.create_pull_request(owner, repo, head=request.head_branch, base=request.base_branch, title=request.title, body=request.body)
        return self._create_response(pr, already_exists=False)

    async def get_pull_request(self, owner: str, repo: str, request: GetPullRequestRequest) -> GetPullRequestResponse:
        self.policy.assert_repo_allowed(owner, repo)
        pr = await self.github.get_pull_request(owner, repo, request.pr_number)
        return GetPullRequestResponse(pull_request=self._info(pr))

    async def list_pull_requests(self, owner: str, repo: str, request: ListPullRequestsRequest) -> ListPullRequestsResponse:
        self.policy.assert_repo_allowed(owner, repo)
        head = f"{owner}:{request.head_branch}" if request.head_branch else None
        pulls = await self.github.list_pull_requests(owner, repo, head=head, base=request.base_branch, state=request.state, per_page=request.max_results)
        items = [self._info(pr) for pr in pulls[: request.max_results]]
        return ListPullRequestsResponse(pull_requests=items, total_count=len(items))

    async def get_pull_request_files(self, owner: str, repo: str, request: PullRequestFilesRequest) -> PullRequestFilesResponse:
        self.policy.assert_repo_allowed(owner, repo)
        files = await self.github.get_pull_request_files(owner, repo, request.pr_number, per_page=request.max_results)
        mapped = [
            PullRequestFile(
                filename=item["filename"],
                status=item.get("status", ""),
                additions=int(item.get("additions") or 0),
                deletions=int(item.get("deletions") or 0),
                changes=int(item.get("changes") or 0),
                previous_filename=item.get("previous_filename"),
                sha=item.get("sha"),
                blob_url=item.get("blob_url"),
                raw_url=item.get("raw_url"),
            )
            for item in files[: request.max_results]
        ]
        return PullRequestFilesResponse(pr_number=request.pr_number, files=mapped, total_count=len(mapped))

    async def update_pull_request(self, owner: str, repo: str, request: UpdatePullRequestRequest) -> UpdatePullRequestResponse:
        self.policy.assert_repo_allowed(owner, repo)
        pr = await self.github.update_pull_request(owner, repo, request.pr_number, title=request.title, body=request.body, state=request.state, base=request.base_branch)
        return UpdatePullRequestResponse(pull_request=self._info(pr))

    async def merge_pull_request(self, owner: str, repo: str, request: MergePullRequestRequest) -> MergePullRequestResponse:
        self.policy.assert_repo_allowed(owner, repo)
        pr = await self.github.get_pull_request(owner, repo, request.pr_number)
        info = self._info(pr)
        self.policy.assert_write_branch_allowed(info.head_branch)
        if info.merged:
            raise ApiError(ErrorCode.GITHUB_CONFLICT, "Pull request is already merged.", status_code=409, details={"pr_number": request.pr_number})
        if info.state != "open":
            raise ApiError(ErrorCode.GITHUB_CONFLICT, "Pull request is not open.", status_code=409, details={"pr_number": request.pr_number, "state": info.state})
        if info.draft:
            raise ApiError(ErrorCode.GITHUB_CONFLICT, "Draft pull requests cannot be merged.", status_code=409, details={"pr_number": request.pr_number})
        if info.mergeable is False:
            raise ApiError(ErrorCode.GITHUB_CONFLICT, "Pull request is not mergeable.", status_code=409, details={"pr_number": request.pr_number})
        if request.expected_head_sha != info.head_sha:
            raise ApiError(
                ErrorCode.GITHUB_CONFLICT,
                "Pull request head SHA does not match expected_head_sha.",
                status_code=409,
                suggestion="Re-read the pull request and retry with the current head_sha after review.",
                details={"pr_number": request.pr_number, "expected_head_sha": request.expected_head_sha, "actual_head_sha": info.head_sha},
            )

        merged = await self.github.merge_pull_request(
            owner,
            repo,
            request.pr_number,
            commit_title=request.commit_title,
            commit_message=request.commit_message,
            sha=request.expected_head_sha,
            merge_method=request.merge_method,
        )
        updated = await self.github.get_pull_request(owner, repo, request.pr_number)
        return MergePullRequestResponse(
            pr_number=request.pr_number,
            merged=bool(merged.get("merged")),
            message=merged.get("message", ""),
            commit_sha=merged.get("sha"),
            pull_request=self._info(updated),
        )

    async def comment_pull_request(self, owner: str, repo: str, request: CommentPullRequestRequest) -> CommentPullRequestResponse:
        self.policy.assert_repo_allowed(owner, repo)
        comment = await self.github.create_issue_comment(owner, repo, request.pr_number, request.body)
        return CommentPullRequestResponse(comment_id=comment["id"], comment_url=comment.get("html_url") or comment.get("url", ""), body=comment.get("body", request.body), created_at=comment.get("created_at"))

    @staticmethod
    def _info(pr: dict) -> PullRequestInfo:
        head = pr.get("head") or {}
        base = pr.get("base") or {}
        return PullRequestInfo(
            pr_number=pr["number"],
            pr_url=pr.get("html_url") or pr.get("url", ""),
            state=pr.get("state", ""),
            title=pr.get("title", ""),
            body=pr.get("body"),
            head_branch=head.get("ref", ""),
            head_sha=head.get("sha", ""),
            base_branch=base.get("ref", ""),
            base_sha=base.get("sha"),
            merged=pr.get("merged"),
            draft=pr.get("draft"),
            mergeable=pr.get("mergeable"),
            labels=[label.get("name", "") for label in pr.get("labels", []) if label.get("name")],
            created_at=pr.get("created_at"),
            updated_at=pr.get("updated_at"),
        )

    def _create_response(self, pr: dict, *, already_exists: bool) -> CreatePullRequestResponse:
        info = self._info(pr)
        return CreatePullRequestResponse(
            pr_number=info.pr_number,
            pr_url=info.pr_url,
            state=info.state,
            head_branch=info.head_branch,
            head_sha=info.head_sha,
            base_branch=info.base_branch,
            already_exists=already_exists,
        )
