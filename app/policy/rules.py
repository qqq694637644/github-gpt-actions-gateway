from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
from pathlib import PurePosixPath

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_PURPOSE_RE = re.compile(r"[^a-z0-9-]+")

ALLOW_WRITE_PATHS = {".env.example", ".env.sample", ".env.template"}
DENY_WRITE_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.key",
    "*.crt",
    "*.cer",
    "secrets/**",
    "credentials/**",
    "node_modules/**",
    ".venv/**",
    "venv/**",
    "env/**",
    "myvenv/**",
    ".tox/**",
    ".nox/**",
    "dist/**",
    "build/**",
    "coverage/**",
    ".git/**",
]
WORKFLOW_PATTERNS = [".github/workflows/*"]
LOCAL_ENV_DIRS = {".venv", "venv", "env", "myvenv", ".tox", ".nox"}
DENY_WRITE_DIRS = {".git", "dist", "build", "node_modules", "vendor", ".next", ".turbo", "coverage", *LOCAL_ENV_DIRS}
BINARY_DENY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tgz",
    ".bz2",
    ".7z",
    ".rar",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".wasm",
    ".jar",
    ".class",
    ".pyc",
    ".pyo",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
}


def is_sha(value: str | None) -> bool:
    return bool(value and _SHA_RE.fullmatch(value))


def branch_matches(branch: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(branch, pattern) for pattern in patterns)


def normalize_path(path: str) -> str:
    raw = path.replace("\\", "/").strip()
    if raw == ".":
        return "."
    if not raw or raw.startswith("/"):
        raise ApiError(ErrorCode.PATH_NOT_ALLOWED, "Path must be a non-empty relative POSIX path.", status_code=400)
    parts = PurePosixPath(raw).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ApiError(ErrorCode.PATH_NOT_ALLOWED, "Path cannot contain '.', '..', or empty segments.", status_code=400)
    return "/".join(parts)


def sanitize_purpose_slug(value: str) -> str:
    slug = value.lower().strip().replace("_", "-")
    slug = _PURPOSE_RE.sub("-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return (slug or "task")[:48]


class Policy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def assert_repo_allowed(self, owner: str, repo: str) -> None:
        if self.settings.allow_all_repos:
            return
        full_name = f"{owner}/{repo}".lower()
        allowed = self.settings.allowed_repo_set
        if not allowed:
            raise ApiError(
                ErrorCode.REPO_NOT_ALLOWED,
                "No repositories are allowed by configuration.",
                status_code=403,
                suggestion="Set ALLOWED_REPOS or set ALLOW_ALL_REPOS=true only after review.",
                details={"repo": full_name},
            )
        if full_name not in allowed:
            raise ApiError(
                ErrorCode.REPO_NOT_ALLOWED,
                f"Repository {owner}/{repo} is not allowed.",
                status_code=403,
                suggestion="Add this repository to ALLOWED_REPOS or use an allowed repository.",
                details={"repo": full_name},
            )

    def assert_read_ref_allowed(self, ref: str) -> None:
        if is_sha(ref):
            return
        if not branch_matches(ref, self.settings.read_branch_patterns):
            raise ApiError(
                ErrorCode.BRANCH_NOT_ALLOWED,
                f"Reading ref {ref!r} is not allowed.",
                status_code=403,
                suggestion="Use READ_BRANCH_ALLOWLIST or read by exact commit SHA.",
                details={"ref": ref, "allowlist": self.settings.read_branch_patterns},
            )

    def assert_write_branch_allowed(self, branch: str) -> None:
        if not branch or not branch.strip():
            raise ApiError(ErrorCode.BRANCH_NOT_ALLOWED, "Branch name must be non-empty.", status_code=400)

    def assert_workspace_path_allowed(self, path: str) -> str:
        return normalize_path(path)

    def assert_tree_path_allowed(self, path: str | None) -> str | None:
        if path is None or not path.strip():
            return None
        return normalize_path(path)

    def assert_write_path_allowed(self, path: str, *, operation: str = "upsert") -> str:
        normalized = normalize_path(path)
        if normalized == ".":
            return normalized
        parts = PurePosixPath(normalized).parts
        if operation == "deleted" and any(part in LOCAL_ENV_DIRS for part in parts):
            return normalized
        if any(part in DENY_WRITE_DIRS for part in parts):
            raise ApiError(ErrorCode.PATH_NOT_ALLOWED, f"Writing to generated or dependency path {normalized!r} is not allowed.", status_code=403)
        if operation == "deleted" and not self.settings.allow_delete_files:
            raise ApiError(
                ErrorCode.DELETE_NOT_ALLOWED,
                "Deleting files is disabled.",
                status_code=403,
                suggestion="Set ALLOW_DELETE_FILES=true only after reviewing the risk.",
                details={"path": normalized},
            )
        if any(fnmatch.fnmatchcase(normalized, pattern) for pattern in WORKFLOW_PATTERNS) and not self.settings.allow_workflow_edit:
            raise ApiError(
                ErrorCode.WORKFLOW_EDIT_NOT_ALLOWED,
                "Editing GitHub workflow files is disabled.",
                status_code=403,
                suggestion="Set ALLOW_WORKFLOW_EDIT=true only after reviewing workflow risk.",
                details={"path": normalized},
            )
        for pattern in DENY_WRITE_PATTERNS:
            if normalized not in ALLOW_WRITE_PATHS and fnmatch.fnmatchcase(normalized, pattern):
                raise ApiError(ErrorCode.PATH_NOT_ALLOWED, f"Path {normalized!r} is blocked by write policy.", status_code=403)
        if self.has_binary_extension(normalized):
            raise ApiError(ErrorCode.BINARY_FILE_NOT_ALLOWED, "Binary-like files cannot be written by this gateway.", status_code=403, details={"path": normalized})
        return normalized

    @staticmethod
    def has_binary_extension(path: str) -> bool:
        return PurePosixPath(path).suffix.lower() in BINARY_DENY_EXTENSIONS

    @staticmethod
    def looks_binary(data: bytes) -> bool:
        if not data:
            return False
        if b"\x00" in data[:4096]:
            return True
        control = sum(1 for byte in data[:4096] if byte < 9 or (13 < byte < 32))
        return control / min(len(data), 4096) > 0.20

    def assert_file_size(self, size: int, *, max_size: int, error_code: ErrorCode = ErrorCode.FILE_TOO_LARGE) -> None:
        if size > max_size:
            raise ApiError(error_code, f"File or payload is too large: {size} bytes > {max_size} bytes.", status_code=413, details={"actual_bytes": size, "max_bytes": max_size})
