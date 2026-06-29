from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.errors import ApiError, ErrorCode


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def canonical_hash(payload: Any) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class AuditStore:
    def __init__(self, db_url: str) -> None:
        self.db_path = self._parse_sqlite_url(db_url)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    @staticmethod
    def _parse_sqlite_url(db_url: str) -> Path:
        if db_url.startswith("sqlite:///"):
            raw_path = db_url[len("sqlite:///") :]
            path = Path(raw_path)
            return path if path.is_absolute() else path.resolve()
        return Path(db_url).resolve()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT,
                    method TEXT,
                    path TEXT,
                    status_code INTEGER,
                    repo TEXT,
                    branch TEXT,
                    idempotency_key TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    workspace_id TEXT,
                    branch TEXT,
                    head_sha_before TEXT,
                    head_sha_after TEXT,
                    changed_files_json TEXT,
                    command_hash TEXT,
                    exit_code INTEGER,
                    duration_ms INTEGER,
                    metadata_json TEXT,
                    actor TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(workspace_audit_events)").fetchall()}
            if "metadata_json" not in columns:
                self._conn.execute("ALTER TABLE workspace_audit_events ADD COLUMN metadata_json TEXT")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    scope TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (scope, idempotency_key)
                )
                """
            )
            self._conn.commit()

    def record_event(
        self,
        *,
        request_id: str | None,
        method: str,
        path: str,
        status_code: int,
        repo: str | None = None,
        branch: str | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit_events(request_id, method, path, status_code, repo, branch, idempotency_key, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, method, path, status_code, repo, branch, idempotency_key, json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True), utc_now_iso()),
            )
            self._conn.commit()

    def record_workspace_operation(
        self,
        *,
        operation_id: str,
        owner: str,
        repo: str,
        workspace_id: str | None = None,
        branch: str | None = None,
        head_sha_before: str | None = None,
        head_sha_after: str | None = None,
        changed_files: list[dict[str, Any]] | None = None,
        command_hash: str | None = None,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
        actor: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO workspace_audit_events(
                    operation_id, owner, repo, workspace_id, branch, head_sha_before, head_sha_after,
                    changed_files_json, command_hash, exit_code, duration_ms, metadata_json, actor, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    owner,
                    repo,
                    workspace_id,
                    branch,
                    head_sha_before,
                    head_sha_after,
                    json.dumps(changed_files or [], ensure_ascii=False, sort_keys=True),
                    command_hash,
                    exit_code,
                    duration_ms,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    actor,
                    utc_now_iso(),
                ),
            )
            self._conn.commit()

    def get_idempotent_response(self, *, scope: str, key: str, request_payload: Any) -> dict[str, Any] | None:
        request_hash = canonical_hash(request_payload)
        with self._lock:
            row = self._conn.execute(
                "SELECT request_hash, response_json FROM idempotency_records WHERE scope=? AND idempotency_key=?",
                (scope, key),
            ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ApiError(
                ErrorCode.IDEMPOTENCY_KEY_REUSED,
                "The same idempotency_key was reused with a different request payload.",
                status_code=409,
                suggestion="Use a new idempotency_key for a different operation.",
                details={"scope": scope, "idempotency_key": key},
            )
        return json.loads(row["response_json"])

    def save_idempotent_response(self, *, scope: str, key: str, request_payload: Any, response_payload: Any) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO idempotency_records(scope, idempotency_key, request_hash, response_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, idempotency_key) DO UPDATE SET
                    response_json=excluded.response_json,
                    updated_at=excluded.updated_at
                """,
                (scope, key, canonical_hash(request_payload), json.dumps(response_payload, ensure_ascii=False, sort_keys=True), now, now),
            )
            self._conn.commit()
