from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
from app.models.ci import SyncedRunArtifact, SyncRunArtifactsToWorkspaceRequest, SyncRunArtifactsToWorkspaceResponse
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
    WorkspacePrepareDiagnostics,
    WorkspaceResetRequest,
    WorkspaceResetResponse,
    WorkspaceStatusRequest,
    WorkspaceStatusResponse,
    WorkspaceWriteFileRequest,
    WorkspaceWriteFileResponse,
)
from app.policy.rules import Policy
from app.storage.audit import AuditStore, canonical_hash
from app.workspace.exec import PwshExecutor
from app.workspace.manager import WorkspaceManager, command_hash
from app.workspace.text_ops import (
    apply_text_patch,
    assert_payload_size,
    assert_text_bytes,
    normalize_line_endings,
    parse_codex_patch,
    restore_files,
    sha256_hex,
    snapshot_files,
    validate_write_target,
)

_ARTIFACTS_ROOT = ".gpt-artifacts"
_ARTIFACTS_EXCLUDE_ENTRY = ".gpt-artifacts/"
_ARTIFACT_PAGE_SIZE = 100
_SAFE_ARTIFACT_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class WorkspaceService:
    def __init__(self, github: GitHubClient, policy: Policy, settings: Settings, manager: WorkspaceManager, audit: AuditStore) -> None:
        self.github = github
        self.policy = policy
        self.settings = settings
        self.manager = manager
        self.audit = audit
        self.executor = PwshExecutor(settings)

    async def prepare(self, owner: str, repo: str, request: PrepareWorkspaceRequest) -> PrepareWorkspaceResponse:
        result = await self.manager.prepare(
            owner=owner,
            repo=repo,
            branch=request.branch,
            source_pr_number=request.source_pr_number,
            base_ref=request.base_ref,
            workspace_id=request.workspace_id,
            refresh=request.refresh,
            clean=request.clean,
        )
        self._audit(
            operation_id="prepareWorkspace",
            owner=owner,
            repo=repo,
            workspace_id=result.meta.workspace_id,
            branch=result.meta.branch,
            head_sha_after=result.meta.head_sha,
            metadata=self._prepare_metadata(result),
        )
        return self._response_from_prepare_result(owner, repo, result)


    async def exec_pwsh(self, owner: str, repo: str, workspace_id: str, request: WorkspaceExecPwshRequest) -> WorkspaceExecPwshResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        if request.timeout_seconds is not None and request.timeout_seconds > self.settings.workspace_max_timeout_seconds:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "workspaceExecPwsh timeout_seconds exceeds the configured GPT Actions safety limit.",
                status_code=422,
                suggestion="Use a shorter timeout, split the work into smaller commands, or dispatch a workflow and inspect CI logs/artifacts.",
                details={"requested_timeout_seconds": request.timeout_seconds, "max_timeout_seconds": self.settings.workspace_max_timeout_seconds},
            )
        timeout = min(request.timeout_seconds or self.settings.workspace_default_timeout_seconds, self.settings.workspace_max_timeout_seconds)
        max_output = min(request.max_output_bytes or self.settings.workspace_max_output_bytes, self.settings.workspace_max_output_bytes)
        repo_dir = self.manager.repo_dir(workspace_id)
        with self.manager.lock(workspace_id):
            result = await self.executor.execute(
                repo_dir,
                script=request.script,
                timeout_seconds=timeout,
                max_output_bytes=max_output,
                allow_network=request.allow_network,
                plain_output=request.plain_output,
                utf8_output=request.utf8_output,
                activate_python_venv=self.settings.workspace_python_auto_activate
                and self.manager.should_use_python_venv(writable=meta.writable),
                python_venv_dir=self.settings.workspace_python_venv_dir,
            )
        self._audit(
            operation_id="workspaceExecPwsh",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=meta.branch,
            head_sha_before=meta.head_sha,
            command_hash=command_hash(request.script),
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
        )
        return WorkspaceExecPwshResponse(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            truncated=result.truncated,
            duration_ms=result.duration_ms,
        )

    async def status(self, owner: str, repo: str, workspace_id: str, request: WorkspaceStatusRequest) -> WorkspaceStatusResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        repo_dir = self.manager.repo_dir(workspace_id)
        with self.manager.lock(workspace_id):
            if request.refresh:
                await self.manager.fetch_branch(repo_dir, meta.branch)
            head_sha = await self.manager.head_sha(repo_dir)
            remote_head = await self.manager.remote_head_sha(repo_dir, meta.branch)
            changed, untracked, conflicts = await self.manager.changed_files(repo_dir)
            ahead, behind = await self.manager.ahead_behind(repo_dir, meta.branch)
        return WorkspaceStatusResponse(
            workspace_id=workspace_id,
            branch=meta.branch,
            head_sha=head_sha,
            remote_head_sha=remote_head,
            dirty=bool(changed),
            ahead=ahead,
            behind=behind,
            changed_files=changed,
            untracked_files=untracked,
            conflicts=conflicts,
        )

    async def diff(self, owner: str, repo: str, workspace_id: str, request: WorkspaceDiffRequest) -> WorkspaceDiffResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        repo_dir = self.manager.repo_dir(workspace_id)
        max_bytes = min(request.max_bytes or self.settings.workspace_max_diff_bytes, self.settings.workspace_max_diff_bytes)
        with self.manager.lock(workspace_id):
            diff_text, truncated = await self.manager.diff_text(repo_dir, paths=request.paths, stat_only=request.stat_only, max_bytes=max_bytes)
            diff_stat = diff_text if request.stat_only else await self.manager.diff_stat(repo_dir)
        self._audit(
            operation_id="workspaceDiff",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=meta.branch,
            head_sha_before=meta.head_sha,
        )
        return WorkspaceDiffResponse(workspace_id=workspace_id, diff=diff_text, diff_stat=diff_stat, truncated=truncated)

    async def apply_patch(self, owner: str, repo: str, workspace_id: str, request: WorkspaceApplyPatchRequest) -> WorkspaceApplyPatchResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        max_patch_bytes = min(request.max_patch_bytes or self.settings.workspace_max_patch_bytes, self.settings.workspace_max_patch_bytes)
        patch_bytes = request.patch.encode("utf-8")
        assert_payload_size(patch_bytes, max_bytes=max_patch_bytes, error_code=ErrorCode.WORKSPACE_PATCH_TOO_LARGE, label="Patch")
        max_changed_files = min(request.max_changed_files or self.settings.workspace_max_changed_files, self.settings.workspace_max_changed_files)
        repo_dir = self.manager.repo_dir(workspace_id)
        with self.manager.lock(workspace_id):
            operations = parse_codex_patch(request.patch, self.policy, repo_dir, allow_delete=request.allow_delete, max_changed_files=max_changed_files)
            target_paths = list(dict.fromkeys(item.path for item in operations))
            snapshots = snapshot_files(repo_dir, target_paths)
            should_restore = True
            try:
                apply_text_patch(repo_dir, operations)
                changed = await self.manager.changed_files_for_paths(repo_dir, target_paths)
                if len(changed) > max_changed_files:
                    raise ApiError(ErrorCode.WORKSPACE_TOO_MANY_CHANGED_FILES, "Patch changes too many files.", status_code=413, details={"count": len(changed), "max": max_changed_files})
                await self.manager.validate_changed_paths(repo_dir, changed)
                diff_stat = await self.manager.diff_stat_for_paths(repo_dir, target_paths)
                response = WorkspaceApplyPatchResponse(
                    applied=not request.dry_run,
                    dry_run=request.dry_run,
                    changed_files=changed,
                    diff_stat=diff_stat,
                )
                should_restore = request.dry_run
            except Exception:
                restore_files(repo_dir, snapshots)
                raise
            finally:
                if should_restore:
                    restore_files(repo_dir, snapshots)
        self._audit(
            operation_id="workspaceApplyPatch",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=meta.branch,
            head_sha_before=meta.head_sha,
            changed_files=[item.model_dump() for item in response.changed_files],
            command_hash=command_hash(request.patch),
        )
        return response

    async def write_file(self, owner: str, repo: str, workspace_id: str, request: WorkspaceWriteFileRequest) -> WorkspaceWriteFileResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        repo_dir = self.manager.repo_dir(workspace_id)
        max_bytes = min(request.max_bytes or self.settings.workspace_max_write_bytes, self.settings.workspace_max_write_bytes)
        with self.manager.lock(workspace_id):
            path, resolved = validate_write_target(self.policy, repo_dir, request.path, operation="modified", error_code=ErrorCode.WORKSPACE_WRITE_INVALID_PATH)
            previous_bytes: bytes | None = None
            if resolved.exists():
                if not resolved.is_file():
                    raise ApiError(ErrorCode.WORKSPACE_WRITE_INVALID_PATH, "Target path exists but is not a file.", status_code=400, details={"path": path})
                previous_bytes = resolved.read_bytes()
                assert_text_bytes(previous_bytes, path=path)
                previous_sha = sha256_hex(previous_bytes)
            else:
                previous_sha = None

            if request.mode == "create_only" and previous_bytes is not None:
                raise ApiError(ErrorCode.WORKSPACE_FILE_EXISTS, "File already exists; create_only refused to overwrite it.", status_code=409, details={"path": path})
            if request.mode == "overwrite_if_sha256_matches":
                if previous_bytes is None:
                    raise ApiError(ErrorCode.WORKSPACE_FILE_NOT_FOUND, "File does not exist; cannot verify expected_sha256.", status_code=404, details={"path": path})
                if not request.expected_sha256:
                    raise ApiError(ErrorCode.VALIDATION_ERROR, "expected_sha256 is required for overwrite_if_sha256_matches.", status_code=422, details={"path": path})
                if previous_sha != request.expected_sha256:
                    raise ApiError(ErrorCode.WORKSPACE_SHA_MISMATCH, "Current file SHA-256 does not match expected_sha256.", status_code=409, details={"path": path, "expected_sha256": request.expected_sha256, "actual_sha256": previous_sha})

            normalized_content = normalize_line_endings(request.content, line_ending=request.line_ending, previous_bytes=previous_bytes)
            data = normalized_content.encode("utf-8")
            assert_text_bytes(data, path=path)
            assert_payload_size(data, max_bytes=max_bytes, error_code=ErrorCode.WORKSPACE_CONTENT_TOO_LARGE, label="Content")
            new_sha = sha256_hex(data)
            if previous_bytes is None:
                operation = "added"
            elif previous_bytes == data:
                operation = "unchanged"
            else:
                operation = "modified"

            changed = []
            diff_stat = ""
            if operation != "unchanged":
                snapshots = snapshot_files(repo_dir, [path])
                should_restore = True
                try:
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                    resolved.write_bytes(data)
                    changed = await self.manager.changed_files_for_paths(repo_dir, [path])
                    if len(changed) > 1:
                        raise ApiError(ErrorCode.WORKSPACE_TOO_MANY_CHANGED_FILES, "write-file unexpectedly changed more than one file.", status_code=409, details={"count": len(changed)})
                    await self.manager.validate_changed_paths(repo_dir, changed)
                    diff_stat = await self.manager.diff_stat_for_paths(repo_dir, [path])
                    should_restore = request.dry_run
                except Exception as exc:
                    restore_files(repo_dir, snapshots)
                    if isinstance(exc, ApiError):
                        raise
                    raise ApiError(ErrorCode.WORKSPACE_WRITE_FAILED, "Failed to write workspace file.", status_code=500, details={"path": path, "error": str(exc)}) from exc
                finally:
                    if should_restore:
                        restore_files(repo_dir, snapshots)
            response = WorkspaceWriteFileResponse(
                written=bool(operation != "unchanged" and not request.dry_run),
                dry_run=request.dry_run,
                path=path,
                operation=operation,
                previous_sha256=previous_sha,
                new_sha256=new_sha,
                bytes=len(data),
                changed_files=changed,
                diff_stat=diff_stat,
            )
        self._audit(
            operation_id="workspaceWriteFile",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=meta.branch,
            head_sha_before=meta.head_sha,
            changed_files=[item.model_dump() for item in response.changed_files],
            command_hash=command_hash(path + "\n" + new_sha),
        )
        return response

    async def commit_and_push(self, owner: str, repo: str, workspace_id: str, request: WorkspaceCommitAndPushRequest) -> WorkspaceCommitAndPushResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        if not meta.writable:
            raise ApiError(
                ErrorCode.WORKSPACE_POLICY_VIOLATION,
                "Cannot commit and push from a read-only base_ref workspace.",
                status_code=403,
                details={"workspace_id": workspace_id, "branch": meta.branch},
            )
        self.policy.assert_write_branch_allowed(request.branch)
        if request.branch != meta.branch:
            raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Commit branch must match prepared workspace branch.", status_code=403, details={"workspace_branch": meta.branch, "request_branch": request.branch})
        scope = f"{owner}/{repo}:{workspace_id}:commit_and_push"
        payload = request.model_dump()
        if request.idempotency_key:
            cached = self.audit.get_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload)
            if cached:
                return WorkspaceCommitAndPushResponse(**cached)

        repo_dir = self.manager.repo_dir(workspace_id)
        with self.manager.lock(workspace_id):
            await self.manager.fetch_branch(repo_dir, request.branch)
            remote_head = await self.manager.remote_head_sha(repo_dir, request.branch)
            if remote_head != request.expected_head_sha:
                raise ApiError(
                    ErrorCode.WORKSPACE_HEAD_CHANGED,
                    "Remote branch head changed before commit.",
                    status_code=409,
                    suggestion="Refresh the workspace and retry with the latest expected_head_sha.",
                    details={"expected_head_sha": request.expected_head_sha, "actual_head_sha": remote_head, "branch": request.branch},
                )
            current_branch = await self.manager.current_branch(repo_dir)
            if current_branch != request.branch:
                raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Current workspace checkout is not the requested branch.", status_code=409, details={"current_branch": current_branch, "request_branch": request.branch})
            current_head = await self.manager.head_sha(repo_dir)
            changed, _, conflicts = await self.manager.changed_files(repo_dir)
            if conflicts:
                raise ApiError(ErrorCode.WORKSPACE_DIRTY, "Workspace has unresolved conflicts.", status_code=409, details={"conflicts": conflicts})
            if not changed and current_head != remote_head:
                if not await self.manager.is_ancestor(repo_dir, str(remote_head), current_head):
                    raise ApiError(
                        ErrorCode.WORKSPACE_HEAD_CHANGED,
                        "Workspace has an unpushed local commit that is not based on the expected remote head.",
                        status_code=409,
                        suggestion="Reset or refresh the workspace before retrying the push.",
                        details={"expected_head_sha": request.expected_head_sha, "remote_head_sha": remote_head, "local_head_sha": current_head},
                    )
                committed = await self.manager.committed_changed_files_between(repo_dir, str(remote_head), current_head)
                await self.manager.validate_changed_paths(repo_dir, committed)
                diff_stat = await self.manager.diff_stat_between(repo_dir, str(remote_head), current_head)
                if request.dry_run:
                    response = WorkspaceCommitAndPushResponse(
                        previous_head_sha=str(remote_head),
                        new_head_sha=current_head,
                        commit_sha=current_head,
                        commit_url=None,
                        changed_files=committed,
                        diff_stat=diff_stat,
                        pushed=False,
                        dry_run=True,
                    )
                else:
                    auth_config = await self.github.git_auth_config()
                    push = await self.manager.git.run(["git", *auth_config, "push", "origin", f"HEAD:{request.branch}"], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds, check=False, allowed_exit_codes=(0,), max_output_bytes=self.settings.workspace_max_output_bytes)
                    if push.exit_code != 0:
                        raise ApiError(ErrorCode.WORKSPACE_PUSH_FAILED, "Git push failed; local commit was not force-pushed.", status_code=502, details={"stdout": push.stdout, "stderr": push.stderr})
                    meta.head_sha = current_head
                    from app.workspace.models import save_meta

                    save_meta(self.manager.workspace_dir(workspace_id), meta)
                    response = WorkspaceCommitAndPushResponse(
                        previous_head_sha=str(remote_head),
                        new_head_sha=current_head,
                        commit_sha=current_head,
                        commit_url=f"https://github.com/{owner}/{repo}/commit/{current_head}",
                        changed_files=committed,
                        diff_stat=diff_stat,
                        pushed=True,
                        dry_run=False,
                    )
            elif current_head != remote_head:
                raise ApiError(
                    ErrorCode.WORKSPACE_DIRTY,
                    "Workspace has unpushed local commits and additional uncommitted changes.",
                    status_code=409,
                    suggestion="Retry commitAndPush before making more edits, or reset the workspace to the remote head.",
                    details={"expected_head_sha": request.expected_head_sha, "remote_head_sha": remote_head, "local_head_sha": current_head},
                )
            if not changed:
                if current_head == remote_head:
                    raise ApiError(ErrorCode.WORKSPACE_NO_CHANGES, "Workspace has no changes to commit.", status_code=409)
            else:
                await self.manager.validate_changed_paths(repo_dir, changed)
                paths = [self.policy.assert_workspace_path_allowed(path) for path in request.paths]
                await self.manager.git.run(["git", "add", "--", *paths], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds)
                staged = await self.manager.staged_changed_files(repo_dir)
                if not staged:
                    await self.manager.git.run(["git", "reset", "--mixed"], cwd=repo_dir)
                    raise ApiError(ErrorCode.WORKSPACE_NO_CHANGES, "Selected paths have no staged changes.", status_code=409)
                await self.manager.validate_changed_paths(repo_dir, staged)
                diff_stat_result = await self.manager.git.run(["git", "diff", "--cached", "--stat"], cwd=repo_dir)
                diff_stat = diff_stat_result.stdout.strip()
                previous_head = current_head
                if request.dry_run:
                    await self.manager.git.run(["git", "reset", "--mixed"], cwd=repo_dir)
                    response = WorkspaceCommitAndPushResponse(
                        previous_head_sha=previous_head,
                        new_head_sha=previous_head,
                        commit_sha=None,
                        commit_url=None,
                        changed_files=staged,
                        diff_stat=diff_stat,
                        pushed=False,
                        dry_run=True,
                    )
                else:
                    await self.manager.git.run(["git", "config", "user.name", self.settings.workspace_git_user_name], cwd=repo_dir)
                    await self.manager.git.run(["git", "config", "user.email", self.settings.workspace_git_user_email], cwd=repo_dir)
                    await self.manager.git.run(["git", "commit", "-m", request.commit_message], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds)
                    new_head = await self.manager.head_sha(repo_dir)
                    auth_config = await self.github.git_auth_config()
                    push = await self.manager.git.run(["git", *auth_config, "push", "origin", f"HEAD:{request.branch}"], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds, check=False, allowed_exit_codes=(0,), max_output_bytes=self.settings.workspace_max_output_bytes)
                    if push.exit_code != 0:
                        raise ApiError(ErrorCode.WORKSPACE_PUSH_FAILED, "Git push failed; local commit was not force-pushed.", status_code=502, details={"stdout": push.stdout, "stderr": push.stderr})
                    meta.head_sha = new_head
                    from app.workspace.models import save_meta

                    save_meta(self.manager.workspace_dir(workspace_id), meta)
                    response = WorkspaceCommitAndPushResponse(
                        previous_head_sha=previous_head,
                        new_head_sha=new_head,
                        commit_sha=new_head,
                        commit_url=f"https://github.com/{owner}/{repo}/commit/{new_head}",
                        changed_files=staged,
                        diff_stat=diff_stat,
                        pushed=True,
                        dry_run=False,
                    )
        self._audit(
            operation_id="workspaceCommitAndPush",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=request.branch,
            head_sha_before=request.expected_head_sha,
            head_sha_after=response.new_head_sha,
            changed_files=[item.model_dump() for item in response.changed_files],
        )
        if request.idempotency_key:
            self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload, response_payload=response.model_dump())
        return response

    async def reset(self, owner: str, repo: str, workspace_id: str, request: WorkspaceResetRequest) -> WorkspaceResetResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        if request.branch != meta.branch:
            raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Reset branch must match prepared workspace branch.", status_code=403, details={"workspace_branch": meta.branch, "request_branch": request.branch})
        repo_dir = self.manager.repo_dir(workspace_id)
        with self.manager.lock(workspace_id):
            removed = await self.manager.reset_to_remote(repo_dir, request.branch, clean_untracked=request.clean_untracked)
            head_sha = await self.manager.head_sha(repo_dir)
        self._audit(
            operation_id="workspaceReset",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=request.branch,
            head_sha_after=head_sha,
        )
        return WorkspaceResetResponse(workspace_id=workspace_id, branch=request.branch, head_sha=head_sha, removed_untracked_files=removed)

    async def sync_run_artifacts_to_workspace(
        self,
        owner: str,
        repo: str,
        workspace_id: str,
        request: SyncRunArtifactsToWorkspaceRequest,
    ) -> SyncRunArtifactsToWorkspaceResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        raw_run = await self.github.get_workflow_run(owner, repo, request.run_id)
        if raw_run.get("status") != "completed":
            raise ApiError(
                ErrorCode.CI_LOG_NOT_READY,
                "Workflow run has not completed; artifacts may still be changing.",
                status_code=409,
                details={"run_id": request.run_id, "status": raw_run.get("status"), "conclusion": raw_run.get("conclusion")},
            )

        raw_artifacts, total_count = await self._list_all_run_artifacts(owner, repo, request.run_id)
        remote_artifacts = _artifact_manifest_records(raw_artifacts)
        remote_fingerprint = canonical_hash({"run_id": request.run_id, "artifacts": _artifact_fingerprint_inputs(remote_artifacts)})

        repo_dir = self.manager.repo_dir(workspace_id)
        target_dir = repo_dir / _ARTIFACTS_ROOT / "runs" / str(request.run_id)
        manifest_path = target_dir / "manifest.json"
        target_dir_rel = _relative_repo_path(repo_dir, target_dir)
        manifest_path_rel = _relative_repo_path(repo_dir, manifest_path)
        gitignore_path_rel = ".git/info/exclude"

        with self.manager.lock(workspace_id):
            gitignore_updated = _ensure_gpt_artifacts_local_exclude(repo_dir)
            existing_manifest = _read_artifact_manifest(manifest_path)
            skipped = _manifest_is_current(repo_dir, existing_manifest, remote_fingerprint)
            if skipped:
                artifacts = [SyncedRunArtifact(**item) for item in existing_manifest.get("artifacts", [])]
            else:
                _remove_existing_artifact_target(target_dir)
                target_dir.mkdir(parents=True, exist_ok=True)
                artifacts = []
                for item in remote_artifacts:
                    artifact_id = int(item["artifact_id"])
                    name = str(item["name"])
                    destination = target_dir / f"{artifact_id}-{_safe_artifact_name(name)}"
                    archive_data = await self.github.download_artifact(owner, repo, artifact_id)
                    _verify_artifact_digest(archive_data, str(item["digest"]))
                    file_count, bytes_written = _extract_artifact_archive(archive_data, destination)
                    artifacts.append(
                        SyncedRunArtifact(
                            artifact_id=artifact_id,
                            name=name,
                            digest=str(item["digest"]),
                            destination_dir=_relative_repo_path(repo_dir, destination),
                            file_count=file_count,
                            bytes_written=bytes_written,
                        )
                    )
                _write_artifact_manifest(
                    manifest_path,
                    {
                        "run_id": request.run_id,
                        "run_attempt": raw_run.get("run_attempt"),
                        "workflow_name": raw_run.get("name"),
                        "head_branch": raw_run.get("head_branch"),
                        "head_sha": raw_run.get("head_sha"),
                        "status": raw_run.get("status"),
                        "conclusion": raw_run.get("conclusion"),
                        "run_url": raw_run.get("html_url"),
                        "remote_fingerprint": remote_fingerprint,
                        "remote_artifacts": remote_artifacts,
                        "artifacts": [item.model_dump() for item in artifacts],
                        "synced_at": _utc_now_iso(),
                    },
                )
        response = SyncRunArtifactsToWorkspaceResponse(
            workspace_id=workspace_id,
            run_id=request.run_id,
            run_attempt=raw_run.get("run_attempt"),
            target_dir=target_dir_rel,
            manifest_path=manifest_path_rel,
            remote_fingerprint=remote_fingerprint,
            downloaded=not skipped,
            skipped=skipped,
            gitignore_path=gitignore_path_rel,
            gitignore_updated=gitignore_updated,
            artifacts=artifacts,
            total_count=total_count,
        )
        self._audit(
            operation_id="syncRunArtifactsToWorkspace",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=meta.branch,
            head_sha_before=meta.head_sha,
            metadata={
                "run_id": request.run_id,
                "remote_fingerprint": remote_fingerprint,
                "downloaded": response.downloaded,
                "skipped": response.skipped,
                "artifact_count": len(artifacts),
            },
        )
        return response

    async def _list_all_run_artifacts(self, owner: str, repo: str, run_id: int) -> tuple[list[dict[str, Any]], int]:
        artifacts: list[dict[str, Any]] = []
        total_count = 0
        page = 1
        while True:
            payload = await self.github.list_artifacts_for_run(owner, repo, run_id, per_page=_ARTIFACT_PAGE_SIZE, page=page)
            if page == 1:
                total_count = int(payload.get("total_count") or 0)
            page_items = payload.get("artifacts", [])
            artifacts.extend(page_items)
            if not page_items or len(artifacts) >= total_count or len(page_items) < _ARTIFACT_PAGE_SIZE:
                break
            page += 1
        return artifacts, total_count or len(artifacts)

    def _assert_workspace(self, owner: str, repo: str, workspace_id: str):
        self.policy.assert_repo_allowed(owner, repo)
        meta = self.manager.get_meta(workspace_id)
        if meta.owner != owner or meta.repo != repo:
            raise ApiError(ErrorCode.WORKSPACE_NOT_FOUND, "Workspace was not found for this repository.", status_code=404, details={"workspace_id": workspace_id})
        return meta

    def _audit(self, **kwargs) -> None:
        try:
            self.audit.record_workspace_operation(**kwargs)
        except Exception:
            pass

    @staticmethod
    def _diagnostics_model(result) -> WorkspacePrepareDiagnostics:
        return WorkspacePrepareDiagnostics(
            mirror_stage=result.mirror.stage,
            mirror_duration_ms=result.mirror.duration_ms,
            mirror_pack_bytes=result.mirror.pack_bytes,
            mirror_pack_files=result.mirror.pack_files,
            workspace_stage=result.workspace_stage,
            workspace_duration_ms=result.workspace_duration_ms,
            total_duration_ms=result.total_duration_ms,
        )

    def _response_from_prepare_result(self, owner: str, repo: str, result) -> PrepareWorkspaceResponse:
        return PrepareWorkspaceResponse(
            workspace_id=result.meta.workspace_id,
            owner=owner,
            repo=repo,
            branch=result.meta.branch,
            source_pr_number=result.meta.source_pr_number,
            head_sha=result.meta.head_sha,
            default_branch=result.meta.default_branch,
            created=result.created,
            refreshed=result.refreshed,
            diagnostics=self._diagnostics_model(result),
        )

    @staticmethod
    def _prepare_metadata(result) -> dict:
        return {
            "created": result.created,
            "refreshed": result.refreshed,
            "mirror": asdict(result.mirror),
            "workspace_stage": result.workspace_stage,
            "workspace_duration_ms": result.workspace_duration_ms,
            "total_duration_ms": result.total_duration_ms,
        }


def _artifact_manifest_records(raw_artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    missing_digest: list[dict[str, Any]] = []
    for raw in raw_artifacts:
        artifact_id = raw.get("id")
        digest = raw.get("digest")
        name = str(raw.get("name") or "")
        if artifact_id is None:
            raise ApiError(ErrorCode.GITHUB_ERROR, "GitHub artifact payload is missing id.", status_code=502, details={"artifact": raw})
        if not isinstance(digest, str) or not digest.strip():
            missing_digest.append({"artifact_id": artifact_id, "name": name})
            continue
        artifacts.append(
            {
                "artifact_id": int(artifact_id),
                "name": name,
                "digest": digest.strip(),
                "size_in_bytes": raw.get("size_in_bytes"),
                "created_at": raw.get("created_at"),
                "expires_at": raw.get("expires_at"),
                "updated_at": raw.get("updated_at"),
            }
        )
    if missing_digest:
        raise ApiError(
            ErrorCode.GITHUB_ERROR,
            "GitHub artifact metadata did not include digest, so the gateway refused to sync it safely. Use getRunLog/job logs instead, or enable an explicit unsafe artifact sync mode after review.",
            status_code=502,
            details={"missing_artifacts": missing_digest},
        )
    return sorted(artifacts, key=lambda item: item["artifact_id"])


def _artifact_fingerprint_inputs(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "artifact_id": item["artifact_id"],
            "name": item["name"],
            "digest": item["digest"],
        }
        for item in artifacts
    ]


def _ensure_gpt_artifacts_local_exclude(repo_dir: Path) -> bool:
    exclude = repo_dir / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
    if _exclude_has_gpt_artifacts_entry(text):
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    if text.strip():
        text += "\n"
    text += "# GPT Actions Gateway synced artifacts\n" + _ARTIFACTS_EXCLUDE_ENTRY + "\n"
    exclude.write_text(text, encoding="utf-8")
    return True


def _exclude_has_gpt_artifacts_entry(text: str) -> bool:
    accepted = {
        ".gpt-artifacts",
        ".gpt-artifacts/",
        ".gpt-artifacts/**",
        "/.gpt-artifacts",
        "/.gpt-artifacts/",
        "/.gpt-artifacts/**",
    }
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry or entry.startswith("!"):
            continue
        if entry in accepted:
            return True
    return False


def _read_artifact_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Artifact manifest path exists but is not a file.", status_code=409)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Artifact manifest is not valid JSON.", status_code=409) from exc
    if not isinstance(data, dict):
        raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Artifact manifest JSON must be an object.", status_code=409)
    return data


def _manifest_is_current(repo_dir: Path, manifest: dict[str, Any] | None, remote_fingerprint: str) -> bool:
    if not manifest or manifest.get("remote_fingerprint") != remote_fingerprint:
        return False
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        return False
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("destination_dir"), str):
            return False
        if not (repo_dir / artifact["destination_dir"]).exists():
            return False
    return True


def _write_artifact_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _remove_existing_artifact_target(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def _extract_artifact_archive(data: bytes, destination: Path) -> tuple[int, int]:
    file_count = 0
    bytes_written = 0
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                member_path = _safe_zip_member_path(info.filename)
                target = destination.joinpath(*member_path.parts)
                _assert_inside_directory(destination, target)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                file_count += 1
                bytes_written += info.file_size
    except zipfile.BadZipFile as exc:
        raise ApiError(ErrorCode.CI_LOG_NOT_READY, "Artifact archive is not a valid zip file.", status_code=502) from exc
    return file_count, bytes_written


def _verify_artifact_digest(data: bytes, digest: str) -> None:
    algorithm, separator, expected = digest.strip().partition(":")
    if separator != ":" or algorithm.lower() != "sha256" or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise ApiError(
            ErrorCode.GITHUB_ERROR,
            "Unsupported artifact digest format; expected sha256:<64 hex>.",
            status_code=502,
            details={"digest": digest},
        )
    actual = hashlib.sha256(data).hexdigest()
    if actual.lower() != expected.lower():
        raise ApiError(
            ErrorCode.GITHUB_ERROR,
            "Downloaded artifact digest does not match GitHub digest.",
            status_code=502,
            details={"expected_digest": digest, "actual_digest": f"sha256:{actual}"},
        )


def _safe_zip_member_path(filename: str) -> PurePosixPath:
    raw = filename.replace("\\", "/").strip()
    member_path = PurePosixPath(raw)
    if not raw or member_path.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in member_path.parts):
        raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Artifact archive contains an unsafe path.", status_code=403, details={"path": filename})
    return member_path


def _assert_inside_directory(root: Path, candidate: Path) -> None:
    root_resolved = root.resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Artifact archive path escapes its destination directory.", status_code=403) from exc


def _relative_repo_path(repo_dir: Path, path: Path) -> str:
    return path.relative_to(repo_dir).as_posix()


def _safe_artifact_name(name: str) -> str:
    safe = _SAFE_ARTIFACT_NAME_RE.sub("-", name.strip()).strip(".-_")
    return (safe or "artifact")[:80]


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
