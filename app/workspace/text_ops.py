from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.errors import ApiError, ErrorCode
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


def restore_files(repo_dir: Path, snapshots: list[FileSnapshot]) -> None:
    repo_root = repo_dir.resolve()
    for snapshot in snapshots:
        if snapshot.existed:
            snapshot.resolved_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot.resolved_path.write_bytes(snapshot.data or b"")
        else:
            try:
                if snapshot.resolved_path.exists() and snapshot.resolved_path.is_file():
                    snapshot.resolved_path.unlink()
            finally:
                _remove_empty_parents(snapshot.resolved_path.parent, repo_root)


def _remove_empty_parents(path: Path, stop_at: Path) -> None:
    current = path.resolve(strict=False)
    while current != stop_at:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


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


def apply_text_patch(repo_dir: Path, operations: list[TextPatchOperation]) -> list[str]:
    changed_paths: list[str] = []
    for operation in operations:
        file_path = resolve_workspace_file(repo_dir, operation.path, error_code=ErrorCode.WORKSPACE_POLICY_VIOLATION)
        if operation.kind == "add":
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(_join_lines(operation.add_lines, trailing_newline=bool(operation.add_lines)), encoding="utf-8", newline="")
        elif operation.kind == "delete":
            file_path.unlink()
        else:
            original = file_path.read_bytes()
            assert_text_bytes(original, path=operation.path)
            original_text = original.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            lines, trailing = _split_text_lines(original_text)
            new_lines = _apply_hunks(lines, operation.hunks, operation.path)
            file_path.write_text(_join_lines(new_lines, trailing_newline=trailing), encoding="utf-8", newline="")
        changed_paths.append(operation.path)
    return changed_paths


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
