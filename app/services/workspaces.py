from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
from app.models.branches import CreateWorkBranchRequest, CreateWorkBranchResponse
from app.models.ci import SyncedRunArtifact, SyncRunArtifactsToWorkspaceRequest, SyncRunArtifactsToWorkspaceResponse
from app.models.common import ChangedFile
from app.models.workspaces import (
    PrepareWorkspaceRequest,
    PrepareWorkspaceResponse,
    WorkspaceApplyPatchRequest,
    WorkspaceApplyPatchResponse,
    WorkspaceCommandCancelRequest,
    WorkspaceCommandGetRequest,
    WorkspaceCommandListRequest,
    WorkspaceCommandLogsRequest,
    WorkspaceCommandRequest,
    WorkspaceCommandResponse,
    WorkspaceCommandStartRequest,
    WorkspaceCommandVariant,
    WorkspaceCommitAndPushRequest,
    WorkspaceCommitAndPushResponse,
    WorkspaceDiffRequest,
    WorkspaceDiffResponse,
    WorkspaceFileContent,
    WorkspaceInspectRequest,
    WorkspaceInspectResponse,
    WorkspaceInspectSearchResult,
    WorkspaceOperationSummary,
    WorkspacePrepareDiagnostics,
    WorkspaceReadFilesRequest,
    WorkspaceReadFilesResponse,
    WorkspaceSearchMatch,
    WorkspaceSearchRequest,
    WorkspaceSearchResponse,
    WorkspaceStatusRequest,
    WorkspaceStatusResponse,
    WorkspaceTreeEntry,
    WorkspaceWriteFileRequest,
    WorkspaceWriteFileResponse,
)
from app.policy.rules import Policy
from app.services.branches import BranchService
from app.storage.audit import AuditStore, canonical_hash
from app.workspace.manager import WorkspaceManager, command_hash
from app.workspace.operations import WorkspaceOperationManager
from app.workspace.text_ops import (
    PreparedFileChange,
    assert_payload_size,
    assert_text_bytes,
    commit_prepared_changes,
    describe_prepared_changes,
    normalize_line_endings,
    parse_codex_patch,
    prepare_text_patch,
    prepare_write_change,
    sha256_hex,
    snapshot_files,
    validate_write_target,
)

_ARTIFACTS_ROOT = ".gpt-artifacts"
_ARTIFACTS_EXCLUDE_ENTRY = ".gpt-artifacts/"
_ARTIFACT_PAGE_SIZE = 100
_SAFE_ARTIFACT_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_EXCLUDED_INSPECT_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".tox",
    ".nox",
}
_ALLOWED_ENV_READ_FILES = {".env.example", ".env.sample", ".env.template"}
_MIN_STRUCTURED_RESPONSE_BYTES = 1024
_SEARCH_LINE_MAX_BYTES = 4_000
_SEARCH_SNIPPET_MAX_BYTES = 12_000
_TRUNCATION_MARKER = "\n...[truncated]"


class WorkspaceService:
    def __init__(
        self,
        github: GitHubClient,
        policy: Policy,
        settings: Settings,
        manager: WorkspaceManager,
        audit: AuditStore,
        operations: WorkspaceOperationManager | None = None,
    ) -> None:
        self.github = github
        self.policy = policy
        self.settings = settings
        self.manager = manager
        self.audit = audit
        self.operations = operations

    async def prepare(self, owner: str, repo: str, request: PrepareWorkspaceRequest) -> PrepareWorkspaceResponse:
        scope = f"{owner}/{repo}:prepare_workspace"
        payload = request.model_dump()
        async with self.manager.prepare_idempotency_lock(scope, request.idempotency_key):
            cached = self.audit.get_idempotent_response(
                scope=scope,
                key=request.idempotency_key,
                request_payload=payload,
            )
            if cached:
                return PrepareWorkspaceResponse(**cached)

            branch_result: CreateWorkBranchResponse | None = None
            branch = request.branch
            base_ref = request.base_ref
            source_pr_number = request.source_pr_number

            if request.mode == "create_or_prepare_branch":
                branch_result = await BranchService(self.github, self.policy, self.settings, self.audit).create_work_branch(
                    owner,
                    repo,
                    CreateWorkBranchRequest(
                        idempotency_key=request.idempotency_key,
                        base_ref=request.base_ref,
                        base_sha=request.base_sha,
                        branch=request.branch,
                        purpose_slug=request.purpose_slug,
                        continue_if_exists=request.continue_if_exists,
                    ),
                )
                branch = branch_result.branch
                base_ref = None
                source_pr_number = None

            result = await self.manager.prepare(
                owner=owner,
                repo=repo,
                branch=branch,
                source_pr_number=source_pr_number,
                base_ref=base_ref,
                purpose_slug=request.purpose_slug,
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
            response = self._response_from_prepare_result(owner, repo, result)
            if branch_result is not None:
                response = response.model_copy(
                    update={
                        "branch_created": branch_result.created,
                        "branch_continued": branch_result.continued,
                        "branch_already_exists": branch_result.already_exists,
                        "branch_base_ref": branch_result.base_ref,
                        "branch_base_sha": branch_result.base_sha,
                    }
                )
            self.audit.save_idempotent_response(
                scope=scope,
                key=request.idempotency_key,
                request_payload=payload,
                response_payload=response.model_dump(),
            )
            return response

    async def command(
        self,
        owner: str,
        repo: str,
        workspace_id: str,
        request: WorkspaceCommandRequest | WorkspaceCommandVariant,
    ) -> WorkspaceCommandResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        if self.operations is None:
            raise ApiError(ErrorCode.WORKSPACE_EXEC_FAILED, "Workspace operation manager is unavailable.", status_code=500)

        if isinstance(request, WorkspaceCommandRequest):
            request = request.to_variant()

        if isinstance(request, WorkspaceCommandStartRequest):
            if request.timeout_seconds is not None and request.timeout_seconds > self.settings.workspace_max_timeout_seconds:
                raise ApiError(
                    ErrorCode.VALIDATION_ERROR,
                    "workspaceCommand timeout_seconds exceeds the configured limit.",
                    status_code=422,
                    details={
                        "requested_timeout_seconds": request.timeout_seconds,
                        "max_timeout_seconds": self.settings.workspace_max_timeout_seconds,
                    },
                )
            timeout = min(
                request.timeout_seconds or self.settings.workspace_default_timeout_seconds,
                self.settings.workspace_max_timeout_seconds,
            )
            max_output = min(
                request.max_output_bytes or self.settings.workspace_max_output_bytes,
                self.settings.workspace_max_output_bytes,
            )
            record = await self.operations.start(
                workspace_id=workspace_id,
                repo_dir=self.manager.repo_dir(workspace_id),
                idempotency_key=request.idempotency_key,
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
                operation_id="workspaceCommand",
                owner=owner,
                repo=repo,
                workspace_id=workspace_id,
                branch=meta.branch,
                head_sha_before=meta.head_sha,
                command_hash=command_hash(request.script),
                metadata={"command_operation_id": record["operation_id"], "action": "start"},
            )
            return WorkspaceCommandResponse(
                action="start",
                operation=WorkspaceOperationSummary(**record),
            )
        if isinstance(request, WorkspaceCommandGetRequest):
            record = await self.operations.get(workspace_id, request.operation_id)
            return WorkspaceCommandResponse(action="get", operation=WorkspaceOperationSummary(**record))
        if isinstance(request, WorkspaceCommandCancelRequest):
            record = await self.operations.cancel(workspace_id, request.operation_id)
            return WorkspaceCommandResponse(action="cancel", operation=WorkspaceOperationSummary(**record))
        if isinstance(request, WorkspaceCommandListRequest):
            records = await self.operations.list_operations(workspace_id, request.state)
            return WorkspaceCommandResponse(
                action="list",
                operations=[WorkspaceOperationSummary(**item) for item in records],
            )
        if isinstance(request, WorkspaceCommandLogsRequest):
            logs = await self.operations.logs(
                workspace_id,
                request.operation_id,
                stdout_offset=request.stdout_offset,
                stderr_offset=request.stderr_offset,
                max_bytes=request.max_bytes,
            )
            return WorkspaceCommandResponse(action="logs", **logs)
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Unknown workspaceCommand action.", status_code=422)

    async def read_files(self, owner: str, repo: str, workspace_id: str, request: WorkspaceReadFilesRequest) -> WorkspaceReadFilesResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        repo_dir = self.manager.repo_dir(workspace_id)
        max_file_bytes = self._bounded_output_bytes(request.max_bytes_per_file)
        max_response_bytes = self._bounded_output_bytes(request.max_bytes, minimum=_MIN_STRUCTURED_RESPONSE_BYTES)
        with self.manager.workspace_scope(workspace_id):
            files = [
                self._read_file_content(
                    repo_dir,
                    path,
                    start_line=request.start_line,
                    max_lines=request.max_lines,
                    max_bytes=max_file_bytes,
                )
                for path in request.paths
            ]
        self._audit(
            operation_id="workspaceReadFiles",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=meta.branch,
            head_sha_before=meta.head_sha,
            metadata={"paths": request.paths},
        )
        response = WorkspaceReadFilesResponse(
            workspace_id=workspace_id,
            files=files,
            truncated=any(item.truncated for item in files),
        )
        return self._fit_read_files_response(response, max_response_bytes)

    async def search(self, owner: str, repo: str, workspace_id: str, request: WorkspaceSearchRequest) -> WorkspaceSearchResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        repo_dir = self.manager.repo_dir(workspace_id)
        with self.manager.workspace_scope(workspace_id):
            response = await self._search_workspace(workspace_id, repo_dir, request)
        self._audit(
            operation_id="workspaceSearch",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=meta.branch,
            head_sha_before=meta.head_sha,
            command_hash=command_hash(request.query),
            metadata={"paths": request.paths, "match_count": response.match_count, "engine": response.engine},
        )
        return response

    async def inspect(self, owner: str, repo: str, workspace_id: str, request: WorkspaceInspectRequest) -> WorkspaceInspectResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        repo_dir = self.manager.repo_dir(workspace_id)
        max_file_bytes = self._bounded_output_bytes(request.max_bytes_per_file)
        max_response_bytes = self._bounded_output_bytes(request.max_bytes, minimum=_MIN_STRUCTURED_RESPONSE_BYTES)
        with self.manager.workspace_scope(workspace_id):
            tree, tree_truncated = self._tree_entries(
                repo_dir,
                request.paths,
                max_depth=request.max_depth,
                max_entries=request.max_tree_entries,
            )
            searches: list[WorkspaceInspectSearchResult] = []
            related: dict[str, int] = {}
            for query in request.queries:
                search_response = await self._search_workspace(
                    workspace_id,
                    repo_dir,
                    WorkspaceSearchRequest(
                        query=query,
                        paths=request.paths,
                        context_lines=request.context_lines,
                        max_matches=request.max_search_matches,
                        max_bytes=max_response_bytes,
                    ),
                )
                searches.append(
                    WorkspaceInspectSearchResult(
                        query=query,
                        engine=search_response.engine,
                        matches=search_response.matches,
                        match_count=search_response.match_count,
                        truncated=search_response.truncated,
                    )
                )
                if request.max_read_files > 0:
                    for match in search_response.matches:
                        related.setdefault(match.path, match.line_number)
                        if len(related) >= request.max_read_files:
                            break

            files = [
                self._read_file_content(
                    repo_dir,
                    path,
                    start_line=max(1, first_line - request.context_lines),
                    max_lines=request.max_file_lines,
                    max_bytes=max_file_bytes,
                )
                for path, first_line in list(related.items())[: request.max_read_files]
            ]
        self._audit(
            operation_id="workspaceInspect",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=meta.branch,
            head_sha_before=meta.head_sha,
            metadata={"paths": request.paths, "queries": request.queries},
        )
        response = WorkspaceInspectResponse(
            workspace_id=workspace_id,
            tree=tree,
            tree_truncated=tree_truncated,
            searches=searches,
            files=files,
            truncated=tree_truncated or any(item.truncated for item in files) or any(item.truncated for item in searches),
        )
        return self._fit_inspect_response(response, max_response_bytes)

    async def _search_workspace(self, workspace_id: str, repo_dir: Path, request: WorkspaceSearchRequest) -> WorkspaceSearchResponse:
        max_bytes = self._bounded_output_bytes(request.max_bytes, minimum=_MIN_STRUCTURED_RESPONSE_BYTES)
        rg = shutil.which("rg")
        if not rg:
            raise ApiError(
                ErrorCode.WORKSPACE_EXEC_FAILED,
                "ripgrep (rg) is required for workspaceSearch/workspaceInspect but was not found on PATH.",
                status_code=500,
                suggestion="Install ripgrep in the gateway runtime image so workspaceSearch fails loudly instead of silently falling back to a slow scanner.",
                details={"executable": "rg"},
            )
        return await self._search_with_ripgrep(workspace_id, repo_dir, request, rg, max_bytes=max_bytes)

    async def _search_with_ripgrep(
        self,
        workspace_id: str,
        repo_dir: Path,
        request: WorkspaceSearchRequest,
        rg: str,
        *,
        max_bytes: int,
    ) -> WorkspaceSearchResponse:
        paths = self._normalize_existing_tree_paths(repo_dir, request.paths)
        args = [
            rg,
            "--json",
            "--line-number",
            "--column",
            "--color",
            "never",
        ]
        if not request.regex:
            args.append("--fixed-strings")
        if not request.case_sensitive:
            args.append("--ignore-case")
        args.extend(["--", request.query, *paths])
        result = await self.manager.git.run(
            args,
            cwd=repo_dir,
            timeout=self.settings.workspace_default_timeout_seconds,
            check=False,
            allowed_exit_codes=(0, 1, 2),
            max_output_bytes=max_bytes,
        )
        if result.exit_code == 2:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "ripgrep rejected the search query.",
                status_code=422,
                details={"stderr": result.stderr},
            )
        matches: list[WorkspaceSearchMatch] = []
        truncated = result.truncated
        for raw_line in result.stdout.splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            raw_path = ((data.get("path") or {}).get("text") or "").replace("\\", "/")
            if _path_is_excluded_from_inspection(raw_path):
                continue
            line_number = int(data.get("line_number") or 0)
            line_text = str((data.get("lines") or {}).get("text") or "").rstrip("\r\n")
            submatches = data.get("submatches") or []
            column = None
            if submatches and isinstance(submatches[0], dict):
                column = int(submatches[0].get("start") or 0) + 1
            line_text, line_truncated = _clip_text_to_bytes(line_text, _SEARCH_LINE_MAX_BYTES)
            snippet_file = self._read_file_content(
                repo_dir,
                raw_path,
                start_line=max(1, line_number - request.context_lines),
                max_lines=(request.context_lines * 2) + 1,
                max_bytes=min(max_bytes, _SEARCH_SNIPPET_MAX_BYTES),
            )
            truncated = truncated or line_truncated or snippet_file.truncated
            matches.append(
                WorkspaceSearchMatch(
                    path=raw_path,
                    line_number=line_number,
                    column=column,
                    line=line_text,
                    snippet=snippet_file.content or None,
                )
            )
            if len(matches) >= request.max_matches:
                truncated = True
                break
        response = WorkspaceSearchResponse(
            workspace_id=workspace_id,
            query=request.query,
            engine="ripgrep",
            matches=matches,
            match_count=len(matches),
            truncated=truncated,
        )
        return self._fit_search_response(response, max_bytes)

    def _tree_entries(self, repo_dir: Path, paths: list[str], *, max_depth: int, max_entries: int) -> tuple[list[WorkspaceTreeEntry], bool]:
        entries: list[WorkspaceTreeEntry] = []
        truncated = False
        for base in self._normalize_existing_tree_paths(repo_dir, paths):
            base_path = repo_dir if base == "." else repo_dir / base
            if base_path.is_file():
                entries.append(WorkspaceTreeEntry(path=base, type="file", depth=0, bytes=base_path.stat().st_size))
                continue
            for current, dirs, files in os.walk(base_path):
                current_path = Path(current)
                rel_current = _relative_repo_path(repo_dir, current_path) if current_path != repo_dir else "."
                depth = 0 if rel_current == "." else len(PurePosixPath(rel_current).parts)
                if depth >= max_depth:
                    dirs[:] = []
                    continue
                dirs[:] = [item for item in sorted(dirs) if not _path_is_excluded_from_inspection(_join_repo_path(rel_current, item))]
                for dirname in dirs:
                    rel = _join_repo_path(rel_current, dirname)
                    entries.append(WorkspaceTreeEntry(path=rel, type="dir", depth=len(PurePosixPath(rel).parts)))
                    if len(entries) >= max_entries:
                        return entries, True
                for filename in sorted(files):
                    rel = _join_repo_path(rel_current, filename)
                    if _path_is_excluded_from_inspection(rel):
                        continue
                    file_path = current_path / filename
                    entries.append(WorkspaceTreeEntry(path=rel, type="file", depth=len(PurePosixPath(rel).parts), bytes=file_path.stat().st_size))
                    if len(entries) >= max_entries:
                        return entries, True
        return entries, truncated

    def _read_file_content(self, repo_dir: Path, path: str, *, start_line: int, max_lines: int, max_bytes: int) -> WorkspaceFileContent:
        try:
            normalized = self.policy.assert_tree_path_allowed(path)
            if normalized is None or normalized == ".":
                raise ApiError(ErrorCode.WORKSPACE_WRITE_INVALID_PATH, "A file path is required.", status_code=400, details={"path": path})
            if _path_is_excluded_from_inspection(normalized):
                raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Path is excluded from workspace inspection.", status_code=403, details={"path": normalized})
            repo_root = repo_dir.resolve()
            candidate = repo_dir / normalized
            _assert_no_symlink_components(repo_dir, normalized, message="Workspace read operations refuse symlinks.")
            resolved = candidate.resolve(strict=False)
            try:
                resolved.relative_to(repo_root)
            except ValueError as exc:
                raise ApiError(ErrorCode.PATH_NOT_ALLOWED, "Resolved path escapes the workspace repository.", status_code=403, details={"path": normalized}) from exc
            if not resolved.is_file():
                raise ApiError(ErrorCode.WORKSPACE_FILE_NOT_FOUND, "Workspace file was not found.", status_code=404, details={"path": normalized})
            if self.policy.has_binary_extension(normalized):
                raise ApiError(ErrorCode.WORKSPACE_BINARY_NOT_ALLOWED, "Binary-like files cannot be read by this gateway.", status_code=403, details={"path": normalized})
            data = resolved.read_bytes()
            assert_text_bytes(data, path=normalized)
            text = data.decode("utf-8")
            lines = text.splitlines()
            start_idx = start_line - 1
            selected = lines[start_idx : start_idx + max_lines]
            output_lines: list[str] = []
            output_bytes = 0
            truncated = start_idx + len(selected) < len(lines)
            next_start_line: int | None = start_line + len(selected) if truncated else None
            for offset, line in enumerate(selected, start=start_line):
                rendered = f"{offset}: {line}"
                rendered_bytes = len((rendered + "\n").encode("utf-8"))
                if rendered_bytes > max_bytes:
                    truncated = True
                    next_start_line = offset
                    break
                if output_bytes + rendered_bytes > max_bytes:
                    truncated = True
                    next_start_line = offset
                    break
                output_lines.append(rendered)
                output_bytes += rendered_bytes
            end_line = start_line + len(output_lines) - 1 if output_lines else None
            content = "\n".join(output_lines)
            content, content_truncated = _clip_text_to_bytes(content, max_bytes)
            was_truncated = truncated or content_truncated
            return WorkspaceFileContent(
                path=normalized,
                start_line=start_line,
                end_line=end_line,
                total_lines=len(lines),
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                content=content,
                truncated=was_truncated,
                next_start_line=next_start_line if was_truncated else None,
            )
        except Exception as exc:
            if isinstance(exc, ApiError):
                message = exc.message
            else:
                message = str(exc)
            return WorkspaceFileContent(path=path, start_line=start_line, error=message, truncated=False)

    def _normalize_existing_tree_paths(self, repo_dir: Path, paths: list[str]) -> list[str]:
        normalized_paths: list[str] = []
        repo_root = repo_dir.resolve()
        for raw_path in paths:
            normalized = self.policy.assert_tree_path_allowed(raw_path) or "."
            if _path_is_excluded_from_inspection(normalized):
                continue
            if normalized != ".":
                _assert_no_symlink_components(repo_dir, normalized, message="Workspace inspection operations refuse symlinks.")
            resolved = repo_root if normalized == "." else (repo_dir / normalized).resolve(strict=False)
            try:
                resolved.relative_to(repo_root)
            except ValueError as exc:
                raise ApiError(ErrorCode.PATH_NOT_ALLOWED, "Resolved path escapes the workspace repository.", status_code=403, details={"path": normalized}) from exc
            if not resolved.exists():
                raise ApiError(ErrorCode.WORKSPACE_FILE_NOT_FOUND, "Workspace path was not found.", status_code=404, details={"path": normalized})
            normalized_paths.append(normalized)
        if not normalized_paths:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "All requested paths are excluded from workspace inspection.",
                status_code=422,
                details={"paths": paths},
            )
        return normalized_paths

    def _bounded_output_bytes(self, requested: int | None, *, minimum: int = 1) -> int:
        max_bytes = min(requested or self.settings.workspace_max_output_bytes, self.settings.workspace_max_output_bytes)
        if max_bytes < minimum:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Requested output byte budget is too small for this workspace response.",
                status_code=422,
                details={"requested_max_bytes": max_bytes, "minimum_max_bytes": minimum},
            )
        return max_bytes

    def _validate_prepared_changes(self, changes: list[PreparedFileChange], changed_files: list[ChangedFile]) -> None:
        changed_by_path = {item.path: item for item in changed_files}
        for change in changes:
            changed = changed_by_path.get(change.path)
            operation = changed.operation if changed is not None else ("deleted" if change.after is None else "modified")
            self.policy.assert_write_path_allowed(change.path, operation=operation)
            if change.after is None:
                continue
            self.policy.assert_file_size(len(change.after), max_size=self.settings.max_blob_read_bytes)
            if self.policy.looks_binary(change.after):
                raise ApiError(
                    ErrorCode.BINARY_FILE_NOT_ALLOWED,
                    "Non-text changes are not allowed in workspace text operations.",
                    status_code=403,
                    details={"path": change.path},
                )

    async def _describe_prepared_git_changes(
        self,
        repo_dir: Path,
        changes: list[PreparedFileChange],
    ) -> tuple[list[ChangedFile], str]:
        final_changes: list[PreparedFileChange] = []
        for change in changes:
            head_bytes = await self._read_head_blob(repo_dir, change.path)
            if head_bytes == change.after:
                continue
            final_changes.append(
                PreparedFileChange(
                    path=change.path,
                    resolved_path=change.resolved_path,
                    before=head_bytes,
                    after=change.after,
                )
            )
        return describe_prepared_changes(final_changes)

    async def _read_head_blob(self, repo_dir: Path, path: str) -> bytes | None:
        max_bytes = self.settings.max_blob_read_bytes

        def read_blob() -> bytes | None:
            with tempfile.SpooledTemporaryFile(max_size=max_bytes + 1) as output:
                result = subprocess.run(
                    [
                        "git",
                        "cat-file",
                        "--filters",
                        f"--path={path}",
                        f"HEAD:{path}",
                    ],
                    cwd=repo_dir,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                if result.returncode == 128:
                    return None
                if result.returncode != 0:
                    raise ApiError(
                        ErrorCode.WORKSPACE_EXEC_FAILED,
                        "Unable to read the HEAD version of a workspace file.",
                        status_code=500,
                        details={
                            "path": path,
                            "exit_code": result.returncode,
                            "stderr": result.stderr.decode("utf-8", errors="replace"),
                        },
                    )
                output.seek(0)
                data = output.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ApiError(
                        ErrorCode.WORKSPACE_POLICY_VIOLATION,
                        "The HEAD version of a workspace file exceeds the configured size limit.",
                        status_code=413,
                        details={"path": path, "max_bytes": max_bytes},
                    )
                return data

        return await asyncio.to_thread(read_blob)

    def _fit_read_files_response(self, response: WorkspaceReadFilesResponse, max_bytes: int) -> WorkspaceReadFilesResponse:
        while _model_json_bytes(response) > max_bytes and response.files:
            files = list(response.files)
            last_file = files[-1]
            if last_file.content:
                files[-1] = last_file.model_copy(
                    update={
                        "content": "",
                        "truncated": True,
                        "next_start_line": last_file.start_line,
                    }
                )
            else:
                files.pop()
            response = response.model_copy(update={"files": files, "truncated": True})
        if _model_json_bytes(response) > max_bytes:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "max_bytes is too small for the workspaceReadFiles response envelope.",
                status_code=422,
                details={"max_bytes": max_bytes, "minimum_max_bytes": _MIN_STRUCTURED_RESPONSE_BYTES},
            )
        return response

    def _fit_search_response(self, response: WorkspaceSearchResponse, max_bytes: int) -> WorkspaceSearchResponse:
        while _model_json_bytes(response) > max_bytes and response.matches:
            matches = list(response.matches)
            last = matches[-1]
            if last.snippet:
                matches[-1] = last.model_copy(update={"snippet": None})
            elif last.line:
                matches[-1] = last.model_copy(update={"line": ""})
            else:
                matches.pop()
            response = response.model_copy(update={"matches": matches, "match_count": len(matches), "truncated": True})
        if _model_json_bytes(response) > max_bytes:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "max_bytes is too small for the workspaceSearch response envelope.",
                status_code=422,
                details={"max_bytes": max_bytes, "minimum_max_bytes": _MIN_STRUCTURED_RESPONSE_BYTES},
            )
        return response

    def _fit_inspect_response(self, response: WorkspaceInspectResponse, max_bytes: int) -> WorkspaceInspectResponse:
        while _model_json_bytes(response) > max_bytes:
            if response.files:
                files = list(response.files)
                last_file = files[-1]
                if last_file.content:
                    files[-1] = last_file.model_copy(
                        update={
                            "content": "",
                            "truncated": True,
                            "next_start_line": last_file.start_line,
                        }
                    )
                else:
                    files.pop()
                response = response.model_copy(update={"files": files, "truncated": True})
                continue
            if any(search.matches for search in response.searches):
                searches = list(response.searches)
                for index in range(len(searches) - 1, -1, -1):
                    search = searches[index]
                    if not search.matches:
                        continue
                    matches = list(search.matches)
                    last_match = matches[-1]
                    if last_match.snippet:
                        matches[-1] = last_match.model_copy(update={"snippet": None})
                    elif last_match.line:
                        matches[-1] = last_match.model_copy(update={"line": ""})
                    else:
                        matches.pop()
                    searches[index] = search.model_copy(update={"matches": matches, "match_count": len(matches), "truncated": True})
                    break
                response = response.model_copy(update={"searches": searches, "truncated": True})
                continue
            if response.tree:
                response = response.model_copy(update={"tree": response.tree[:-1], "tree_truncated": True, "truncated": True})
                continue
            if response.searches:
                response = response.model_copy(update={"searches": response.searches[:-1], "truncated": True})
                continue
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "max_bytes is too small for the workspaceInspect response envelope.",
                status_code=422,
                details={"max_bytes": max_bytes, "minimum_max_bytes": _MIN_STRUCTURED_RESPONSE_BYTES},
            )
        return response

    async def status(self, owner: str, repo: str, workspace_id: str, request: WorkspaceStatusRequest) -> WorkspaceStatusResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        repo_dir = self.manager.repo_dir(workspace_id)
        with self.manager.workspace_scope(workspace_id):
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
            active_operations=(
                [
                    WorkspaceOperationSummary(**item)
                    for item in self.operations.active_for_workspace(workspace_id)
                ]
                if self.operations is not None
                else []
            ),
        )

    async def diff(self, owner: str, repo: str, workspace_id: str, request: WorkspaceDiffRequest) -> WorkspaceDiffResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        repo_dir = self.manager.repo_dir(workspace_id)
        max_bytes = min(request.max_bytes or self.settings.workspace_max_diff_bytes, self.settings.workspace_max_diff_bytes)
        with self.manager.workspace_scope(workspace_id):
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
        with self.manager.workspace_scope(workspace_id):
            operations = parse_codex_patch(request.patch, self.policy, repo_dir, allow_delete=request.allow_delete, max_changed_files=max_changed_files)
            target_paths = list(dict.fromkeys(item.path for item in operations))
            snapshots = snapshot_files(repo_dir, target_paths)
            prepared = prepare_text_patch(repo_dir, operations, snapshots)
            changed, diff_stat = await self._describe_prepared_git_changes(repo_dir, prepared)
            if len(changed) > max_changed_files:
                raise ApiError(
                    ErrorCode.WORKSPACE_TOO_MANY_CHANGED_FILES,
                    "Patch changes too many files.",
                    status_code=413,
                    details={"count": len(changed), "max": max_changed_files},
                )
            self._validate_prepared_changes(prepared, changed)
            if not request.dry_run:
                try:
                    commit_prepared_changes(repo_dir, prepared)
                except Exception as exc:
                    if isinstance(exc, ApiError):
                        raise
                    raise ApiError(
                        ErrorCode.WORKSPACE_WRITE_FAILED,
                        "Failed to commit the prepared workspace patch.",
                        status_code=500,
                        details={"paths": target_paths, "error": str(exc)},
                    ) from exc
            response = WorkspaceApplyPatchResponse(
                applied=not request.dry_run,
                dry_run=request.dry_run,
                changed_files=changed,
                diff_stat=diff_stat,
            )
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
        with self.manager.workspace_scope(workspace_id):
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

            changed: list[ChangedFile] = []
            diff_stat = ""
            if operation != "unchanged":
                prepared = prepare_write_change(path=path, resolved_path=resolved, before=previous_bytes, after=data)
                changed, diff_stat = await self._describe_prepared_git_changes(repo_dir, prepared)
                self._validate_prepared_changes(prepared, changed)
                if not request.dry_run:
                    try:
                        commit_prepared_changes(repo_dir, prepared)
                    except Exception as exc:
                        if isinstance(exc, ApiError):
                            raise
                        raise ApiError(
                            ErrorCode.WORKSPACE_WRITE_FAILED,
                            "Failed to write workspace file.",
                            status_code=500,
                            details={"path": path, "error": str(exc)},
                        ) from exc
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
        with self.manager.workspace_scope(workspace_id):
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

        with self.manager.workspace_scope(workspace_id):
            gitignore_updated = _ensure_gpt_artifacts_local_exclude(repo_dir)
            existing_manifest = _read_artifact_manifest(manifest_path)
            skipped = _manifest_is_current(repo_dir, existing_manifest, remote_fingerprint)
            if skipped:
                assert existing_manifest is not None
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
            diagnostics=self._diagnostics_model(result),
        )

    @staticmethod
    def _prepare_metadata(result) -> dict:
        return {
            "created": result.created,
            "workspace_stage": result.workspace_stage,
            "workspace_duration_ms": result.workspace_duration_ms,
            "total_duration_ms": result.total_duration_ms,
        }


def _path_is_excluded_from_inspection(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    if not normalized or normalized == ".":
        return False
    parts = PurePosixPath(normalized).parts
    if any(part in _EXCLUDED_INSPECT_DIRS for part in parts):
        return True
    filename = parts[-1]
    if (filename == ".env" or filename.startswith(".env.")) and filename not in _ALLOWED_ENV_READ_FILES:
        return True
    return False


def _assert_no_symlink_components(repo_dir: Path, normalized_path: str, *, message: str) -> None:
    current = repo_dir
    for part in PurePosixPath(normalized_path).parts:
        current = current / part
        if current.is_symlink():
            raise ApiError(
                ErrorCode.WORKSPACE_POLICY_VIOLATION,
                message,
                status_code=403,
                details={"path": normalized_path},
            )


def _clip_text_to_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= max_bytes:
        return text, False
    marker = _TRUNCATION_MARKER.encode("utf-8")
    if max_bytes <= len(marker):
        return data[:max_bytes].decode("utf-8", errors="ignore"), True
    clipped = data[: max_bytes - len(marker)].decode("utf-8", errors="ignore")
    return clipped + _TRUNCATION_MARKER, True


def _model_json_bytes(model: Any) -> int:
    return len(model.model_dump_json().encode("utf-8"))


def _join_repo_path(parent: str, child: str) -> str:
    if parent in {"", "."}:
        return child.replace("\\", "/")
    return f"{parent.rstrip('/')}/{child}".replace("\\", "/")


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
