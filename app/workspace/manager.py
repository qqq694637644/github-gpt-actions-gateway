from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import shlex
import shutil
import stat
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
from app.models.common import ChangedFile
from app.policy.rules import Policy, is_sha
from app.workspace.git import GitRunner, attach_numstat, normalize_git_paths, parse_numstat, parse_porcelain_z
from app.workspace.ids import WORKSPACE_ID_RE
from app.workspace.models import WorkspaceMeta, WorkspacePrepareStats, load_meta, save_meta

logger = logging.getLogger(__name__)


class WorkspaceManager:
    def __init__(self, settings: Settings, github: GitHubClient, policy: Policy) -> None:
        self.settings = settings
        self.github = github
        self.policy = policy
        self.root = Path(settings.workspace_root).resolve()
        self.git = GitRunner(default_timeout=max(settings.workspace_default_timeout_seconds, 1))
        self._prepare_locks: dict[str, asyncio.Lock] = {}
        self._active_workspace_provider: Callable[[], set[str]] = set
        self.root.mkdir(parents=True, exist_ok=True)

    def set_active_workspace_provider(self, provider: Callable[[], set[str]]) -> None:
        self._active_workspace_provider = provider

    @asynccontextmanager
    async def prepare_idempotency_lock(self, scope: str, key: str) -> AsyncIterator[None]:
        lock_key = f"{scope}:{key}"
        lock = self._prepare_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            yield

    def workspace_dir(self, workspace_id: str) -> Path:
        if not WORKSPACE_ID_RE.fullmatch(workspace_id):
            raise ApiError(ErrorCode.WORKSPACE_NOT_FOUND, "Workspace id is invalid.", status_code=404)
        return self.root / workspace_id

    def repo_dir(self, workspace_id: str) -> Path:
        return self.workspace_dir(workspace_id) / "repo"

    def get_meta(self, workspace_id: str) -> WorkspaceMeta:
        workspace_dir = self.workspace_dir(workspace_id)
        if not (workspace_dir / "meta.json").exists():
            raise ApiError(ErrorCode.WORKSPACE_NOT_FOUND, "Workspace was not found.", status_code=404, details={"workspace_id": workspace_id})
        meta = load_meta(workspace_dir)
        try:
            os.utime(workspace_dir, None)
        except OSError:
            logger.exception("workspace_touch.error workspace_id=%s path=%s", workspace_id, workspace_dir)
        return meta

    @contextmanager
    def workspace_scope(self, workspace_id: str) -> Iterator[None]:
        """Validate the workspace path without applying workspace-wide locking."""
        self.workspace_dir(workspace_id)
        yield

    async def prepare(
        self,
        *,
        owner: str,
        repo: str,
        branch: str | None,
        source_pr_number: int | None,
        base_ref: str | None,
        purpose_slug: str,
    ) -> WorkspacePrepareStats:
        return await self._prepare_workspace(
            owner=owner,
            repo=repo,
            branch=branch,
            source_pr_number=source_pr_number,
            base_ref=base_ref,
            purpose_slug=purpose_slug,
        )


    async def _prepare_workspace(
        self,
        *,
        owner: str,
        repo: str,
        branch: str | None,
        source_pr_number: int | None,
        base_ref: str | None,
        purpose_slug: str,
    ) -> WorkspacePrepareStats:
        self.policy.assert_repo_allowed(owner, repo)
        selected = [branch is not None, source_pr_number is not None, base_ref is not None]
        if sum(selected) != 1:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Provide exactly one of branch, source_pr_number, or base_ref.", status_code=422)

        repository = await self.github.get_repository(owner, repo)
        default_branch = repository.get("default_branch") or self.settings.default_base_branch
        source_pr = None
        if source_pr_number is not None:
            source_pr = await self.github.get_pull_request(owner, repo, source_pr_number)
            head_repo = ((source_pr.get("head") or {}).get("repo") or {}).get("full_name", "").lower()
            if head_repo and head_repo != f"{owner}/{repo}".lower():
                raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Workspaces can only be prepared from same-repository PR heads.", status_code=403)
            branch = (source_pr.get("head") or {}).get("ref")
            if not branch:
                raise ApiError(ErrorCode.GITHUB_ERROR, "PR head branch was missing from GitHub response.", status_code=502)
        writable = branch is not None
        if writable:
            assert branch is not None
            self.policy.assert_write_branch_allowed(branch)
            target_ref: str = branch
        else:
            target_ref = base_ref or default_branch
            self.policy.assert_read_ref_allowed(target_ref)

        pruned_count = self.prune_expired_workspace_dirs()
        logger.warning(
            "workspace_prune.prepare_triggered ttl_hours=%s pruned_count=%s target_ref=%s root=%s",
            self.settings.workspace_ttl_hours,
            pruned_count,
            target_ref,
            self.root,
        )
        workspace_id = self._new_workspace_id(purpose_slug)
        workspace_dir = self.workspace_dir(workspace_id)
        repo_dir = workspace_dir / "repo"
        created = True
        start_total = time.perf_counter()
        with self.workspace_scope(workspace_id):
            workspace_dir.mkdir(parents=True, exist_ok=False)
            meta = WorkspaceMeta(
                workspace_id=workspace_id,
                owner=owner,
                repo=repo,
                branch=target_ref,
                default_branch=default_branch,
                head_sha="",
                source_pr_number=source_pr_number,
                writable=writable,
            )
            try:
                workspace_start = time.perf_counter()
                workspace_stage = "clone"
                await self._clone_workspace(owner, repo, repo_dir)
                await self.fetch_branch(repo_dir, target_ref)
                await self.checkout_ref(repo_dir, target_ref)
                if self.should_use_python_venv(writable=writable):
                    await self.ensure_python_venv(repo_dir)
                workspace_duration_ms = round((time.perf_counter() - workspace_start) * 1000)
                head_sha = await self.head_sha(repo_dir)
                meta.head_sha = head_sha
                meta.default_branch = default_branch
                meta.source_pr_number = source_pr_number
                meta.writable = writable
                save_meta(workspace_dir, meta)
                diagnostics = WorkspacePrepareStats(
                    meta=meta,
                    created=created,
                    workspace_stage=workspace_stage,
                    workspace_duration_ms=workspace_duration_ms,
                    total_duration_ms=round((time.perf_counter() - start_total) * 1000),
                )
                return diagnostics
            except BaseException:
                shutil.rmtree(workspace_dir, ignore_errors=True)
                raise

    async def _clone_workspace(self, owner: str, repo: str, repo_dir: Path) -> None:
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        auth_config = await self.github.git_auth_config()
        await self.git.run(
            ["git", *auth_config, "clone", self.github.git_remote_url(owner, repo), str(repo_dir)],
            timeout=self.settings.workspace_max_timeout_seconds,
        )
        await self.git.run(["git", "config", "user.name", self.settings.workspace_git_user_name], cwd=repo_dir)
        await self.git.run(["git", "config", "user.email", self.settings.workspace_git_user_email], cwd=repo_dir)

    def should_use_python_venv(self, *, writable: bool) -> bool:
        return self.settings.workspace_python_venv_enabled and writable

    async def ensure_python_venv(self, repo_dir: Path) -> None:
        venv_dir = self.settings.workspace_python_venv_dir
        venv_path = repo_dir / venv_dir
        if self.settings.workspace_python_auto_gitignore:
            self.ensure_python_venv_local_exclude(repo_dir)
        if venv_path.exists():
            if not venv_path.is_dir():
                raise ApiError(
                    ErrorCode.WORKSPACE_POLICY_VIOLATION,
                    "Configured Python virtual environment path exists but is not a directory.",
                    status_code=409,
                    details={"path": venv_dir},
                )
            if not (venv_path / "pyvenv.cfg").is_file():
                raise ApiError(
                    ErrorCode.WORKSPACE_POLICY_VIOLATION,
                    "Configured Python virtual environment is incomplete: pyvenv.cfg is missing.",
                    status_code=409,
                    details={"path": venv_dir},
                )
        else:
            python_cmd = split_command(self.settings.workspace_python_venv_python)
            await self.git.run([*python_cmd, "-m", "venv", venv_dir], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds)
        await self.validate_python_venv(repo_dir)

    async def validate_python_venv(self, repo_dir: Path) -> None:
        python_path = self.python_venv_executable(repo_dir)
        try:
            await self.git.run([str(python_path), "--version"], cwd=repo_dir, timeout=self.settings.workspace_default_timeout_seconds, max_output_bytes=8_000)
        except ApiError as exc:
            raise ApiError(
                ErrorCode.WORKSPACE_EXEC_FAILED,
                "Workspace Python virtual environment executable failed validation.",
                status_code=500,
                details={"path": str(python_path)},
            ) from exc

    def python_venv_executable(self, repo_dir: Path) -> Path:
        venv_dir = self.settings.workspace_python_venv_dir
        venv_path = repo_dir / venv_dir
        if not venv_path.is_dir():
            raise ApiError(
                ErrorCode.WORKSPACE_POLICY_VIOLATION,
                "Configured Python virtual environment directory is missing.",
                status_code=409,
                details={"path": venv_dir},
            )
        if not (venv_path / "pyvenv.cfg").is_file():
            raise ApiError(
                ErrorCode.WORKSPACE_POLICY_VIOLATION,
                "Configured Python virtual environment is incomplete: pyvenv.cfg is missing.",
                status_code=409,
                details={"path": venv_dir},
            )
        bin_dir = venv_path / ("Scripts" if os.name == "nt" else "bin")
        python_path = bin_dir / ("python.exe" if os.name == "nt" else "python")
        if not bin_dir.is_dir():
            raise ApiError(
                ErrorCode.WORKSPACE_POLICY_VIOLATION,
                "Configured Python virtual environment is incomplete: interpreter directory is missing.",
                status_code=409,
                details={"path": str(bin_dir.relative_to(repo_dir))},
            )
        if not python_path.is_file():
            raise ApiError(
                ErrorCode.WORKSPACE_POLICY_VIOLATION,
                "Configured Python virtual environment is incomplete: Python executable is missing.",
                status_code=409,
                details={"path": str(python_path.relative_to(repo_dir))},
            )
        return python_path

    def ensure_python_venv_local_exclude(self, repo_dir: Path) -> None:
        venv_entry = self.settings.workspace_python_venv_dir.rstrip("/") + "/"
        exclude = repo_dir / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        text = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
        if gitignore_contains_entry(text, venv_entry):
            return
        if text and not text.endswith("\n"):
            text += "\n"
        if text.strip():
            text += "\n"
        text += "# Local Python virtual environment\n" + venv_entry + "\n"
        exclude.write_text(text, encoding="utf-8")

    async def _ensure_origin(self, owner: str, repo: str, repo_dir: Path) -> None:
        await self.git.run(["git", "remote", "set-url", "origin", self.github.git_remote_url(owner, repo)], cwd=repo_dir)

    async def fetch_branch(self, repo_dir: Path, branch: str) -> None:
        auth_config = await self.github.git_auth_config()
        if is_sha(branch):
            await self.git.run(["git", *auth_config, "fetch", "origin", branch], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds)
        else:
            await self.git.run(["git", *auth_config, "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds)

    async def checkout_ref(self, repo_dir: Path, ref: str) -> None:
        if is_sha(ref):
            await self.git.run(["git", "checkout", "--detach", ref], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds)
        else:
            await self.git.run(["git", "checkout", "-B", ref, f"origin/{ref}"], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds)

    async def reset_to_remote(self, repo_dir: Path, branch: str, *, clean_untracked: bool) -> list[str]:
        if is_sha(branch):
            await self.git.run(["git", "reset", "--hard", branch], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds)
        else:
            await self.fetch_branch(repo_dir, branch)
            await self.git.run(["git", "reset", "--hard", f"origin/{branch}"], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds)
        removed: list[str] = []
        if clean_untracked:
            before = await self.untracked_files(repo_dir)
            await self.git.run(["git", "clean", "-fd"], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds)
            removed = before
        return removed

    async def head_sha(self, repo_dir: Path) -> str:
        result = await self.git.run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
        return result.stdout.strip()

    async def remote_head_sha(self, repo_dir: Path, branch: str) -> str | None:
        if is_sha(branch):
            return branch
        result = await self.git.run(["git", "rev-parse", f"origin/{branch}"], cwd=repo_dir, check=False, allowed_exit_codes=(0, 128))
        return result.stdout.strip() if result.exit_code == 0 else None

    async def current_branch(self, repo_dir: Path) -> str:
        result = await self.git.run(["git", "branch", "--show-current"], cwd=repo_dir)
        return result.stdout.strip()

    async def changed_files(self, repo_dir: Path) -> tuple[list[ChangedFile], list[str], list[str]]:
        status = await self.git.run(["git", "status", "--porcelain=v1", "-z"], cwd=repo_dir, max_output_bytes=self.settings.workspace_max_output_bytes)
        changed, untracked, conflicts = parse_porcelain_z(status.stdout)
        stats = await self.diff_numstat(repo_dir, staged=False)
        changed = attach_numstat(changed, stats)
        return changed, untracked, conflicts

    async def untracked_files(self, repo_dir: Path) -> list[str]:
        result = await self.git.run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=repo_dir)
        return [item for item in result.stdout.split("\0") if item]

    async def diff_numstat(self, repo_dir: Path, *, staged: bool) -> dict[str, tuple[int, int]]:
        args = ["git", "diff", "--numstat"]
        if staged:
            args.insert(2, "--cached")
        result = await self.git.run(args, cwd=repo_dir, check=False, allowed_exit_codes=(0, 1))
        stats = parse_numstat(result.stdout)
        if not staged:
            for path in await self.untracked_files(repo_dir):
                file_path = repo_dir / path
                if file_path.is_file():
                    try:
                        text = file_path.read_text(encoding="utf-8", errors="replace")
                        stats.setdefault(path, (len(text.splitlines()), 0))
                    except OSError:
                        pass
        return stats

    async def diff_text(self, repo_dir: Path, *, paths: list[str], stat_only: bool, max_bytes: int) -> tuple[str, bool]:
        git_paths = normalize_git_paths(paths)
        args = ["git", "diff", "--stat" if stat_only else "--"]
        if stat_only:
            args.extend(["--", *git_paths])
        else:
            args.extend(git_paths)
        result = await self.git.run(args, cwd=repo_dir, check=False, allowed_exit_codes=(0, 1), max_output_bytes=max_bytes)
        text = result.stdout
        if not stat_only:
            untracked = await self.untracked_files(repo_dir)
            for path in untracked[: self.settings.workspace_max_changed_files]:
                if not _path_is_selected(path, git_paths):
                    continue
                file_path = repo_dir / path
                if file_path.is_file():
                    no_index = await self.git.run(["git", "diff", "--no-index", "--", os.devnull, path], cwd=repo_dir, check=False, allowed_exit_codes=(0, 1), max_output_bytes=max_bytes)
                    text += ("\n" if text else "") + no_index.stdout
                    if len(text.encode("utf-8", errors="replace")) > max_bytes:
                        encoded = text.encode("utf-8", errors="replace")[:max_bytes]
                        return encoded.decode("utf-8", errors="replace") + "\n...[truncated]", True
        return text, result.truncated

    async def diff_stat(self, repo_dir: Path) -> str:
        result = await self.git.run(["git", "diff", "--stat"], cwd=repo_dir, check=False, allowed_exit_codes=(0, 1), max_output_bytes=20_000)
        text = result.stdout.strip()
        untracked = await self.untracked_files(repo_dir)
        if untracked:
            text = (text + "\n" if text else "") + "Untracked files:\n" + "\n".join(f"  {p}" for p in untracked[: self.settings.workspace_max_changed_files])
        return text

    async def staged_changed_files(self, repo_dir: Path) -> list[ChangedFile]:
        result = await self.git.run(["git", "diff", "--cached", "--name-status", "-z"], cwd=repo_dir)
        changed = _changed_files_from_name_status_z(result.stdout)
        stats = await self.diff_numstat(repo_dir, staged=True)
        return attach_numstat(changed, stats)

    async def committed_changed_files_between(self, repo_dir: Path, base_ref: str, head_ref: str) -> list[ChangedFile]:
        result = await self.git.run(["git", "diff", "--name-status", "-z", f"{base_ref}..{head_ref}"], cwd=repo_dir)
        changed = _changed_files_from_name_status_z(result.stdout)
        stats_result = await self.git.run(["git", "diff", "--numstat", f"{base_ref}..{head_ref}"], cwd=repo_dir, check=False, allowed_exit_codes=(0, 1))
        return attach_numstat(changed, parse_numstat(stats_result.stdout))

    async def diff_stat_between(self, repo_dir: Path, base_ref: str, head_ref: str) -> str:
        result = await self.git.run(["git", "diff", "--stat", f"{base_ref}..{head_ref}"], cwd=repo_dir, check=False, allowed_exit_codes=(0, 1), max_output_bytes=20_000)
        return result.stdout.strip()

    async def is_ancestor(self, repo_dir: Path, ancestor_ref: str, descendant_ref: str) -> bool:
        result = await self.git.run(["git", "merge-base", "--is-ancestor", ancestor_ref, descendant_ref], cwd=repo_dir, check=False, allowed_exit_codes=(0, 1))
        return result.exit_code == 0

    async def changed_files_for_paths(self, repo_dir: Path, paths: list[str]) -> list[ChangedFile]:
        selectors = normalize_git_paths(paths)
        changed, _, _ = await self.changed_files(repo_dir)
        selected: list[ChangedFile] = []
        for item in changed:
            if _path_is_selected(item.path, selectors):
                operation = "added" if item.operation == "untracked" else item.operation
                selected.append(item.model_copy(update={"operation": operation}))
        return selected

    async def diff_stat_for_paths(self, repo_dir: Path, paths: list[str]) -> str:
        selectors = normalize_git_paths(paths)
        result = await self.git.run(["git", "diff", "--stat", "--", *selectors], cwd=repo_dir, check=False, allowed_exit_codes=(0, 1), max_output_bytes=20_000)
        chunks: list[str] = [result.stdout.strip()] if result.stdout.strip() else []
        for path in (await self.untracked_files(repo_dir))[: self.settings.workspace_max_changed_files]:
            if not _path_is_selected(path, selectors):
                continue
            file_path = repo_dir / path
            if file_path.is_file():
                no_index = await self.git.run(["git", "diff", "--no-index", "--stat", "--", os.devnull, path], cwd=repo_dir, check=False, allowed_exit_codes=(0, 1), max_output_bytes=20_000)
                if no_index.stdout.strip():
                    chunks.append(no_index.stdout.strip())
        return "\n".join(chunks)

    async def ahead_behind(self, repo_dir: Path, branch: str) -> tuple[int, int]:
        if is_sha(branch):
            return 0, 0
        result = await self.git.run(["git", "rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"], cwd=repo_dir, check=False, allowed_exit_codes=(0, 128))
        if result.exit_code != 0:
            return 0, 0
        parts = result.stdout.strip().split()
        if len(parts) != 2:
            return 0, 0
        return int(parts[0]), int(parts[1])

    async def validate_changed_paths(self, repo_dir: Path, changed_files: list[ChangedFile]) -> None:
        if len(changed_files) > self.settings.workspace_max_changed_files:
            raise ApiError(ErrorCode.TOO_MANY_FILES, "Too many changed files in workspace.", status_code=413, details={"count": len(changed_files), "max": self.settings.workspace_max_changed_files})
        for item in changed_files:
            self.policy.assert_write_path_allowed(item.path, operation=item.operation)
            if item.previous_path:
                self.policy.assert_write_path_allowed(item.previous_path, operation="deleted")
            file_path = (repo_dir / item.path).resolve()
            if item.operation != "deleted" and file_path.exists() and file_path.is_file():
                try:
                    data = file_path.read_bytes()
                except OSError as exc:
                    raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Unable to inspect changed file.", status_code=400, details={"path": item.path, "error": str(exc)}) from exc
                self.policy.assert_file_size(len(data), max_size=self.settings.max_blob_read_bytes)
                if self.policy.looks_binary(data):
                    raise ApiError(ErrorCode.BINARY_FILE_NOT_ALLOWED, "Binary file changes are not allowed in workspace commits.", status_code=403, details={"path": item.path})

    def _new_workspace_id(self, purpose_slug: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", purpose_slug).strip("_-").lower()
        slug = slug[:40] or "task"
        for _ in range(10):
            candidate = f"ws_{slug}_{secrets.token_hex(4)}"
            if not (self.root / candidate).exists():
                return candidate
        raise ApiError(
            ErrorCode.WORKSPACE_STORAGE_LIMIT,
            "Unable to allocate a unique workspace id.",
            status_code=507,
        )

    def prune_expired_workspace_dirs(self) -> int:
        ttl_hours = self.settings.workspace_ttl_hours
        if ttl_hours <= 0:
            logger.warning("workspace_prune.disabled ttl_hours=%s root=%s", ttl_hours, self.root)
            return 0
        now = time.time()
        cutoff = now - ttl_hours * 60 * 60
        deleted = 0
        scanned = 0
        logger.warning("workspace_prune.start ttl_hours=%s cutoff_epoch=%s root=%s", ttl_hours, round(cutoff, 3), self.root)
        for item in self.root.glob("ws_*"):
            scanned += 1
            if not item.is_dir() or not WORKSPACE_ID_RE.fullmatch(item.name):
                logger.warning("workspace_prune.skip_invalid name=%s path=%s is_dir=%s", item.name, item, item.is_dir())
                continue
            if item.name in self._active_workspace_provider():
                logger.warning("workspace_prune.skip_active workspace_id=%s path=%s", item.name, item)
                continue
            try:
                mtime = item.stat().st_mtime
                age_seconds = max(0.0, now - mtime)
                if mtime >= cutoff:
                    logger.warning(
                        "workspace_prune.skip_fresh workspace_id=%s path=%s age_hours=%.2f ttl_hours=%s mtime_epoch=%s",
                        item.name,
                        item,
                        age_seconds / 3600,
                        ttl_hours,
                        round(mtime, 3),
                    )
                    continue
                _remove_tree(item)
                deleted += 1
                logger.warning(
                    "workspace_prune.deleted workspace_id=%s path=%s age_hours=%.2f ttl_hours=%s mtime_epoch=%s",
                    item.name,
                    item,
                    age_seconds / 3600,
                    ttl_hours,
                    round(mtime, 3),
                )
            except OSError:
                logger.exception("workspace_prune.error workspace_id=%s path=%s", item.name, item)
                continue
        logger.warning("workspace_prune.done ttl_hours=%s scanned_count=%s deleted_count=%s root=%s", ttl_hours, scanned, deleted, self.root)
        return deleted

    def remove_legacy_lock_files(self) -> int:
        removed = 0
        for item in self.root.glob("ws_*/lock"):
            try:
                item.unlink()
                removed += 1
            except OSError:
                logger.exception("workspace_lock_cleanup.error path=%s", item)
        return removed


def _path_is_selected(path: str, selectors: list[str]) -> bool:
    if "." in selectors:
        return True
    return any(path == selector or path.startswith(selector.rstrip("/") + "/") for selector in selectors)


def _changed_files_from_name_status_z(raw: str) -> list[ChangedFile]:
    entries = [part for part in raw.split("\0") if part]
    changed: list[ChangedFile] = []
    idx = 0
    while idx < len(entries):
        code = entries[idx]
        idx += 1
        if idx >= len(entries):
            break
        path = entries[idx]
        previous_path = None
        idx += 1
        if code.startswith("R"):
            previous_path = path
            if idx >= len(entries):
                break
            path = entries[idx]
            idx += 1
        operation = {"A": "added", "M": "modified", "D": "deleted"}.get(code[:1], "renamed" if code.startswith("R") else "modified")
        changed.append(ChangedFile(path=path, operation=operation, status=code, previous_path=previous_path))
    return changed


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, onerror=_make_tree_entry_writable_and_retry)


def _make_tree_entry_writable_and_retry(func: Callable[[str], object], path: str, exc_info: object) -> None:
    del exc_info
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        logger.warning("workspace_prune.retry_writable path=%s", path)
        func(path)
    except OSError:
        raise


def split_command(command: str) -> list[str]:
    parts = shlex.split(command, posix=os.name != "nt")
    if not parts:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Python venv command is empty.", status_code=422)
    if os.name == "nt":
        parts = [strip_matching_quotes(part) for part in parts]
    return parts


def strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def gitignore_contains_entry(text: str, entry: str) -> bool:
    normalized_entry = entry.replace("\\", "/").strip().rstrip("/") + "/"
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        normalized_line = stripped.replace("\\", "/").rstrip("/") + "/"
        if normalized_line == normalized_entry:
            return True
    return False


def command_hash(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()
