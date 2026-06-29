from __future__ import annotations

import asyncio

from app.config.settings import Settings
from app.models.pulls import (
    CommentPullRequestRequest,
    CreatePullRequestRequest,
    GetPullRequestRequest,
    ListPullRequestsRequest,
    PullRequestFilesRequest,
    UpdatePullRequestRequest,
)
from app.policy.rules import Policy
from app.services.pulls import PullRequestService


def pr_payload(number: int = 7) -> dict:
    return {
        "number": number,
        "html_url": f"https://github.test/acme/demo/pull/{number}",
        "state": "open",
        "title": "Fix CI",
        "body": "body",
        "head": {"ref": "gpt/fix", "sha": "2222222222222222222222222222222222222222"},
        "base": {"ref": "main", "sha": "1111111111111111111111111111111111111111"},
        "draft": False,
        "merged": False,
        "mergeable": True,
        "created_at": "2026-05-30T00:00:00Z",
        "updated_at": "2026-05-30T00:00:00Z",
    }


class PullGitHubStub:
    def __init__(self) -> None:
        self.created: dict | None = None
        self.updated: dict | None = None
        self.comments: list[str] = []

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        return pr_payload(pr_number)

    async def list_pull_requests(self, owner: str, repo: str, *, head: str | None = None, base: str | None = None, state: str = "open", per_page: int = 50) -> list[dict]:
        assert state in {"open", "closed", "all"}
        assert per_page >= 1
        if head == "acme:feature/direct-maintenance":
            return []
        if base in {"gpt/parent", "feature/parent"}:
            return []
        return [pr_payload(7)]

    async def create_pull_request(self, owner: str, repo: str, *, head: str, base: str, title: str, body: str) -> dict:
        self.created = {"head": head, "base": base, "title": title, "body": body}
        payload = pr_payload(8)
        payload["head"]["ref"] = head
        payload["base"]["ref"] = base
        payload["title"] = title
        payload["body"] = body
        return payload

    async def get_pull_request_files(self, owner: str, repo: str, pr_number: int, *, per_page: int = 100) -> list[dict]:
        return [
            {"filename": "app.py", "status": "modified", "additions": 3, "deletions": 1},
            {"filename": "new.py", "status": "added", "additions": 2, "deletions": 0},
        ]

    async def update_pull_request(self, owner: str, repo: str, pr_number: int, **kwargs) -> dict:
        self.updated = kwargs
        payload = pr_payload(pr_number)
        payload["title"] = kwargs.get("title") or payload["title"]
        payload["body"] = kwargs.get("body") or payload["body"]
        payload["base"]["ref"] = kwargs.get("base") or payload["base"]["ref"]
        return payload

    async def create_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict:
        self.comments.append(body)
        return {"id": 99, "html_url": "https://github.test/comment/99", "body": body}


def make_service(github: PullGitHubStub) -> PullRequestService:
    settings = Settings(gpt_action_secret="secret", allowed_repos="acme/demo")
    return PullRequestService(github, Policy(settings))


def test_pull_request_query_and_files() -> None:
    github = PullGitHubStub()
    service = make_service(github)

    pr = asyncio.run(service.get_pull_request("acme", "demo", GetPullRequestRequest(pr_number=7)))
    prs = asyncio.run(service.list_pull_requests("acme", "demo", ListPullRequestsRequest(head_branch="gpt/fix", base_branch="main")))
    files = asyncio.run(service.get_pull_request_files("acme", "demo", PullRequestFilesRequest(pr_number=7)))

    assert pr.pull_request.head_branch == "gpt/fix"
    assert prs.pull_requests[0].pr_number == 7
    assert [item.status for item in files.files] == ["modified", "added"]


def test_create_pull_request_can_target_gpt_base_branch() -> None:
    github = PullGitHubStub()
    service = make_service(github)

    response = asyncio.run(
        service.create_pull_request(
            "acme",
            "demo",
            CreatePullRequestRequest(head_branch="gpt/child", base_branch="gpt/parent", title="Follow up", body="Stacked PR"),
        )
    )

    assert response.pr_number == 8
    assert response.head_branch == "gpt/child"
    assert response.base_branch == "gpt/parent"
    assert github.created == {"head": "gpt/child", "base": "gpt/parent", "title": "Follow up", "body": "Stacked PR"}


def test_create_pull_request_can_target_arbitrary_base_branch() -> None:
    github = PullGitHubStub()
    service = make_service(github)

    response = asyncio.run(
        service.create_pull_request(
            "acme",
            "demo",
            CreatePullRequestRequest(head_branch="gpt/child", base_branch="feature/parent", title="Follow up", body="Stacked PR"),
        )
    )

    assert response.pr_number == 8
    assert response.head_branch == "gpt/child"
    assert response.base_branch == "feature/parent"
    assert github.created == {"head": "gpt/child", "base": "feature/parent", "title": "Follow up", "body": "Stacked PR"}


def test_create_pull_request_can_use_arbitrary_head_branch() -> None:
    github = PullGitHubStub()
    service = make_service(github)

    response = asyncio.run(
        service.create_pull_request(
            "acme",
            "demo",
            CreatePullRequestRequest(head_branch="feature/direct-maintenance", base_branch="main", title="Follow up", body="Stacked PR"),
        )
    )

    assert response.pr_number == 8
    assert response.head_branch == "feature/direct-maintenance"
    assert response.base_branch == "main"
    assert github.created == {"head": "feature/direct-maintenance", "base": "main", "title": "Follow up", "body": "Stacked PR"}


def test_pull_request_update_and_comment() -> None:
    github = PullGitHubStub()
    service = make_service(github)

    updated = asyncio.run(service.update_pull_request("acme", "demo", UpdatePullRequestRequest(pr_number=7, title="New title", body="New body", base_branch="gpt/parent")))
    comment = asyncio.run(service.comment_pull_request("acme", "demo", CommentPullRequestRequest(pr_number=7, body="CI fixed")))

    assert updated.pull_request.title == "New title"
    assert updated.pull_request.base_branch == "gpt/parent"
    assert comment.comment_id == 99
    assert github.comments == ["CI fixed"]
