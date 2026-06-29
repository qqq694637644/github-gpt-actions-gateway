from __future__ import annotations

import secrets
from datetime import UTC, datetime

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
from app.models.branches import CreateWorkBranchRequest, CreateWorkBranchResponse
from app.policy.rules import Policy, is_sha, sanitize_purpose_slug
from app.storage.audit import AuditStore


class BranchService:
    def __init__(self, github: GitHubClient, policy: Policy, settings: Settings, audit: AuditStore | None = None) -> None:
        self.github = github
        self.policy = policy
        self.settings = settings
        self.audit = audit

    async def create_work_branch(self, owner: str, repo: str, request: CreateWorkBranchRequest) -> CreateWorkBranchResponse:
        self.policy.assert_repo_allowed(owner, repo)
        if request.branch is not None:
            self.policy.assert_write_branch_allowed(request.branch)
        scope = f"{owner}/{repo}:create_work_branch"
        payload = request.model_dump()
        if request.idempotency_key and self.audit:
            cached = self.audit.get_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload)
            if cached:
                return CreateWorkBranchResponse(**cached)

        base_ref, base_sha = await self._resolve_base(owner, repo, request)
        branch = request.branch if request.branch is not None else self._generate_branch_name(request.purpose_slug)
        self.policy.assert_write_branch_allowed(branch)
        try:
            await self.github.create_ref(owner, repo, branch, base_sha)
            response = CreateWorkBranchResponse(branch=branch, base_ref=base_ref, base_sha=base_sha, head_sha=base_sha, created=True)
        except ApiError as exc:
            if exc.error_code != ErrorCode.GITHUB_CONFLICT or not request.continue_if_exists:
                raise
            head_sha = await self.github.get_branch_head(owner, repo, branch)
            response = CreateWorkBranchResponse(
                branch=branch,
                base_ref=base_ref,
                base_sha=base_sha,
                head_sha=head_sha,
                created=False,
                continued=True,
                already_exists=True,
            )
        if request.idempotency_key and self.audit:
            self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload, response_payload=response.model_dump())
        return response

    async def _resolve_base(self, owner: str, repo: str, request: CreateWorkBranchRequest) -> tuple[str, str]:
        if request.base_sha:
            await self.github.get_commit_object(owner, repo, request.base_sha)
            return request.base_sha, request.base_sha
        base_ref = request.base_ref or self.settings.default_base_branch
        if is_sha(base_ref):
            await self.github.get_commit_object(owner, repo, base_ref)
            return base_ref, base_ref
        base_sha = await self.github.get_branch_head(owner, repo, base_ref)
        return base_ref, base_sha

    def _generate_branch_name(self, purpose_slug: str) -> str:
        slug = sanitize_purpose_slug(purpose_slug)
        date_part = datetime.now(UTC).strftime("%Y%m%d")
        suffix = secrets.token_hex(3)
        return f"{self.settings.write_branch_prefix}{slug}-{date_part}-{suffix}"
