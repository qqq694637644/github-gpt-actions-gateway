from __future__ import annotations

import asyncio
import hashlib
import io
import itertools
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.models.ci import SyncRunArtifactsToWorkspaceRequest
from app.models.workspaces import (
    PrepareWorkspaceRequest,
    WorkspaceApplyPatchRequest,
    WorkspaceCommitAndPushRequest,
    WorkspaceInspectRequest,
    WorkspaceReadFilesRequest,
    WorkspaceSearchRequest,
    WorkspaceStatusRequest,
    WorkspaceWriteFileRequest,
)
from app.policy.rules import Policy
from app.services.workspaces import WorkspaceService
from app.storage.audit import AuditStore
from app.workspace.manager import WorkspaceManager, split_command
from app.workspace.models import CommandResult
from app.workspace.text_ops import PreparedFileChange, commit_prepared_changes

_prepare_counter = itertools.count()


class LocalGitHub:
    def __init__(self, remote: Path) -> None:
        self.remote = remote
        self.artifact_zip = make_zip_bytes({"junit.xml": "<testsuite tests='1'/>\n", "nested/log.txt": "ok\n"})
        self.artifact_digest: str | None = artifact_digest(self.artifact_zip)
        self.artifact_size_in_bytes: int | None = None
        self.artifact_updated_at = "2026-05-30T00:01:00Z"
        self.downloaded_artifacts: list[int] = []

    def git_remote_url(self, owner: str, repo: str) -> str:
        return str(self.remote)

    async def git_auth_config(self) -> list[str]:
        return []

    async def get_repository(self, owner: str, repo: str) -> dict:
        return {"default_branch": "main"}

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        return git("rev-parse", f"refs/heads/{branch}", cwd=self.remote)

    async def get_commit_object(self, owner: str, repo: str, sha: str) -> dict:
        git("cat-file", "-e", f"{sha}^{{commit}}", cwd=self.remote)
        return {"sha": sha}

    async def create_ref(self, owner: str, repo: str, branch: str, sha: str) -> dict:
        existing = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
            cwd=self.remote,
            text=True,
            capture_output=True,
        )
        if existing.returncode == 0:
            raise ApiError(ErrorCode.GITHUB_CONFLICT, "already exists", status_code=409)
        git("update-ref", f"refs/heads/{branch}", sha, cwd=self.remote)
        return {"ref": f"refs/heads/{branch}", "object": {"sha": sha}}

    async def get_workflow_run(self, owner: str, repo: str, run_id: int) -> dict:
        return {
            "id": run_id,
            "run_attempt": 1,
            "workflow_id": 123,
            "name": "CI",
            "event": "pull_request",
            "head_branch": "gpt/task",
            "head_sha": "1111111111111111111111111111111111111111",
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.test/run/77",
            "created_at": "2026-05-30T00:00:00Z",
            "updated_at": "2026-05-30T00:01:00Z",
        }

    async def list_artifacts_for_run(self, owner: str, repo: str, run_id: int, *, per_page: int = 100, page: int | None = None) -> dict:
        return {
            "total_count": 1,
            "artifacts": [
                {
                    "id": 55,
                    "name": "reports",
                    "size_in_bytes": self.artifact_size_in_bytes if self.artifact_size_in_bytes is not None else len(self.artifact_zip),
                    "archive_download_url": "https://github.test/artifacts/55/zip",
                    "digest": self.artifact_digest,
                    "expired": False,
                    "created_at": "2026-05-30T00:00:00Z",
                    "expires_at": "2026-06-30T00:00:00Z",
                    "updated_at": self.artifact_updated_at,
                }
            ],
        }

    async def download_artifact(self, owner: str, repo: str, artifact_id: int) -> bytes:
        self.downloaded_artifacts.append(artifact_id)
        return self.artifact_zip


def make_zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def artifact_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def make_local_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(source)], check=True, capture_output=True)
    git("checkout", "-b", "main", cwd=source)
    git("config", "user.name", "tester", cwd=source)
    git("config", "user.email", "tester@example.com", cwd=source)
    (source / "README.md").write_text("before\n", encoding="utf-8")
    git("add", "README.md", cwd=source)
    git("commit", "-m", "Initial", cwd=source)
    git("checkout", "-b", "gpt/task", cwd=source)
    git("checkout", "-b", "feature/task", cwd=source)
    git("push", "origin", "main", "gpt/task", "feature/task", cwd=source)
    git("checkout", "gpt/task", cwd=source)
    return remote, source


def make_service(
    tmp_path: Path,
    remote: Path,
    *,
    allow_all_repos: bool = True,
    allowed_repos: str = "",
    workspace_ttl_hours: int = 48,
    workspace_python_venv_enabled: bool = False,
    workspace_python_venv_python: str | None = None,
) -> tuple[WorkspaceService, WorkspaceManager]:
    settings = Settings(
        allow_all_repos=allow_all_repos,
        allowed_repos=allowed_repos,
        workspace_root=str(tmp_path / "workspaces"),
        workspace_operation_root=str(tmp_path / "operations"),
        audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}",
        allow_workflow_edit=True,
        workspace_ttl_hours=workspace_ttl_hours,
        workspace_python_venv_enabled=workspace_python_venv_enabled,
        workspace_python_venv_python=workspace_python_venv_python or sys.executable,
    )
    github = LocalGitHub(remote)
    policy = Policy(settings)
    audit = AuditStore(settings.audit_db_url)
    manager = WorkspaceManager(settings, github, policy)  # type: ignore[arg-type]
    service = WorkspaceService(github, policy, settings, manager, audit)  # type: ignore[arg-type]
    return service, manager


def run(coro):
    return asyncio.run(coro)


def prepare_request(**kwargs) -> PrepareWorkspaceRequest:
    kwargs.pop("workspace_id", None)
    kwargs.setdefault("idempotency_key", f"prepare_{next(_prepare_counter):08d}")
    return PrepareWorkspaceRequest(**kwargs)


def rg_match_event(path: str, line_number: int, line_text: str, *, start: int = 0, match_text: str = "target_symbol") -> str:
    return json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": path},
                "line_number": line_number,
                "lines": {"text": line_text + "\n"},
                "submatches": [
                    {
                        "match": {"text": match_text},
                        "start": start,
                        "end": start + len(match_text),
                    }
                ],
            },
        }
    )


def test_prepare_can_create_or_continue_branch_before_workspace(tmp_path: Path):
    remote, source = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)
    main_sha = git("rev-parse", "main", cwd=source)

    prepared = run(
        service.prepare(
            "acme",
            "demo",
            prepare_request(
                mode="create_or_prepare_branch",
                base_ref="main",
                branch="gpt/created-by-prepare",
                workspace_id="ws_create_prepare",
            ),
        )
    )

    assert prepared.branch == "gpt/created-by-prepare"
    assert prepared.head_sha == main_sha
    assert prepared.branch_created is True
    assert prepared.branch_continued is False
    assert prepared.branch_already_exists is False
    assert prepared.branch_base_ref == "main"
    assert prepared.branch_base_sha == main_sha
    assert git("rev-parse", "refs/heads/gpt/created-by-prepare", cwd=remote) == main_sha


def test_prepare_is_idempotent_and_does_not_duplicate_workspace(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    request = PrepareWorkspaceRequest(idempotency_key="prepare_same_request", branch="gpt/task")

    first = run(service.prepare("acme", "demo", request))
    second = run(service.prepare("acme", "demo", request))

    assert second.workspace_id == first.workspace_id
    assert len(list(manager.root.glob("ws_*"))) == 1

    with pytest.raises(ApiError) as exc:
        run(
            service.prepare(
                "acme",
                "demo",
                PrepareWorkspaceRequest(
                    idempotency_key="prepare_same_request",
                    branch="feature/task",
                ),
            )
        )

    assert exc.value.error_code == ErrorCode.IDEMPOTENCY_KEY_REUSED


def test_workspace_inspect_search_and_read_files_do_not_need_pwsh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    remote, source = make_local_repo(tmp_path)
    (source / "src").mkdir()
    (source / "src" / "sample.py").write_text(
        "def target_symbol():\n"
        "    return 'needle'\n"
        "\n"
        "def other():\n"
        "    return target_symbol()\n",
        encoding="utf-8",
    )
    git("add", "src/sample.py", cwd=source)
    git("commit", "-m", "Add sample source", cwd=source)
    git("push", "origin", "gpt/task", cwd=source)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_inspect")))

    monkeypatch.setattr("app.services.workspaces.shutil.which", lambda name: "rg" if name == "rg" else None)

    async def fake_rg_run(args, **kwargs):
        assert args[0] == "rg"
        assert "--hidden" not in args
        assert "--no-ignore" not in args
        assert "--ignore-file" not in args
        assert args[-1] in {"src", "."}
        return CommandResult(
            exit_code=0,
            stdout="\n".join(
                [
                    rg_match_event("src/sample.py", 1, "def target_symbol():"),
                    rg_match_event("src/sample.py", 5, "    return target_symbol()", start=11),
                ]
            )
            + "\n",
            stderr="",
            duration_ms=3,
            truncated=False,
        )

    monkeypatch.setattr(manager.git, "run", fake_rg_run)

    read_response = run(
        service.read_files(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceReadFilesRequest(paths=["src/sample.py"], max_lines=2),
        )
    )
    assert read_response.files[0].content.startswith("1: def target_symbol")

    search_response = run(
        service.search(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceSearchRequest(query="target_symbol", paths=["src"], max_matches=5),
        )
    )
    assert search_response.match_count >= 1
    assert search_response.matches[0].path == "src/sample.py"
    assert search_response.matches[0].snippet is not None

    inspect_response = run(
        service.inspect(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceInspectRequest(paths=["."], queries=["target_symbol"], max_read_files=1),
        )
    )
    assert any(entry.path == "src/sample.py" for entry in inspect_response.tree)
    assert inspect_response.searches[0].match_count >= 1
    assert inspect_response.files[0].path == "src/sample.py"


def test_workspace_search_requires_ripgrep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_rg_required")))
    monkeypatch.setattr("app.services.workspaces.shutil.which", lambda name: None)

    with pytest.raises(ApiError) as exc:
        run(
            service.search(
                "acme",
                "demo",
                prepared.workspace_id,
                WorkspaceSearchRequest(query="anything"),
            )
        )

    assert exc.value.error_code == ErrorCode.WORKSPACE_EXEC_FAILED
    assert "ripgrep" in exc.value.message


def test_workspace_search_uses_ripgrep_default_filters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_rg_defaults")))
    monkeypatch.setattr("app.services.workspaces.shutil.which", lambda name: "rg" if name == "rg" else None)
    captured: dict[str, list[str]] = {}

    async def fake_rg_run(args, **kwargs):
        captured["args"] = list(args)
        return CommandResult(exit_code=1, stdout="", stderr="", duration_ms=2, truncated=False)

    monkeypatch.setattr(manager.git, "run", fake_rg_run)

    response = run(
        service.search(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceSearchRequest(query="SECRET", paths=["."], max_matches=5),
        )
    )

    assert response.match_count == 0
    args = captured["args"]
    assert args[0] == "rg"
    assert "--hidden" not in args
    assert "--no-ignore" not in args
    assert "--ignore-file" not in args
    assert args[-1] == "."


def test_workspace_read_files_truncates_single_long_line_to_max_bytes(tmp_path: Path):
    remote, source = make_local_repo(tmp_path)
    (source / "src").mkdir()
    (source / "src" / "long.txt").write_text("x" * 5000 + "\nsecond\n", encoding="utf-8")
    git("add", "src/long.txt", cwd=source)
    git("commit", "-m", "Add long line", cwd=source)
    git("push", "origin", "gpt/task", cwd=source)
    service, _ = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_long_line")))

    response = run(
        service.read_files(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceReadFilesRequest(paths=["src/long.txt"], max_lines=1, max_bytes_per_file=80),
        )
    )

    file = response.files[0]
    assert file.truncated is True
    assert len(file.content.encode("utf-8")) <= 80


def test_workspace_read_files_respects_total_response_budget(tmp_path: Path):
    remote, source = make_local_repo(tmp_path)
    (source / "docs").mkdir()
    paths = []
    for index in range(8):
        path = source / "docs" / f"file-{index}.txt"
        path.write_text(f"file {index}\n" + ("x" * 900) + "\n", encoding="utf-8")
        paths.append(f"docs/file-{index}.txt")
    git("add", "docs", cwd=source)
    git("commit", "-m", "Add docs", cwd=source)
    git("push", "origin", "gpt/task", cwd=source)
    service, _ = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_read_budget")))

    response = run(
        service.read_files(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceReadFilesRequest(paths=paths, max_bytes_per_file=1200, max_bytes=2500),
        )
    )

    assert len(response.model_dump_json().encode("utf-8")) <= 2500
    assert response.truncated is True


def test_workspace_read_files_refuses_symlink_before_resolving(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_read_symlink")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    link_path = repo_dir / "linked-readme.md"
    try:
        link_path.symlink_to("README.md")
    except OSError as exc:
        pytest.skip(f"symlink creation is not available in this test environment: {exc}")

    response = run(
        service.read_files(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceReadFilesRequest(paths=["linked-readme.md"]),
        )
    )

    file = response.files[0]
    assert file.content == ""
    assert file.error == "Workspace read operations refuse symlinks."
    assert str(repo_dir) not in (file.error or "")


def test_workspace_read_files_refuses_symlink_directory_component(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_read_symlink_dir")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    link_path = repo_dir / "visible-link"
    try:
        link_path.symlink_to(".git", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is not available in this test environment: {exc}")

    response = run(
        service.read_files(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceReadFilesRequest(paths=["visible-link/config"]),
        )
    )

    file = response.files[0]
    assert file.content == ""
    assert file.error == "Workspace read operations refuse symlinks."
    assert "hidden config" not in file.content
    assert str(repo_dir) not in (file.error or "")


def test_workspace_inspect_refuses_symlink_directory_path(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_inspect_symlink_dir")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    link_path = repo_dir / "visible-link"
    try:
        link_path.symlink_to(".git", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is not available in this test environment: {exc}")

    with pytest.raises(ApiError) as exc:
        run(
            service.inspect(
                "acme",
                "demo",
                prepared.workspace_id,
                WorkspaceInspectRequest(paths=["visible-link"]),
            )
        )

    assert exc.value.error_code == ErrorCode.WORKSPACE_POLICY_VIOLATION
    assert exc.value.message == "Workspace inspection operations refuse symlinks."
    assert str(repo_dir) not in str(exc.value.details)


def test_workspace_search_and_inspect_respect_total_response_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    remote, source = make_local_repo(tmp_path)
    (source / "src").mkdir()
    lines = [f"def target_symbol_{idx}(): return '{'x' * 180}'" for idx in range(80)]
    (source / "src" / "many.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    git("add", "src/many.py", cwd=source)
    git("commit", "-m", "Add many matches", cwd=source)
    git("push", "origin", "gpt/task", cwd=source)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_budget")))
    monkeypatch.setattr("app.services.workspaces.shutil.which", lambda name: "rg" if name == "rg" else None)

    async def fake_rg_run(args, **kwargs):
        assert args[0] == "rg"
        stdout = "\n".join(
            rg_match_event("src/many.py", idx + 1, line, start=4, match_text="target_symbol")
            for idx, line in enumerate(lines)
        )
        return CommandResult(exit_code=0, stdout=stdout + "\n", stderr="", duration_ms=4, truncated=False)

    monkeypatch.setattr(manager.git, "run", fake_rg_run)

    search_response = run(
        service.search(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceSearchRequest(query="target_symbol", paths=["src"], max_matches=80, max_bytes=2500),
        )
    )
    inspect_response = run(
        service.inspect(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceInspectRequest(paths=["src"], queries=["target_symbol"], max_search_matches=80, max_read_files=10, max_bytes=2500),
        )
    )

    assert len(search_response.model_dump_json().encode("utf-8")) <= 2500
    assert search_response.truncated is True
    assert len(inspect_response.model_dump_json().encode("utf-8")) <= 2500
    assert inspect_response.truncated is True


def test_sync_run_artifacts_to_workspace_downloads_and_skips_unchanged_run(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_artifacts")))
    github = service.github  # type: ignore[attr-defined]

    first = run(
        service.sync_run_artifacts_to_workspace(
            "acme",
            "demo",
            prepared.workspace_id,
            SyncRunArtifactsToWorkspaceRequest(run_id=77),
        )
    )
    repo_dir = manager.repo_dir(prepared.workspace_id)

    assert first.downloaded is True
    assert first.skipped is False
    assert first.target_dir == ".gpt-artifacts/runs/77"
    assert first.manifest_path == ".gpt-artifacts/runs/77/manifest.json"
    assert first.gitignore_path == ".git/info/exclude"
    assert first.gitignore_updated is True
    assert first.artifacts[0].destination_dir == ".gpt-artifacts/runs/77/55-reports"
    assert (repo_dir / first.artifacts[0].destination_dir / "junit.xml").read_text(encoding="utf-8").startswith("<testsuite")
    assert json.loads((repo_dir / first.manifest_path).read_text(encoding="utf-8"))["remote_fingerprint"] == first.remote_fingerprint
    assert ".gpt-artifacts/" in (repo_dir / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".gpt-artifacts" not in git("status", "--porcelain=v1", "--untracked-files=all", cwd=repo_dir)
    status = run(service.status("acme", "demo", prepared.workspace_id, WorkspaceStatusRequest()))
    assert status.dirty is False
    assert status.changed_files == []
    assert github.downloaded_artifacts == [55]

    github.artifact_size_in_bytes = len(github.artifact_zip) + 123
    github.artifact_updated_at = "2026-05-30T00:02:00Z"

    second = run(
        service.sync_run_artifacts_to_workspace(
            "acme",
            "demo",
            prepared.workspace_id,
            SyncRunArtifactsToWorkspaceRequest(run_id=77),
        )
    )

    assert second.downloaded is False
    assert second.skipped is True
    assert github.downloaded_artifacts == [55]


def test_sync_run_artifacts_to_workspace_replaces_target_when_digest_changes(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_artifacts_replace")))
    github = service.github  # type: ignore[attr-defined]

    first = run(
        service.sync_run_artifacts_to_workspace(
            "acme",
            "demo",
            prepared.workspace_id,
            SyncRunArtifactsToWorkspaceRequest(run_id=77),
        )
    )
    repo_dir = manager.repo_dir(prepared.workspace_id)
    assert (repo_dir / first.artifacts[0].destination_dir / "junit.xml").exists()

    github.artifact_zip = make_zip_bytes({"new-report.txt": "new\n"})
    github.artifact_digest = artifact_digest(github.artifact_zip)
    second = run(
        service.sync_run_artifacts_to_workspace(
            "acme",
            "demo",
            prepared.workspace_id,
            SyncRunArtifactsToWorkspaceRequest(run_id=77),
        )
    )

    assert second.downloaded is True
    assert second.skipped is False
    assert github.downloaded_artifacts == [55, 55]
    assert not (repo_dir / first.artifacts[0].destination_dir / "junit.xml").exists()
    assert (repo_dir / second.artifacts[0].destination_dir / "new-report.txt").read_text(encoding="utf-8") == "new\n"


def test_sync_run_artifacts_to_workspace_requires_artifact_digest(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_artifacts_no_digest")))
    service.github.artifact_digest = None  # type: ignore[attr-defined]

    with pytest.raises(ApiError) as exc:
        run(
            service.sync_run_artifacts_to_workspace(
                "acme",
                "demo",
                prepared.workspace_id,
                SyncRunArtifactsToWorkspaceRequest(run_id=77),
            )
        )

    assert exc.value.error_code == ErrorCode.GITHUB_ERROR
    assert exc.value.message == (
        "GitHub artifact metadata did not include digest, so the gateway refused to sync it safely. "
        "Use getRunLog/job logs instead, or enable an explicit unsafe artifact sync mode after review."
    )
    assert exc.value.details == {"missing_artifacts": [{"artifact_id": 55, "name": "reports"}]}


def test_sync_run_artifacts_to_workspace_rejects_unsupported_digest_format(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_artifacts_bad_digest")))
    service.github.artifact_digest = "sha256:not-hex"  # type: ignore[attr-defined]

    with pytest.raises(ApiError) as exc:
        run(
            service.sync_run_artifacts_to_workspace(
                "acme",
                "demo",
                prepared.workspace_id,
                SyncRunArtifactsToWorkspaceRequest(run_id=77),
            )
        )

    assert exc.value.error_code == ErrorCode.GITHUB_ERROR
    assert "Unsupported artifact digest format" in exc.value.message


def test_sync_run_artifacts_to_workspace_rejects_digest_mismatch(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_artifacts_digest_mismatch")))
    service.github.artifact_digest = artifact_digest(b"different archive bytes")  # type: ignore[attr-defined]

    with pytest.raises(ApiError) as exc:
        run(
            service.sync_run_artifacts_to_workspace(
                "acme",
                "demo",
                prepared.workspace_id,
                SyncRunArtifactsToWorkspaceRequest(run_id=77),
            )
        )

    assert exc.value.error_code == ErrorCode.GITHUB_ERROR
    assert "does not match GitHub digest" in exc.value.message


def test_workspace_commit_and_push_updates_local_remote(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)

    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    (repo_dir / "README.md").write_text("after\n", encoding="utf-8")

    response = run(
        service.commit_and_push(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceCommitAndPushRequest(branch="gpt/task", expected_head_sha=prepared.head_sha, commit_message="Update README"),
        )
    )

    assert response.pushed is True
    assert response.previous_head_sha == prepared.head_sha
    assert response.new_head_sha != prepared.head_sha
    assert response.changed_files[0].path == "README.md"
    assert git("rev-parse", "gpt/task", cwd=remote) == response.new_head_sha


def test_workspace_prepare_commit_and_push_allows_arbitrary_branch(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)

    prepared = run(service.prepare("acme", "demo", prepare_request(branch="feature/task")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    (repo_dir / "README.md").write_text("after feature\n", encoding="utf-8")

    response = run(
        service.commit_and_push(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceCommitAndPushRequest(branch="feature/task", expected_head_sha=prepared.head_sha, commit_message="Update feature branch"),
        )
    )

    assert prepared.branch == "feature/task"
    assert response.pushed is True
    assert response.previous_head_sha == prepared.head_sha
    assert response.new_head_sha != prepared.head_sha
    assert response.changed_files[0].path == "README.md"
    assert git("rev-parse", "feature/task", cwd=remote) == response.new_head_sha


def test_workspace_commit_and_push_recovers_after_commit_succeeds_but_push_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)

    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_push_recovery")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    (repo_dir / "README.md").write_text("after\n", encoding="utf-8")
    original_remote_head = git("rev-parse", "gpt/task", cwd=remote)
    original_run = manager.git.run
    failed = {"push": False}

    async def flaky_run(args, *run_args, **run_kwargs):
        if args and args[0] == "git" and "push" in args and not failed["push"]:
            failed["push"] = True
            return CommandResult(exit_code=1, stdout="", stderr="simulated push failure", duration_ms=1, truncated=False, timed_out=False)
        return await original_run(args, *run_args, **run_kwargs)

    monkeypatch.setattr(manager.git, "run", flaky_run)
    request = WorkspaceCommitAndPushRequest(branch="gpt/task", expected_head_sha=prepared.head_sha, commit_message="Update README")

    with pytest.raises(ApiError) as exc:
        run(service.commit_and_push("acme", "demo", prepared.workspace_id, request))

    assert exc.value.error_code == ErrorCode.WORKSPACE_PUSH_FAILED
    assert git("rev-parse", "gpt/task", cwd=remote) == original_remote_head
    assert git("rev-parse", "HEAD", cwd=repo_dir) != original_remote_head

    response = run(service.commit_and_push("acme", "demo", prepared.workspace_id, request))

    assert response.pushed is True
    assert response.previous_head_sha == original_remote_head
    assert response.commit_sha == response.new_head_sha
    assert {item.path for item in response.changed_files} == {"README.md"}
    assert git("rev-parse", "gpt/task", cwd=remote) == response.new_head_sha


def test_workspace_prepare_ignores_legacy_helper_id_and_generates_server_id(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)

    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_custom_1")))

    assert prepared.workspace_id.startswith("ws_")
    assert prepared.workspace_id != "ws_custom_1"
    assert prepared.created is True
    assert prepared.diagnostics.workspace_stage == "clone"
    assert prepared.diagnostics.total_duration_ms >= prepared.diagnostics.workspace_duration_ms


def test_workspace_prepare_prunes_expired_workspace(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote, workspace_ttl_hours=48)
    expired = manager.workspace_dir("ws_expired")
    expired.mkdir(parents=True)
    old = time.time() - 49 * 60 * 60
    os.utime(expired, (old, old))

    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_new")))

    assert prepared.workspace_id.startswith("ws_")
    assert not expired.exists()


def test_workspace_prune_removes_readonly_git_objects(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    _, manager = make_service(tmp_path, remote, workspace_ttl_hours=48)
    expired = manager.workspace_dir("ws_expired_readonly")
    object_dir = expired / "repo" / ".git" / "objects" / "00"
    object_dir.mkdir(parents=True)
    readonly_object = object_dir / "abcdef"
    readonly_object.write_text("git-object", encoding="utf-8")
    readonly_object.chmod(stat.S_IREAD)
    old = time.time() - 49 * 60 * 60
    os.utime(expired, (old, old))

    try:
        assert manager.prune_expired_workspace_dirs() == 1
        assert not expired.exists()
    finally:
        if readonly_object.exists():
            readonly_object.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_workspace_prepare_keeps_fresh_and_active_workspace_dirs(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote, workspace_ttl_hours=48)
    fresh = manager.workspace_dir("ws_fresh")
    active = manager.workspace_dir("ws_active")
    fresh.mkdir(parents=True)
    active.mkdir(parents=True)
    old = time.time() - 49 * 60 * 60
    os.utime(active, (old, old))
    manager.set_active_workspace_provider(lambda: {"ws_active"})

    run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_new")))

    assert fresh.exists()
    assert active.exists()


def test_workspace_prune_ignores_non_workspace_dirs(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    _, manager = make_service(tmp_path, remote, workspace_ttl_hours=48)
    non_workspace = manager.root / "cache"
    non_workspace.mkdir(parents=True)
    old = time.time() - 49 * 60 * 60
    os.utime(non_workspace, (old, old))

    assert manager.prune_expired_workspace_dirs() == 0

    assert non_workspace.exists()


def test_legacy_workspace_lock_cleanup_is_explicit_and_non_persistent(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    _, manager = make_service(tmp_path, remote)
    workspace_dir = manager.workspace_dir("ws_legacy_lock")
    workspace_dir.mkdir(parents=True)
    lock_path = workspace_dir / "lock"
    lock_path.write_text("old-pid", encoding="utf-8")

    assert manager.remove_legacy_lock_files() == 1
    assert not lock_path.exists()
    assert manager.remove_legacy_lock_files() == 0


def test_prepare_work_branch_bootstraps_python_venv_without_committable_diff(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote, workspace_python_venv_enabled=True, workspace_python_venv_python=sys.executable)

    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_python")))
    repo_dir = manager.repo_dir(prepared.workspace_id)

    assert (repo_dir / ".venv" / "pyvenv.cfg").exists()
    assert ".venv/" in (repo_dir / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert git("check-ignore", ".venv/pyvenv.cfg", cwd=repo_dir) == ".venv/pyvenv.cfg"
    status = run(service.status("acme", "demo", prepared.workspace_id, WorkspaceStatusRequest()))
    assert status.dirty is False
    assert status.changed_files == []


def test_prepare_arbitrary_branch_bootstraps_python_venv_without_prefix_check(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote, workspace_python_venv_enabled=True, workspace_python_venv_python=sys.executable)

    prepared = run(service.prepare("acme", "demo", prepare_request(branch="feature/task", workspace_id="ws_python_feature")))
    repo_dir = manager.repo_dir(prepared.workspace_id)

    assert prepared.branch == "feature/task"
    assert (repo_dir / ".venv" / "pyvenv.cfg").exists()
    status = run(service.status("acme", "demo", prepared.workspace_id, WorkspaceStatusRequest()))
    assert status.dirty is False
    assert status.changed_files == []


def test_prepare_base_ref_does_not_bootstrap_python_venv(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote, workspace_python_venv_enabled=True, workspace_python_venv_python=sys.executable)

    prepared = run(service.prepare("acme", "demo", prepare_request(base_ref="main", workspace_id="ws_read_only")))
    repo_dir = manager.repo_dir(prepared.workspace_id)

    assert not (repo_dir / ".venv").exists()
    assert not (repo_dir / ".gitignore").exists()


def test_prepare_arbitrary_base_ref_is_read_only_and_does_not_bootstrap_python_venv(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote, workspace_python_venv_enabled=True, workspace_python_venv_python=sys.executable)

    prepared = run(service.prepare("acme", "demo", prepare_request(base_ref="feature/task", workspace_id="ws_read_only_feature")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    (repo_dir / "README.md").write_text("read-only change\n", encoding="utf-8")

    assert prepared.branch == "feature/task"
    assert not (repo_dir / ".venv").exists()

    with pytest.raises(ApiError) as exc:
        run(
            service.commit_and_push(
                "acme",
                "demo",
                prepared.workspace_id,
                WorkspaceCommitAndPushRequest(branch="feature/task", expected_head_sha=prepared.head_sha, commit_message="Should not publish"),
            )
        )

    assert exc.value.error_code == ErrorCode.WORKSPACE_POLICY_VIOLATION
    assert "read-only base_ref workspace" in exc.value.message


def test_legacy_workspace_meta_missing_writable_is_rejected(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)

    prepared = run(service.prepare("acme", "demo", prepare_request(base_ref="feature/task", workspace_id="ws_legacy_meta")))
    meta_file = manager.workspace_dir(prepared.workspace_id) / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta.pop("writable") is False
    meta_file.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(ApiError) as exc:
        run(
            service.commit_and_push(
                "acme",
                "demo",
                prepared.workspace_id,
                WorkspaceCommitAndPushRequest(branch="feature/task", expected_head_sha=prepared.head_sha, commit_message="Should not publish"),
            )
        )

    assert exc.value.error_code == ErrorCode.WORKSPACE_POLICY_VIOLATION
    assert "missing required field 'writable'" in exc.value.message


def test_prepare_python_venv_does_not_modify_tracked_gitignore_or_create_status_diff(tmp_path: Path):
    remote, source = make_local_repo(tmp_path)
    (source / ".gitignore").write_text("dist/\n", encoding="utf-8")
    git("add", ".gitignore", cwd=source)
    git("commit", "-m", "Track gitignore", cwd=source)
    git("push", "origin", "gpt/task", cwd=source)
    service, manager = make_service(tmp_path, remote, workspace_python_venv_enabled=True, workspace_python_venv_python=sys.executable)

    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_python_no_diff")))
    repo_dir = manager.repo_dir(prepared.workspace_id)

    assert (repo_dir / ".gitignore").read_text(encoding="utf-8") == "dist/\n"
    assert (repo_dir / ".venv" / "pyvenv.cfg").exists()
    assert ".venv/" in (repo_dir / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    status = run(service.status("acme", "demo", prepared.workspace_id, WorkspaceStatusRequest()))
    assert status.dirty is False
    assert status.changed_files == []
    assert git("status", "--porcelain=v1", "--untracked-files=all", cwd=repo_dir) == ""


def test_prepare_existing_broken_python_venv_fails(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote, workspace_python_venv_enabled=False, workspace_python_venv_python=sys.executable)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_broken_python")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    broken_venv = repo_dir / ".venv"
    broken_venv.mkdir()
    (broken_venv / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    with pytest.raises(ApiError) as exc:
        run(manager.validate_python_venv(repo_dir))

    assert exc.value.error_code == ErrorCode.WORKSPACE_POLICY_VIOLATION
    assert "interpreter directory is missing" in exc.value.message


def test_prepare_existing_python_venv_without_pyvenv_cfg_fails(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote, workspace_python_venv_enabled=False, workspace_python_venv_python=sys.executable)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task", workspace_id="ws_missing_pyvenv_cfg")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    (repo_dir / ".venv").mkdir()
    with pytest.raises(ApiError) as exc:
        run(manager.validate_python_venv(repo_dir))

    assert exc.value.error_code == ErrorCode.WORKSPACE_POLICY_VIOLATION
    assert "pyvenv.cfg is missing" in exc.value.message


def test_split_command_handles_quoted_python_path_with_spaces() -> None:
    parts = split_command(r'"C:\Program Files\Python313\python.exe" -m venv')

    assert parts == [r"C:\Program Files\Python313\python.exe", "-m", "venv"]



def test_workspace_apply_patch_dry_run_and_apply_do_not_push(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)

    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    original_remote_head = git("rev-parse", "gpt/task", cwd=remote)
    patch = "*** Begin Patch\n*** Update File: README.md\n@@\n-before\n+after\n*** End Patch\n"

    dry = run(service.apply_patch("acme", "demo", prepared.workspace_id, WorkspaceApplyPatchRequest(patch=patch, dry_run=True)))
    assert dry.applied is False
    assert (repo_dir / "README.md").read_text(encoding="utf-8") == "before\n"

    applied = run(service.apply_patch("acme", "demo", prepared.workspace_id, WorkspaceApplyPatchRequest(patch=patch)))
    assert applied.applied is True
    assert applied.changed_files[0].path == "README.md"
    assert applied.changed_files[0].operation == "modified"
    assert (repo_dir / "README.md").read_text(encoding="utf-8") == "after\n"
    assert git("rev-parse", "gpt/task", cwd=remote) == original_remote_head


def test_workspace_apply_patch_rejects_delete_by_default(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task")))
    patch = "*** Begin Patch\n*** Delete File: README.md\n*** End Patch\n"

    with pytest.raises(ApiError) as exc:
        run(service.apply_patch("acme", "demo", prepared.workspace_id, WorkspaceApplyPatchRequest(patch=patch)))
    assert exc.value.error_code == ErrorCode.WORKSPACE_DELETE_NOT_ALLOWED


def test_workspace_apply_patch_context_mismatch_leaves_file_unchanged(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    patch = "*** Begin Patch\n*** Update File: README.md\n@@\n-missing\n+after\n*** End Patch\n"

    with pytest.raises(ApiError) as exc:
        run(service.apply_patch("acme", "demo", prepared.workspace_id, WorkspaceApplyPatchRequest(patch=patch)))
    assert exc.value.error_code == ErrorCode.WORKSPACE_PATCH_CONTEXT_MISMATCH
    assert (repo_dir / "README.md").read_text(encoding="utf-8") == "before\n"


def test_workspace_write_file_create_only_and_sha_guard(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    original_remote_head = git("rev-parse", "gpt/task", cwd=remote)

    dry = run(
        service.write_file(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceWriteFileRequest(path="docs/ci.md", content="# CI\n", dry_run=True),
        )
    )
    assert dry.written is False
    assert not (repo_dir / "docs/ci.md").exists()

    written = run(
        service.write_file(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceWriteFileRequest(path="docs/ci.md", content="# CI\n", line_ending="lf"),
        )
    )
    assert written.written is True
    assert written.operation == "added"
    assert written.previous_sha256 is None
    assert (repo_dir / "docs/ci.md").read_text(encoding="utf-8") == "# CI\n"
    assert git("rev-parse", "gpt/task", cwd=remote) == original_remote_head

    with pytest.raises(ApiError) as exc:
        run(service.write_file("acme", "demo", prepared.workspace_id, WorkspaceWriteFileRequest(path="docs/ci.md", content="again\n")))
    assert exc.value.error_code == ErrorCode.WORKSPACE_FILE_EXISTS

    with pytest.raises(ApiError) as exc:
        run(
            service.write_file(
                "acme",
                "demo",
                prepared.workspace_id,
                WorkspaceWriteFileRequest(path="docs/ci.md", content="again\n", mode="overwrite_if_sha256_matches", expected_sha256="0" * 64),
            )
        )
    assert exc.value.error_code == ErrorCode.WORKSPACE_SHA_MISMATCH


def test_workspace_write_file_rejects_sensitive_path(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task")))

    with pytest.raises(ApiError) as exc:
        run(service.write_file("acme", "demo", prepared.workspace_id, WorkspaceWriteFileRequest(path=".env", content="SECRET=x\n")))
    assert exc.value.error_code == ErrorCode.WORKSPACE_POLICY_VIOLATION


def test_workspace_write_and_patch_dry_runs_never_touch_disk(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    existing_empty = repo_dir / "existing-empty"
    existing_empty.mkdir()
    readme = repo_dir / "README.md"
    before = readme.stat()

    write = run(
        service.write_file(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceWriteFileRequest(
                path="existing-empty/write.txt",
                content="content\n",
                dry_run=True,
            ),
        )
    )
    assert write.written is False
    assert write.changed_files[0].operation == "added"
    assert existing_empty.is_dir()
    assert not (existing_empty / "write.txt").exists()

    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: README.md\n"
        "@@\n"
        "-before\n"
        "+after\n"
        "*** Add File: existing-empty/patch.txt\n"
        "+new\n"
        "*** End Patch\n"
    )
    patch_result = run(
        service.apply_patch(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceApplyPatchRequest(patch=patch_text, dry_run=True),
        )
    )
    assert patch_result.applied is False
    assert {item.path for item in patch_result.changed_files} == {"README.md", "existing-empty/patch.txt"}
    assert readme.read_text(encoding="utf-8") == "before\n"
    assert existing_empty.is_dir()
    assert not (existing_empty / "patch.txt").exists()
    after = readme.stat()
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ino == before.st_ino


def test_workspace_patch_context_failure_is_precomputed_before_writes(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    readme = repo_dir / "README.md"
    before = readme.stat()
    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: README.md\n"
        "@@\n"
        "-before\n"
        "+first-change\n"
        "*** Update File: README.md\n"
        "@@\n"
        "-missing\n"
        "+second-change\n"
        "*** End Patch\n"
    )

    with pytest.raises(ApiError) as exc:
        run(
            service.apply_patch(
                "acme",
                "demo",
                prepared.workspace_id,
                WorkspaceApplyPatchRequest(patch=patch_text),
            )
        )

    assert exc.value.error_code == ErrorCode.WORKSPACE_PATCH_CONTEXT_MISMATCH
    assert readme.read_text(encoding="utf-8") == "before\n"
    after = readme.stat()
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ino == before.st_ino


def test_commit_prepared_changes_rolls_back_all_files_on_commit_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    first = repo_dir / "first.txt"
    second = repo_dir / "second.txt"
    first.write_bytes(b"first-before\n")
    second.write_bytes(b"second-before\n")
    changes = [
        PreparedFileChange(path="first.txt", resolved_path=first, before=b"first-before\n", after=b"first-after\n"),
        PreparedFileChange(path="second.txt", resolved_path=second, before=b"second-before\n", after=b"second-after\n"),
    ]
    real_replace = os.replace
    stage_replaces = 0

    def fail_second_stage_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        nonlocal stage_replaces
        if str(source).endswith(".stage"):
            stage_replaces += 1
            if stage_replaces == 2:
                raise OSError("injected stage replace failure")
        real_replace(source, destination)

    monkeypatch.setattr("app.workspace.text_ops.os.replace", fail_second_stage_replace)
    with pytest.raises(OSError, match="injected stage replace failure"):
        commit_prepared_changes(repo_dir, changes)

    assert first.read_bytes() == b"first-before\n"
    assert second.read_bytes() == b"second-before\n"
    assert not list(repo_dir.glob(".*.stage"))
    assert not list(repo_dir.glob(".*.backup"))


def test_workspace_read_files_returns_next_start_line(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task")))

    response = run(
        service.read_files(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceReadFilesRequest(paths=["README.md"], start_line=1, max_lines=1),
        )
    )

    assert response.files[0].truncated is False
    assert response.files[0].next_start_line is None

    repo_dir = manager.repo_dir(prepared.workspace_id)
    (repo_dir / "README.md").write_text("one\ntwo\nthree\n", encoding="utf-8")
    truncated = run(
        service.read_files(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceReadFilesRequest(paths=["README.md"], start_line=1, max_lines=2),
        )
    )
    assert truncated.files[0].truncated is True
    assert truncated.files[0].next_start_line == 3


def test_partial_stage_write_is_cleaned_from_git_transaction_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, repo_dir = make_local_repo(tmp_path)
    target = repo_dir / "first.txt"
    target.write_bytes(b"before\n")
    change = PreparedFileChange(
        path="first.txt",
        resolved_path=target,
        before=b"before\n",
        after=b"after\n",
    )
    transaction_parent = repo_dir / ".git" / "gpt-workspace-transactions"
    real_write_bytes = Path.write_bytes

    def partial_then_fail(path: Path, data: bytes) -> int:
        if path.suffix == ".stage":
            with path.open("wb") as handle:
                handle.write(data[:2])
            raise OSError("injected stage write failure")
        return real_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", partial_then_fail)
    with pytest.raises(OSError, match="injected stage write failure"):
        commit_prepared_changes(repo_dir, [change])

    assert target.read_bytes() == b"before\n"
    assert not transaction_parent.exists()
    assert git("status", "--porcelain", cwd=repo_dir) == "?? first.txt"


def test_transaction_cleanup_failure_rolls_back_workspace_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, repo_dir = make_local_repo(tmp_path)
    target = repo_dir / "README.md"
    before_bytes = target.read_bytes()
    change = PreparedFileChange(
        path="README.md",
        resolved_path=target,
        before=before_bytes,
        after=b"after\n",
    )
    transaction_parent = repo_dir / ".git" / "gpt-workspace-transactions"
    real_rmtree = shutil.rmtree
    calls = 0

    def fail_once(path: str | os.PathLike[str], *args, **kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("injected cleanup failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("app.workspace.text_ops.shutil.rmtree", fail_once)
    with pytest.raises(OSError, match="committed changes were rolled back"):
        commit_prepared_changes(repo_dir, [change])

    assert target.read_bytes() == before_bytes
    assert not transaction_parent.exists()
    assert git("status", "--porcelain", cwd=repo_dir) == ""


def test_write_response_matches_final_git_state_relative_to_head(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    readme = repo_dir / "README.md"
    readme.write_text("dirty\n", encoding="utf-8")

    response = run(
        service.write_file(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceWriteFileRequest(
                path="README.md",
                content="before\n",
                mode="overwrite",
            ),
        )
    )

    assert response.changed_files == []
    assert response.diff_stat == ""
    assert git("status", "--porcelain", cwd=repo_dir) == ""

    scratch = repo_dir / "scratch.txt"
    scratch.write_text("existing untracked\n", encoding="utf-8")
    untracked = run(
        service.write_file(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceWriteFileRequest(
                path="scratch.txt",
                content="updated untracked\n",
                mode="overwrite",
                dry_run=True,
            ),
        )
    )
    assert len(untracked.changed_files) == 1
    assert untracked.changed_files[0].operation == "added"
    assert scratch.read_text(encoding="utf-8") == "existing untracked\n"


def test_long_single_line_continuation_retries_current_line(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    (repo_dir / "README.md").write_text("abcdefghij\nsecond\n", encoding="utf-8")

    result = service._read_file_content(
        repo_dir,
        "README.md",
        start_line=1,
        max_lines=2,
        max_bytes=6,
    )

    assert result.content == ""
    assert result.end_line is None
    assert result.truncated is True
    assert result.next_start_line == 1


def test_newline_only_change_has_nonzero_git_relative_counts(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", prepare_request(branch="gpt/task")))

    response = run(
        service.write_file(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceWriteFileRequest(
                path="README.md",
                content="before",
                mode="overwrite",
                dry_run=True,
            ),
        )
    )

    assert len(response.changed_files) == 1
    assert response.changed_files[0].additions == 1
    assert response.changed_files[0].deletions == 1

