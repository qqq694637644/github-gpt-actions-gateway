from __future__ import annotations

import asyncio

import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.models.branches import CreateWorkBranchRequest
from app.policy.rules import Policy
from app.services.branches import BranchService
from app.storage.audit import AuditStore


class EmptyRepoGitHubStub:
    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        raise ApiError(
            ErrorCode.GITHUB_CONFLICT,
            "GitHub reported a conflict.",
            status_code=409,
            details={"github_status": 409, "body": "Git Repository is empty."},
        )

    async def create_ref(self, owner: str, repo: str, branch: str, sha: str) -> dict[str, str]:
        return {"ref": f"refs/heads/{branch}", "object": {"sha": sha}}


def make_service(tmp_path, github: EmptyRepoGitHubStub) -> BranchService:
    settings = Settings(gpt_action_secret="secret", allowed_repos="acme/demo", audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}")
    return BranchService(github, Policy(settings), settings, AuditStore(settings.audit_db_url))


def test_create_work_branch_surfaces_empty_repo_conflict(tmp_path) -> None:
    service = make_service(tmp_path, EmptyRepoGitHubStub())

    with pytest.raises(ApiError) as exc:
        asyncio.run(
            service.create_work_branch(
                "acme",
                "demo",
                CreateWorkBranchRequest(base_ref="main", purpose_slug="bootstrap-test"),
            )
        )

    assert exc.value.error_code == ErrorCode.GITHUB_CONFLICT
