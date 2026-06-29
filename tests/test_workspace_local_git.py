from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
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
    WorkspaceExecPwshRequest,
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
    workspace_max_count: int = 50,
    workspace_ttl_hours: int = 48,
    workspace_python_venv_enabled: bool = False,
    workspace_python_venv_python: str | None = None,
) -> tuple[WorkspaceService, WorkspaceManager]:
    settings = Settings(
        allow_all_repos=allow_all_repos,
        allowed_repos=allowed_repos,
        workspace_root=str(tmp_path / "workspaces"),
        workspace_mirror_root=str(tmp_path / "mirrors"),
        audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}",
        allow_workflow_edit=True,
        workspace_max_count=workspace_max_count,
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


def test_exec_pwsh_does_not_collect_workspace_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_exec_fast")))

    async def fail_changed_files(*args, **kwargs):
        raise AssertionError("workspaceExecPwsh must not call changed_files")

    async def fail_diff_stat(*args, **kwargs):
        raise AssertionError("workspaceExecPwsh must not call diff_stat")

    class FakeExecutor:
        async def execute(self, *args, **kwargs):
            return CommandResult(exit_code=0, stdout="ok\n", stderr="", duration_ms=7, truncated=False)

    monkeypatch.setattr(manager, "changed_files", fail_changed_files)
    monkeypatch.setattr(manager, "diff_stat", fail_diff_stat)
    service.executor = FakeExecutor()  # type: ignore[assignment]

    response = run(
        service.exec_pwsh(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceExecPwshRequest(script="Write-Output ok"),
        )
    )

    assert response.model_dump() == {
        "exit_code": 0,
        "stdout": "ok\n",
        "stderr": "",
        "truncated": False,
        "duration_ms": 7,
    }


def test_exec_pwsh_rejects_timeout_above_configured_safety_limit(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_exec_timeout")))

    with pytest.raises(ApiError) as exc:
        run(
            service.exec_pwsh(
                "acme",
                "demo",
                prepared.workspace_id,
                WorkspaceExecPwshRequest(script="Write-Output ok", timeout_seconds=service.settings.workspace_max_timeout_seconds + 1),
            )
        )

    assert exc.value.error_code == ErrorCode.VALIDATION_ERROR
    assert "safety limit" in exc.value.message
    assert exc.value.suggestion is not None and "dispatch a workflow" in exc.value.suggestion


def test_prepare_can_create_or_continue_branch_before_workspace(tmp_path: Path):
    remote, source = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)
    main_sha = git("rev-parse", "main", cwd=source)

    prepared = run(
        service.prepare(
            "acme",
            "demo",
            PrepareWorkspaceRequest(
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


def test_workspace_inspect_search_and_read_files_do_not_need_pwsh(tmp_path: Path):
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
    service, _ = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_inspect")))

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


def test_sync_run_artifacts_to_workspace_downloads_and_skips_unchanged_run(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_artifacts")))
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
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_artifacts_replace")))
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
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_artifacts_no_digest")))
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
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_artifacts_bad_digest")))
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
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_artifacts_digest_mismatch")))
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

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task")))
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

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="feature/task")))
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

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_push_recovery")))
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


def test_workspace_prepare_explicit_ws_id_reports_created_true(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_custom_1")))

    assert prepared.workspace_id == "ws_custom_1"
    assert prepared.created is True
    assert prepared.diagnostics.mirror_stage in {"clone", "fetch"}
    assert prepared.diagnostics.total_duration_ms >= prepared.diagnostics.mirror_duration_ms


def test_workspace_prepare_prunes_expired_workspace_before_count_limit(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote, workspace_max_count=1, workspace_ttl_hours=48)
    expired = manager.workspace_dir("ws_expired")
    expired.mkdir(parents=True)
    old = time.time() - 49 * 60 * 60
    os.utime(expired, (old, old))

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_new")))

    assert prepared.workspace_id == "ws_new"
    assert not expired.exists()


def test_workspace_prune_removes_readonly_git_objects(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    _, manager = make_service(tmp_path, remote, workspace_max_count=3, workspace_ttl_hours=48)
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


def test_workspace_prepare_keeps_fresh_and_locked_workspace_dirs(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote, workspace_max_count=3, workspace_ttl_hours=48)
    fresh = manager.workspace_dir("ws_fresh")
    locked = manager.workspace_dir("ws_locked")
    fresh.mkdir(parents=True)
    locked.mkdir(parents=True)
    (locked / "lock").write_text("busy", encoding="utf-8")
    old = time.time() - 49 * 60 * 60
    os.utime(locked, (old, old))

    run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_new")))

    assert fresh.exists()
    assert locked.exists()


def test_workspace_prune_ignores_non_workspace_dirs_and_mirrors(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    _, manager = make_service(tmp_path, remote, workspace_max_count=3, workspace_ttl_hours=48)
    non_workspace = manager.root / "cache"
    mirror_dir = manager.mirror_root / "acme" / "demo.git"
    non_workspace.mkdir(parents=True)
    mirror_dir.mkdir(parents=True)
    old = time.time() - 49 * 60 * 60
    os.utime(non_workspace, (old, old))
    os.utime(mirror_dir, (old, old))

    assert manager.prune_expired_workspace_dirs() == 0

    assert non_workspace.exists()
    assert mirror_dir.exists()


def test_prepare_work_branch_bootstraps_python_venv_without_committable_diff(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote, workspace_python_venv_enabled=True, workspace_python_venv_python=sys.executable)

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_python")))
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

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="feature/task", workspace_id="ws_python_feature")))
    repo_dir = manager.repo_dir(prepared.workspace_id)

    assert prepared.branch == "feature/task"
    assert (repo_dir / ".venv" / "pyvenv.cfg").exists()
    status = run(service.status("acme", "demo", prepared.workspace_id, WorkspaceStatusRequest()))
    assert status.dirty is False
    assert status.changed_files == []


def test_prepare_base_ref_does_not_bootstrap_python_venv(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote, workspace_python_venv_enabled=True, workspace_python_venv_python=sys.executable)

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(base_ref="main", workspace_id="ws_read_only")))
    repo_dir = manager.repo_dir(prepared.workspace_id)

    assert not (repo_dir / ".venv").exists()
    assert not (repo_dir / ".gitignore").exists()


def test_prepare_arbitrary_base_ref_is_read_only_and_does_not_bootstrap_python_venv(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote, workspace_python_venv_enabled=True, workspace_python_venv_python=sys.executable)

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(base_ref="feature/task", workspace_id="ws_read_only_feature")))
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

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(base_ref="feature/task", workspace_id="ws_legacy_meta")))
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

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_python_no_diff")))
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
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_broken_python")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    broken_venv = repo_dir / ".venv"
    broken_venv.mkdir()
    (broken_venv / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    service.settings.workspace_python_venv_enabled = True
    manager.settings.workspace_python_venv_enabled = True

    with pytest.raises(ApiError) as exc:
        run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_broken_python")))

    assert exc.value.error_code == ErrorCode.WORKSPACE_POLICY_VIOLATION
    assert "interpreter directory is missing" in exc.value.message


def test_prepare_existing_python_venv_without_pyvenv_cfg_fails(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote, workspace_python_venv_enabled=False, workspace_python_venv_python=sys.executable)
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_missing_pyvenv_cfg")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    (repo_dir / ".venv").mkdir()
    service.settings.workspace_python_venv_enabled = True
    manager.settings.workspace_python_venv_enabled = True

    with pytest.raises(ApiError) as exc:
        run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_missing_pyvenv_cfg")))

    assert exc.value.error_code == ErrorCode.WORKSPACE_POLICY_VIOLATION
    assert "pyvenv.cfg is missing" in exc.value.message


def test_split_command_handles_quoted_python_path_with_spaces() -> None:
    parts = split_command(r'"C:\Program Files\Python313\python.exe" -m venv')

    assert parts == [r"C:\Program Files\Python313\python.exe", "-m", "venv"]



def test_workspace_apply_patch_dry_run_and_apply_do_not_push(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task")))
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
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task")))
    patch = "*** Begin Patch\n*** Delete File: README.md\n*** End Patch\n"

    with pytest.raises(ApiError) as exc:
        run(service.apply_patch("acme", "demo", prepared.workspace_id, WorkspaceApplyPatchRequest(patch=patch)))
    assert exc.value.error_code == ErrorCode.WORKSPACE_DELETE_NOT_ALLOWED


def test_workspace_apply_patch_context_mismatch_leaves_file_unchanged(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    patch = "*** Begin Patch\n*** Update File: README.md\n@@\n-missing\n+after\n*** End Patch\n"

    with pytest.raises(ApiError) as exc:
        run(service.apply_patch("acme", "demo", prepared.workspace_id, WorkspaceApplyPatchRequest(patch=patch)))
    assert exc.value.error_code == ErrorCode.WORKSPACE_PATCH_CONTEXT_MISMATCH
    assert (repo_dir / "README.md").read_text(encoding="utf-8") == "before\n"


def test_workspace_write_file_create_only_and_sha_guard(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task")))
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
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task")))

    with pytest.raises(ApiError) as exc:
        run(service.write_file("acme", "demo", prepared.workspace_id, WorkspaceWriteFileRequest(path=".env", content="SECRET=x\n")))
    assert exc.value.error_code == ErrorCode.WORKSPACE_POLICY_VIOLATION
