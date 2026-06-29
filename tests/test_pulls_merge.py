from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.models.pulls import MergePullRequestRequest
from app.policy.rules import Policy
from app.services.pulls import PullRequestService

HEAD_SHA = "2222222222222222222222222222222222222222"


def pr_payload(
    *,
    merged: bool = False,
    state: str = "open",
    draft: bool = False,
    mergeable: bool | None = True,
    base_ref: str = "main",
    head_ref: str = "gpt/fix-ci",
    head_sha: str = HEAD_SHA,
) -> dict:
    return {
        "number": 10,
        "html_url": "https://github.test/acme/demo/pull/10",
        "state": state,
        "title": "Fix CI",
        "body": "body",
        "head": {"ref": head_ref, "sha": head_sha},
        "base": {"ref": base_ref, "sha": "1111111111111111111111111111111111111111"},
        "draft": draft,
        "merged": merged,
        "mergeable": mergeable,
        "created_at": "2026-05-30T00:00:00Z",
        "updated_at": "2026-05-30T00:00:00Z",
    }


class MergeGitHubStub:
    def __init__(
        self,
        *,
        merged: bool = False,
        state: str = "open",
        draft: bool = False,
        mergeable: bool | None = True,
        base_ref: str = "main",
        head_ref: str = "gpt/fix-ci",
        head_sha: str = HEAD_SHA,
    ) -> None:
        self.pr = pr_payload(
            merged=merged,
            state=state,
            draft=draft,
            mergeable=mergeable,
            base_ref=base_ref,
            head_ref=head_ref,
            head_sha=head_sha,
        )
        self.merge_request: dict | None = None

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        return self.pr

    async def merge_pull_request(self, owner: str, repo: str, pr_number: int, **kwargs) -> dict:
        self.merge_request = kwargs
        self.pr = {**self.pr, "state": "closed", "merged": True, "mergeable": None}
        return {
            "sha": "3333333333333333333333333333333333333333",
            "merged": True,
            "message": "Pull Request successfully merged",
        }


def make_service(github: MergeGitHubStub) -> PullRequestService:
    settings = Settings(gpt_action_secret="secret", allowed_repos="acme/demo")
    return PullRequestService(github, Policy(settings))


def merge_request(**kwargs) -> MergePullRequestRequest:
    values = {"pr_number": 10, "expected_head_sha": HEAD_SHA}
    values.update(kwargs)
    return MergePullRequestRequest(**values)


def test_merge_pull_request_success() -> None:
    github = MergeGitHubStub()
    service = make_service(github)

    response = asyncio.run(
        service.merge_pull_request(
            "acme",
            "demo",
            merge_request(
                commit_title="Merge PR #10",
                commit_message="合并修复",
                merge_method="squash",
            ),
        )
    )

    assert response.merged is True
    assert response.commit_sha == "3333333333333333333333333333333333333333"
    assert response.pull_request.merged is True
    assert response.pull_request.state == "closed"
    assert github.merge_request == {
        "commit_title": "Merge PR #10",
        "commit_message": "合并修复",
        "sha": HEAD_SHA,
        "merge_method": "squash",
    }


def test_merge_pull_request_can_target_gpt_base_branch() -> None:
    github = MergeGitHubStub(base_ref="gpt/parent")
    service = make_service(github)

    response = asyncio.run(service.merge_pull_request("acme", "demo", merge_request()))

    assert response.merged is True
    assert response.pull_request.base_branch == "gpt/parent"


def test_merge_pull_request_can_target_arbitrary_base_branch() -> None:
    github = MergeGitHubStub(base_ref="release/2026.06")
    service = make_service(github)

    response = asyncio.run(service.merge_pull_request("acme", "demo", merge_request()))

    assert response.merged is True
    assert response.pull_request.base_branch == "release/2026.06"


def test_merge_pull_request_requires_expected_head_sha() -> None:
    with pytest.raises(ValidationError):
        MergePullRequestRequest(pr_number=10)


def test_merge_pull_request_rejects_head_sha_mismatch() -> None:
    service = make_service(MergeGitHubStub())

    with pytest.raises(ApiError) as exc:
        asyncio.run(service.merge_pull_request("acme", "demo", merge_request(expected_head_sha="9" * 40)))

    assert exc.value.error_code == ErrorCode.GITHUB_CONFLICT
    assert exc.value.message == "Pull request head SHA does not match expected_head_sha."


def test_merge_pull_request_allows_arbitrary_head_branch() -> None:
    service = make_service(MergeGitHubStub(head_ref="feature/fix-ci"))

    response = asyncio.run(service.merge_pull_request("acme", "demo", merge_request()))

    assert response.merged is True
    assert response.pull_request.head_branch == "feature/fix-ci"


def test_merge_pull_request_rejects_invalid_pr_state() -> None:
    closed_service = make_service(MergeGitHubStub(state="closed"))
    with pytest.raises(ApiError) as closed_exc:
        asyncio.run(closed_service.merge_pull_request("acme", "demo", merge_request()))
    assert closed_exc.value.error_code == ErrorCode.GITHUB_CONFLICT
    assert closed_exc.value.message == "Pull request is not open."

    draft_service = make_service(MergeGitHubStub(draft=True))
    with pytest.raises(ApiError) as draft_exc:
        asyncio.run(draft_service.merge_pull_request("acme", "demo", merge_request()))
    assert draft_exc.value.error_code == ErrorCode.GITHUB_CONFLICT
    assert draft_exc.value.message == "Draft pull requests cannot be merged."

    blocked_service = make_service(MergeGitHubStub(mergeable=False))
    with pytest.raises(ApiError) as blocked_exc:
        asyncio.run(blocked_service.merge_pull_request("acme", "demo", merge_request()))
    assert blocked_exc.value.error_code == ErrorCode.GITHUB_CONFLICT
    assert blocked_exc.value.message == "Pull request is not mergeable."
