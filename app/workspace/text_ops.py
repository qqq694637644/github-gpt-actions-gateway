from __future__ import annotations

import difflib
import hashlib
import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.errors import ApiError, ErrorCode
from app.models.common import ChangedFile
from app.policy.rules import Policy

PatchKind = Literal["update", "add", "delete"]

_BINARY_PATCH_MARKERS = (
    "GIT binary patch",
    "Binary files ",
    "Binary file ",
)


@dataclass(frozen=True)
class TextPatchHunk:
    old_lines: list[str]
    new_lines: list[str]


@dataclass(frozen=True)
class TextPatchOperation:
    kind: PatchKind
    path: str
    hunks: list[TextPatchHunk] = field(default_factory=list)
    add_lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    resolved_path: Path
    existed: bool
    data: bytes | None


@dataclass(frozen=True)
class PreparedFileChange:
    path: str
    resolved_path: Path
    before: bytes | None
    after: bytes | None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def assert_payload_size(data: bytes, *, max_bytes: int, error_code: ErrorCode, label: str) -> None:
    if len(data) > max_bytes:
        raise ApiError(
            error_code,
            f"{label} is too large: {len(data)} bytes > {max_bytes} bytes.",
            status_code=413,
            details={"actual_bytes": len(data), "max_bytes": max_bytes},
        )


def assert_text_bytes(data: bytes, *, path: str | None = None, error_code: ErrorCode = ErrorCode.WORKSPACE_BINARY_NOT_ALLOWED) -> None:
    if b"\x00" in data:
        raise ApiError(error_code, "NUL bytes are not allowed in workspace text operations.", status_code=403, details={"path": path} if path else {})
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApiError(error_code, "Only UTF-8 text files are allowed in workspace text operations.", status_code=403, details={"path": path} if path else {}) from exc


def resolve_workspace_file(repo_dir: Path, normalized_path: str, *, error_code: ErrorCode) -> Path:
    repo_root = repo_dir.resolve()
    candidate = repo_dir / normalized_path
    if candidate.is_symlink():
        raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Workspace text operations refuse to write through symlinks.", status_code=403, details={"path": normalized_path})
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ApiError(error_code, "Resolved path escapes the workspace repository.", status_code=403, details={"path": normalized_path}) from exc
    return resolved


def validate_write_target(policy: Policy, repo_dir: Path, path: str, *, operation: str, error_code: ErrorCode) -> tuple[str, Path]:
    try:
        normalized = policy.assert_write_path_allowed(path, operation=operation)
    except ApiError as exc:
        if exc.error_code == ErrorCode.DELETE_NOT_ALLOWED:
            mapped = ErrorCode.WORKSPACE_DELETE_NOT_ALLOWED
        elif exc.status_code == 400:
            mapped = error_code
        else:
            mapped = ErrorCode.WORKSPACE_POLICY_VIOLATION
        raise ApiError(mapped, exc.message, status_code=exc.status_code, suggestion=exc.suggestion, details=exc.details) from exc
    if normalized == ".":
        raise ApiError(error_code, "Workspace text operations require a file path, not '.'.", status_code=400)
    resolved = resolve_workspace_file(repo_dir, normalized, error_code=error_code)
    return normalized, resolved


def snapshot_files(repo_dir: Path, paths: list[str]) -> list[FileSnapshot]:
    snapshots: list[FileSnapshot] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        resolved = resolve_workspace_file(repo_dir, path, error_code=ErrorCode.WORKSPACE_POLICY_VIOLATION)
        if resolved.exists():
            if not resolved.is_file():
                raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Workspace text operations only support files.", status_code=403, details={"path": path})
            snapshots.append(FileSnapshot(path=path, resolved_path=resolved, existed=True, data=resolved.read_bytes()))
        else:
            snapshots.append(FileSnapshot(path=path, resolved_path=resolved, existed=False, data=None))
    return snapshots


def parse_codex_patch(patch: str, policy: Policy, repo_dir: Path, *, allow_delete: bool, max_changed_files: int) -> list[TextPatchOperation]:
    payload = patch.encode("utf-8")
    assert_text_bytes(payload, error_code=ErrorCode.WORKSPACE_BINARY_NOT_ALLOWED)
    if any(marker in patch for marker in _BINARY_PATCH_MARKERS):
        raise ApiError(ErrorCode.WORKSPACE_BINARY_NOT_ALLOWED, "Binary patches are not allowed.", status_code=403)
    lines = patch.splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch" or lines[-1].strip() != "*** End Patch":
        raise ApiError(ErrorCode.WORKSPACE_PATCH_INVALID, "Patch must start with '*** Begin Patch' and end with '*** End Patch'.", status_code=400)

    operations: list[TextPatchOperation] = []
    paths_seen: set[str] = set()
    idx = 1
    while idx < len(lines) - 1:
        line = lines[idx]
        if not line.strip():
            idx += 1
            continue
        if line.startswith("*** Update File: "):
            raw_path = line.removeprefix("*** Update File: ").strip()
            path, resolved = validate_write_target(policy, repo_dir, raw_path, operation="modified", error_code=ErrorCode.WORKSPACE_PATCH_INVALID)
            if not resolved.exists() or not resolved.is_file():
                raise ApiError(ErrorCode.WORKSPACE_PATCH_INVALID, "Update File target does not exist as a file.", status_code=400, details={"path": path})
            body, idx = _collect_operation_body(lines, idx + 1)
            hunks = _parse_update_hunks(body, path)
            operations.append(TextPatchOperation(kind="update", path=path, hunks=hunks))
        elif line.startswith("*** Add File: "):
            raw_path = line.removeprefix("*** Add File: ").strip()
            path, resolved = validate_write_target(policy, repo_dir, raw_path, operation="added", error_code=ErrorCode.WORKSPACE_PATCH_INVALID)
            if resolved.exists():
                raise ApiError(ErrorCode.WORKSPACE_PATCH_INVALID, "Add File target already exists.", status_code=409, details={"path": path})
            body, idx = _collect_operation_body(lines, idx + 1)
            operations.append(TextPatchOperation(kind="add", path=path, add_lines=_parse_add_file_lines(body, path)))
        elif line.startswith("*** Delete File: "):
            raw_path = line.removeprefix("*** Delete File: ").strip()
            if not allow_delete:
                raise ApiError(ErrorCode.WORKSPACE_DELETE_NOT_ALLOWED, "Delete File is disabled for this request.", status_code=403, details={"path": raw_path})
            path, resolved = validate_write_target(policy, repo_dir, raw_path, operation="deleted", error_code=ErrorCode.WORKSPACE_PATCH_INVALID)
            if not resolved.exists() or not resolved.is_file():
                raise ApiError(ErrorCode.WORKSPACE_PATCH_INVALID, "Delete File target does not exist as a file.", status_code=400, details={"path": path})
            body, idx = _collect_operation_body(lines, idx + 1)
            if any(item.strip() for item in body):
                raise ApiError(ErrorCode.WORKSPACE_PATCH_INVALID, "Delete File sections cannot contain file content.", status_code=400, details={"path": path})
            operations.append(TextPatchOperation(kind="delete", path=path))
        else:
            raise ApiError(ErrorCode.WORKSPACE_PATCH_INVALID, "Unsupported patch operation.", status_code=400, details={"line": line})
        paths_seen.add(operations[-1].path)
        if len(paths_seen) > max_changed_files:
            raise ApiError(ErrorCode.WORKSPACE_TOO_MANY_CHANGED_FILES, "Patch changes too many files.", status_code=413, details={"count": len(paths_seen), "max": max_changed_files})

    if not operations:
        raise ApiError(ErrorCode.WORKSPACE_PATCH_INVALID, "Patch does not contain any file operations.", status_code=400)
    return operations


def prepare_text_patch(
    repo_dir: Path,
    operations: list[TextPatchOperation],
    snapshots: list[FileSnapshot],
) -> list[PreparedFileChange]:
    current = {snapshot.path: snapshot.data for snapshot in snapshots}
    for operation in operations:
        if operation.kind == "add":
            current[operation.path] = _join_lines(
                operation.add_lines,
                trailing_newline=bool(operation.add_lines),
            ).encode("utf-8")
        elif operation.kind == "delete":
            current[operation.path] = None
        else:
            original = current[operation.path]
            if original is None:
                raise ApiError(
                    ErrorCode.WORKSPACE_PATCH_CONTEXT_MISMATCH,
                    "Patch update target no longer exists.",
                    status_code=409,
                    details={"path": operation.path},
                )
            assert_text_bytes(original, path=operation.path)
            original_text = original.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            lines, trailing = _split_text_lines(original_text)
            new_lines = _apply_hunks(lines, operation.hunks, operation.path)
            current[operation.path] = _join_lines(new_lines, trailing_newline=trailing).encode("utf-8")
    return [
        PreparedFileChange(
            path=snapshot.path,
            resolved_path=snapshot.resolved_path,
            before=snapshot.data,
            after=current[snapshot.path],
        )
        for snapshot in snapshots
        if snapshot.data != current[snapshot.path]
    ]


def prepare_write_change(
    *,
    path: str,
    resolved_path: Path,
    before: bytes | None,
    after: bytes,
) -> list[PreparedFileChange]:
    if before == after:
        return []
    return [
        PreparedFileChange(
            path=path,
            resolved_path=resolved_path,
            before=before,
            after=after,
        )
    ]


def describe_prepared_changes(changes: list[PreparedFileChange]) -> tuple[list[ChangedFile], str]:
    changed_files: list[ChangedFile] = []
    for change in changes:
        if change.before is None:
            operation = "added"
        elif change.after is None:
            operation = "deleted"
        else:
            operation = "modified"
        additions, deletions = _line_change_counts(change.before, change.after)
        changed_files.append(
            ChangedFile(
                path=change.path,
                operation=operation,
                additions=additions,
                deletions=deletions,
            )
        )

    stat_lines = [
        f" {item.path} | {item.additions + item.deletions} "
        f"{'+' * min(item.additions, 40)}{'-' * min(item.deletions, 40)}"
        for item in changed_files
    ]
    if changed_files:
        total_additions = sum(item.additions for item in changed_files)
        total_deletions = sum(item.deletions for item in changed_files)
        stat_lines.append(
            f" {len(changed_files)} file(s) changed, "
            f"{total_additions} insertion(s)(+), {total_deletions} deletion(s)(-)"
        )
    return changed_files, "\n".join(stat_lines)


def commit_prepared_changes(repo_dir: Path, changes: list[PreparedFileChange]) -> None:
    repo_root = repo_dir.resolve()
    transaction_parent = _git_dir(repo_root) / "gpt-workspace-transactions"
    transaction_dir = transaction_parent / ("txn_" + secrets.token_hex(12))
    staged_dir = transaction_dir / "staged"
    backup_dir = transaction_dir / "backups"
    staged: dict[str, Path] = {}
    backups: dict[str, Path] = {}
    committed: list[PreparedFileChange] = []
    created_dirs: set[Path] = set()
    transaction_dir.mkdir(parents=True, exist_ok=False)
    try:
        for index, change in enumerate(changes):
            if change.after is not None:
                staged_dir.mkdir(parents=True, exist_ok=True)
                temporary = staged_dir / f"{index:04d}.stage"
                staged[change.path] = temporary
                temporary.write_bytes(change.after)

        for index, change in enumerate(changes):
            target = change.resolved_path
            created_dirs.update(_missing_parent_dirs(target.parent, repo_root))
            target.parent.mkdir(parents=True, exist_ok=True)
            if change.before is not None:
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_path = backup_dir / f"{index:04d}.backup"
                os.replace(target, backup_path)
                backups[change.path] = backup_path
            committed.append(change)
            if change.after is not None:
                temporary = staged[change.path]
                os.replace(temporary, target)
                staged.pop(change.path)

    except Exception as original:
        rollback_errors = _rollback_committed_changes(committed, backups)
        _remove_created_dirs(created_dirs, repo_root)
        cleanup_error = _cleanup_transaction_dir(transaction_dir, transaction_parent)
        if rollback_errors or cleanup_error is not None:
            details = "; ".join([*rollback_errors, *([str(cleanup_error)] if cleanup_error else [])])
            raise OSError(f"Workspace transaction recovery was incomplete: {details}") from original
        raise
    cleanup_error = _cleanup_transaction_dir(transaction_dir, transaction_parent)
    if cleanup_error is not None:
        raise OSError(
            "Workspace changes were committed, but transaction cleanup failed. "
            f"The committed files were left intact: {cleanup_error}"
        ) from cleanup_error


def _git_dir(repo_root: Path) -> Path:
    marker = repo_root / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        text = marker.read_text(encoding="utf-8").strip()
        if text.lower().startswith("gitdir:"):
            value = text.split(":", 1)[1].strip()
            candidate = Path(value)
            return candidate.resolve() if candidate.is_absolute() else (repo_root / candidate).resolve()
    raise OSError(f"Git metadata directory was not found for {repo_root}")


def _rollback_committed_changes(
    committed: list[PreparedFileChange],
    backups: dict[str, Path],
) -> list[str]:
    errors: list[str] = []
    for change in reversed(committed):
        target = change.resolved_path
        try:
            if change.before is not None:
                backup = backups.get(change.path)
                if backup is None or not backup.exists():
                    errors.append(
                        f"{change.path}: backup is unavailable; the current target was left intact"
                    )
                    continue
                if target.exists() and target.is_file():
                    target.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, target)
            elif target.exists() and target.is_file():
                target.unlink()
        except OSError as exc:
            errors.append(f"{change.path}: {exc}")
    return errors


def _remove_created_dirs(created_dirs: set[Path], repo_root: Path) -> None:
    for directory in sorted(created_dirs, key=lambda item: len(item.parts), reverse=True):
        if directory == repo_root:
            continue
        try:
            directory.rmdir()
        except OSError:
            pass


def _cleanup_transaction_dir(transaction_dir: Path, transaction_parent: Path) -> OSError | None:
    try:
        shutil.rmtree(transaction_dir)
        try:
            transaction_parent.rmdir()
        except OSError:
            pass
        return None
    except OSError as exc:
        return exc


def _missing_parent_dirs(path: Path, repo_root: Path) -> set[Path]:
    missing: set[Path] = set()
    current = path
    while current != repo_root and repo_root in current.parents and not current.exists():
        missing.add(current)
        current = current.parent
    return missing


def _line_change_counts(before: bytes | None, after: bytes | None) -> tuple[int, int]:
    old_lines = [] if before is None else before.decode("utf-8", errors="replace").splitlines(keepends=True)
    new_lines = [] if after is None else after.decode("utf-8", errors="replace").splitlines(keepends=True)
    additions = 0
    deletions = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old_lines, b=new_lines).get_opcodes():
        if tag in {"insert", "replace"}:
            additions += j2 - j1
        if tag in {"delete", "replace"}:
            deletions += i2 - i1
    return additions, deletions


def _collect_operation_body(lines: list[str], start: int) -> tuple[list[str], int]:
    end = start
    while end < len(lines) - 1 and not lines[end].startswith("*** Update File: ") and not lines[end].startswith("*** Add File: ") and not lines[end].startswith("*** Delete File: "):
        end += 1
    return lines[start:end], end


def _parse_add_file_lines(body: list[str], path: str) -> list[str]:
    output: list[str] = []
    for line in body:
        if line == "":
            continue
        if not line.startswith("+"):
            raise ApiError(ErrorCode.WORKSPACE_PATCH_INVALID, "Add File content lines must start with '+'.", status_code=400, details={"path": path, "line": line})
        output.append(line[1:])
    return output


def _parse_update_hunks(body: list[str], path: str) -> list[TextPatchHunk]:
    hunks: list[TextPatchHunk] = []
    current: list[str] | None = None
    for line in body:
        if line.startswith("@@"):
            if current is not None:
                hunks.append(_build_hunk(current, path))
            current = []
            continue
        if current is None:
            if not line.strip():
                continue
            raise ApiError(ErrorCode.WORKSPACE_PATCH_INVALID, "Update File sections must contain '@@' hunks.", status_code=400, details={"path": path, "line": line})
        if line.startswith("\\ No newline at end of file"):
            continue
        if line == "":
            raise ApiError(ErrorCode.WORKSPACE_PATCH_INVALID, "Patch hunk lines must start with ' ', '+', or '-'.", status_code=400, details={"path": path})
        if line[0] not in {" ", "+", "-"}:
            raise ApiError(ErrorCode.WORKSPACE_PATCH_INVALID, "Patch hunk lines must start with ' ', '+', or '-'.", status_code=400, details={"path": path, "line": line})
        current.append(line)
    if current is not None:
        hunks.append(_build_hunk(current, path))
    if not hunks:
        raise ApiError(ErrorCode.WORKSPACE_PATCH_INVALID, "Update File operation has no hunks.", status_code=400, details={"path": path})
    return hunks


def _build_hunk(lines: list[str], path: str) -> TextPatchHunk:
    old_lines: list[str] = []
    new_lines: list[str] = []
    for line in lines:
        marker = line[0]
        value = line[1:]
        if marker == " ":
            old_lines.append(value)
            new_lines.append(value)
        elif marker == "-":
            old_lines.append(value)
        elif marker == "+":
            new_lines.append(value)
    if not old_lines and not new_lines:
        raise ApiError(ErrorCode.WORKSPACE_PATCH_INVALID, "Empty patch hunk is not allowed.", status_code=400, details={"path": path})
    return TextPatchHunk(old_lines=old_lines, new_lines=new_lines)


def _apply_hunks(lines: list[str], hunks: list[TextPatchHunk], path: str) -> list[str]:
    current = list(lines)
    cursor = 0
    for hunk in hunks:
        if hunk.old_lines:
            idx = _find_subsequence(current, hunk.old_lines, cursor)
            if idx < 0 and cursor > 0:
                idx = _find_subsequence(current, hunk.old_lines, 0)
            if idx < 0:
                raise ApiError(ErrorCode.WORKSPACE_PATCH_CONTEXT_MISMATCH, "Patch context did not match the current file content.", status_code=409, details={"path": path})
            current = current[:idx] + hunk.new_lines + current[idx + len(hunk.old_lines) :]
            cursor = idx + len(hunk.new_lines)
        else:
            current = current[:cursor] + hunk.new_lines + current[cursor:]
            cursor += len(hunk.new_lines)
    return current


def _find_subsequence(lines: list[str], needle: list[str], start: int) -> int:
    if not needle:
        return start
    last_start = len(lines) - len(needle)
    for idx in range(max(start, 0), last_start + 1):
        if lines[idx : idx + len(needle)] == needle:
            return idx
    return -1


def _split_text_lines(text: str) -> tuple[list[str], bool]:
    if text == "":
        return [], False
    parts = text.split("\n")
    trailing = parts[-1] == ""
    if trailing:
        parts = parts[:-1]
    return parts, trailing


def _join_lines(lines: list[str], *, trailing_newline: bool) -> str:
    text = "\n".join(lines)
    if trailing_newline:
        text += "\n"
    return text


def normalize_line_endings(content: str, *, line_ending: str, previous_bytes: bytes | None) -> str:
    if line_ending == "preserve":
        if previous_bytes and b"\r\n" in previous_bytes and previous_bytes.count(b"\r\n") >= previous_bytes.count(b"\n"):
            line_ending = "crlf"
        else:
            return content
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if line_ending == "lf":
        return normalized
    if line_ending == "crlf":
        return normalized.replace("\n", "\r\n")
    raise ApiError(ErrorCode.VALIDATION_ERROR, "Unsupported line ending mode.", status_code=422, details={"line_ending": line_ending})
