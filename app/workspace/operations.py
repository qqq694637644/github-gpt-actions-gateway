from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import secrets
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.storage.audit import canonical_hash
from app.workspace.exec import build_pwsh_script, strip_ansi_escape_sequences
from app.workspace.security import sanitized_environment, validate_script

OperationState = Literal[
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "canceled",
    "interrupted",
]
_TERMINAL_STATES: set[str] = {
    "succeeded",
    "failed",
    "timed_out",
    "canceled",
    "interrupted",
}
T = TypeVar("T")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class WindowsJob:
    """Small ctypes wrapper around a kill-on-close Windows Job Object."""

    def __init__(self) -> None:
        self.handle: int | None = None
        self.assigned = False
        if os.name != "nt":
            return

        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise OSError(error, "SetInformationJobObject failed")
        self.handle = int(handle)

    def assign(self, pid: int) -> None:
        if os.name != "nt" or self.handle is None:
            self.assigned = True
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        process = kernel32.OpenProcess(0x0100 | 0x0001 | 0x0400, False, pid)
        if not process:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not kernel32.AssignProcessToJobObject(ctypes.c_void_p(self.handle), process):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
            self.assigned = True
        finally:
            kernel32.CloseHandle(process)

    def terminate(self, exit_code: int = 1) -> None:
        if os.name != "nt" or self.handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject.restype = ctypes.c_bool
        kernel32.TerminateJobObject(ctypes.c_void_p(self.handle), exit_code)

    def close(self) -> None:
        if os.name == "nt" and self.handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None


class OperationDeadlineExceeded(Exception):
    pass


@dataclass(slots=True)
class OperationRuntime:
    record: dict[str, Any]
    started_monotonic: float
    deadline_monotonic: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    process: asyncio.subprocess.Process | None = None
    job: WindowsJob | None = None
    stored_bytes: int = 0
    last_progress_persist_monotonic: float = 0.0


class WorkspaceOperationManager:
    def __init__(self, settings: Settings, *, recover_running: bool = True) -> None:
        self.settings = settings
        self.root = Path(settings.workspace_operation_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._registry_lock = asyncio.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self._runtimes: dict[str, OperationRuntime] = {}
        self._idempotency: dict[tuple[str, str, str], str] = {}
        self._background_cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._load_records()
        if recover_running:
            self.recover_running_operations()
            self.prune_terminal_operations()

    def _load_records(self) -> None:
        for directory in self.root.glob("op_*"):
            state_path = directory / "state.json"
            if not state_path.is_file():
                continue
            try:
                record = _read_json(state_path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            operation_id = str(record.get("operation_id") or directory.name)
            self._records[operation_id] = record
            key = record.get("idempotency_key")
            workspace_id = record.get("workspace_id")
            request_hash = record.get("request_hash")
            if (
                isinstance(key, str)
                and isinstance(workspace_id, str)
                and isinstance(request_hash, str)
            ):
                self._idempotency[(workspace_id, key, request_hash)] = operation_id

    def recover_running_operations(self) -> int:
        recovered = 0
        for record in self._records.values():
            if record.get("state") != "running":
                continue
            record["state"] = "interrupted"
            record["finished_at"] = _utc_now()
            record["error_code"] = "gateway_restarted"
            record["error_message"] = "Gateway restarted before the command reached a terminal state."
            self._write_record(record)
            recovered += 1
        return recovered

    async def start(
        self,
        *,
        workspace_id: str,
        repo_dir: Path,
        idempotency_key: str,
        script: str,
        timeout_seconds: int,
        max_output_bytes: int,
        allow_network: bool,
        plain_output: bool,
        utf8_output: bool,
        activate_python_venv: bool,
        python_venv_dir: str,
    ) -> dict[str, Any]:
        request_payload = {
            "workspace_id": workspace_id,
            "script": script,
            "timeout_seconds": timeout_seconds,
            "max_output_bytes": max_output_bytes,
            "allow_network": allow_network,
            "plain_output": plain_output,
            "utf8_output": utf8_output,
            "activate_python_venv": activate_python_venv,
            "python_venv_dir": python_venv_dir,
        }
        request_hash = canonical_hash(request_payload)
        idempotency_index = (workspace_id, idempotency_key, request_hash)
        async with self._registry_lock:
            self.prune_terminal_operations()
            existing_id = self._idempotency.get(idempotency_index)
            if existing_id:
                return self._public_record(self._records[existing_id])

            validate_script(script, allow_network=allow_network, settings=self.settings)

            operation_id = "op_" + secrets.token_hex(8)
            started_monotonic = time.monotonic()
            started_at = _utc_now()
            deadline_at = (datetime.now(UTC) + timedelta(seconds=timeout_seconds)).isoformat()
            record: dict[str, Any] = {
                "operation_id": operation_id,
                "workspace_id": workspace_id,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
                "script_summary": _script_summary(script),
                "state": "running",
                "root_pid": None,
                "job_assigned": False,
                "started_at": started_at,
                "deadline_at": deadline_at,
                "finished_at": None,
                "duration_ms": 0,
                "exit_code": None,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "error_code": None,
                "error_message": None,
                "plain_output": plain_output,
                "max_output_bytes": max_output_bytes,
            }
            runtime = OperationRuntime(
                record=record,
                started_monotonic=started_monotonic,
                deadline_monotonic=started_monotonic + timeout_seconds,
                last_progress_persist_monotonic=started_monotonic,
            )
            self._records[operation_id] = record
            self._runtimes[operation_id] = runtime
            self._idempotency[idempotency_index] = operation_id
            try:
                self._write_record(record)
            except OSError:
                self._records.pop(operation_id, None)
                self._runtimes.pop(operation_id, None)
                self._idempotency.pop(idempotency_index, None)
                raise
            runtime.task = asyncio.create_task(
                self._run(
                    runtime,
                    repo_dir=repo_dir,
                    script=script,
                    timeout_seconds=timeout_seconds,
                    max_output_bytes=max_output_bytes,
                    allow_network=allow_network,
                    plain_output=plain_output,
                    utf8_output=utf8_output,
                    activate_python_venv=activate_python_venv,
                    python_venv_dir=python_venv_dir,
                ),
                name=f"workspace-command-{operation_id}",
            )
            return self._public_record(record)

    async def get(self, workspace_id: str, operation_id: str) -> dict[str, Any]:
        record = self._require_operation(workspace_id, operation_id)
        return self._public_record(record)

    async def list_operations(self, workspace_id: str, state: str | None = None) -> list[dict[str, Any]]:
        records = [
            self._public_record(record)
            for record in self._records.values()
            if record.get("workspace_id") == workspace_id
            and (state is None or record.get("state") == state)
        ]
        records.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
        return records

    async def logs(
        self,
        workspace_id: str,
        operation_id: str,
        *,
        stdout_offset: int,
        stderr_offset: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        record = self._require_operation(workspace_id, operation_id)
        stdout, next_stdout = _read_log(self._stdout_path(operation_id), stdout_offset, max_bytes)
        stderr, next_stderr = _read_log(self._stderr_path(operation_id), stderr_offset, max_bytes)
        if record.get("plain_output"):
            stdout = strip_ansi_escape_sequences(stdout)
            stderr = strip_ansi_escape_sequences(stderr)
        terminal = record.get("state") in _TERMINAL_STATES
        return {
            "stdout": stdout,
            "stderr": stderr,
            "next_stdout_offset": next_stdout,
            "next_stderr_offset": next_stderr,
            "stdout_eof": terminal and next_stdout >= _file_size(self._stdout_path(operation_id)),
            "stderr_eof": terminal and next_stderr >= _file_size(self._stderr_path(operation_id)),
        }

    async def cancel(self, workspace_id: str, operation_id: str) -> dict[str, Any]:
        record = self._require_operation(workspace_id, operation_id)
        if record.get("state") in _TERMINAL_STATES:
            return self._public_record(record)
        runtime = self._runtimes.get(operation_id)
        if runtime is not None:
            runtime.cancel_event.set()
        return self._public_record(record)

    def active_for_workspace(self, workspace_id: str) -> list[dict[str, Any]]:
        return [
            self._public_record(record)
            for record in self._records.values()
            if record.get("workspace_id") == workspace_id and record.get("state") == "running"
        ]

    def active_workspace_ids(self) -> set[str]:
        return {
            str(record["workspace_id"])
            for record in self._records.values()
            if record.get("state") == "running" and record.get("workspace_id")
        }

    def active_operation_count(self) -> int:
        return sum(1 for record in self._records.values() if record.get("state") == "running")

    async def shutdown(self) -> None:
        runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            if runtime.record.get("state") == "running":
                runtime.shutdown_event.set()
        tasks = [runtime.task for runtime in runtimes if runtime.task is not None and not runtime.task.done()]
        if tasks:
            _, pending = await asyncio.wait(
                tasks,
                timeout=self.settings.workspace_command_shutdown_seconds,
            )
            if pending:
                for runtime in runtimes:
                    if runtime.task in pending:
                        runtime.task.cancel()
                    if runtime.job:
                        runtime.job.terminate(1)
                        runtime.job.close()
                    await self._finish(
                        runtime,
                        state="interrupted",
                        error_code="gateway_shutdown_timeout",
                        error_message="Gateway shutdown exceeded the command cleanup deadline.",
                    )
                await asyncio.wait(
                    pending,
                    timeout=self.settings.workspace_command_kill_grace_seconds,
                )

    def prune_terminal_operations(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(hours=self.settings.workspace_operation_ttl_hours)
        removed = 0
        for operation_id, record in list(self._records.items()):
            if record.get("state") not in _TERMINAL_STATES:
                continue
            finished_at = record.get("finished_at")
            try:
                finished = datetime.fromisoformat(str(finished_at))
            except (TypeError, ValueError):
                continue
            if finished >= cutoff:
                continue
            directory = self.root / operation_id
            try:
                import shutil

                shutil.rmtree(directory)
            except OSError:
                continue
            self._records.pop(operation_id, None)
            key = record.get("idempotency_key")
            workspace_id = record.get("workspace_id")
            request_hash = record.get("request_hash")
            if (
                isinstance(key, str)
                and isinstance(workspace_id, str)
                and isinstance(request_hash, str)
            ):
                self._idempotency.pop((workspace_id, key, request_hash), None)
            removed += 1
        return removed

    @staticmethod
    def _remaining_seconds(runtime: OperationRuntime) -> float:
        return max(0.0, runtime.deadline_monotonic - time.monotonic())

    async def _await_before_deadline(
        self,
        runtime: OperationRuntime,
        awaitable: Awaitable[T],
        *,
        on_late_result: Callable[[asyncio.Future[T]], None] | None = None,
    ) -> T:
        future = asyncio.ensure_future(awaitable)
        try:
            remaining = self._remaining_seconds(runtime)
            if remaining > 0:
                done, _ = await asyncio.wait({future}, timeout=remaining)
                if future in done:
                    return future.result()
        except asyncio.CancelledError:
            if on_late_result is not None:
                future.add_done_callback(on_late_result)
            else:
                future.cancel()
            raise
        if on_late_result is not None:
            future.add_done_callback(on_late_result)
        else:
            future.cancel()
        raise OperationDeadlineExceeded

    def _track_cleanup_task(self, task: asyncio.Task[Any]) -> None:
        self._background_cleanup_tasks.add(task)
        task.add_done_callback(self._background_cleanup_tasks.discard)

    async def _create_job_before_deadline(self, runtime: OperationRuntime) -> WindowsJob:
        def close_late_job(future: asyncio.Future[WindowsJob]) -> None:
            try:
                future.result().close()
            except BaseException:
                pass

        return await self._await_before_deadline(
            runtime,
            asyncio.to_thread(WindowsJob),
            on_late_result=close_late_job,
        )

    async def _create_process_before_deadline(
        self,
        runtime: OperationRuntime,
        *args: str,
        cwd: str,
        env: dict[str, str],
        creationflags: int,
        preexec_fn: Callable[[], None] | None,
    ) -> asyncio.subprocess.Process:
        def terminate_late_process(future: asyncio.Future[asyncio.subprocess.Process]) -> None:
            try:
                process = future.result()
            except BaseException:
                return
            cleanup = asyncio.create_task(
                _terminate_process_tree(
                    process,
                    None,
                    self.settings.workspace_command_kill_grace_seconds,
                )
            )
            self._track_cleanup_task(cleanup)

        return await self._await_before_deadline(
            runtime,
            asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
                preexec_fn=preexec_fn,
            ),
            on_late_result=terminate_late_process,
        )

    async def _assign_job_before_deadline(
        self,
        runtime: OperationRuntime,
        job: WindowsJob,
        process: asyncio.subprocess.Process,
    ) -> None:
        def finish_late_assignment(future: asyncio.Future[None]) -> None:
            try:
                future.result()
            except BaseException:
                pass
            job.terminate(1)
            job.close()

        try:
            await self._await_before_deadline(
                runtime,
                asyncio.to_thread(job.assign, process.pid),
                on_late_result=finish_late_assignment,
            )
        except (OperationDeadlineExceeded, asyncio.CancelledError):
            runtime.job = None
            await _terminate_process_tree(
                process,
                job,
                self.settings.workspace_command_kill_grace_seconds,
            )
            raise

    async def _persist_progress_if_due(self, runtime: OperationRuntime) -> None:
        now = time.monotonic()
        if (
            now - runtime.last_progress_persist_monotonic
            < self.settings.workspace_operation_progress_flush_seconds
        ):
            return
        runtime.last_progress_persist_monotonic = now
        try:
            await asyncio.to_thread(self._write_record, dict(runtime.record))
        except OSError:
            pass

    async def _run(
        self,
        runtime: OperationRuntime,
        *,
        repo_dir: Path,
        script: str,
        timeout_seconds: int,
        max_output_bytes: int,
        allow_network: bool,
        plain_output: bool,
        utf8_output: bool,
        activate_python_venv: bool,
        python_venv_dir: str,
    ) -> None:
        del allow_network
        started_monotonic = runtime.started_monotonic
        operation_id = str(runtime.record["operation_id"])
        ready_path = self.root / operation_id / "job.ready"
        prepared_script = build_pwsh_script(
            script,
            plain_output=plain_output,
            utf8_output=utf8_output,
            activate_python_venv=activate_python_venv,
            python_venv_dir=python_venv_dir,
        )
        effective_script = "\n".join(
            [
                "while (-not (Test-Path -LiteralPath $env:GATEWAY_JOB_READY_FILE)) { Start-Sleep -Milliseconds 10 }",
                "Remove-Item -LiteralPath $env:GATEWAY_JOB_READY_FILE -Force -ErrorAction SilentlyContinue",
                prepared_script,
            ]
        )
        args = [
            self.settings.workspace_shell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            effective_script,
        ]
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        preexec_fn = getattr(os, "setsid", None) if os.name != "nt" else None
        process_env = sanitized_environment()
        process_env["GATEWAY_JOB_READY_FILE"] = str(ready_path)
        try:
            job = await self._create_job_before_deadline(runtime)
            runtime.job = job
            proc = await self._create_process_before_deadline(
                runtime,
                *args,
                cwd=str(repo_dir),
                env=process_env,
                creationflags=creationflags,
                preexec_fn=preexec_fn,
            )
            runtime.process = proc
            try:
                await self._assign_job_before_deadline(runtime, job, proc)
            except OSError as exc:
                await _terminate_process_tree(
                    proc,
                    job,
                    self.settings.workspace_command_kill_grace_seconds,
                )
                raise ApiError(
                    ErrorCode.WORKSPACE_EXEC_FAILED,
                    "Unable to attach the PowerShell process to a Windows Job Object.",
                    status_code=500,
                    details={"pid": proc.pid, "error": str(exc)},
                ) from exc
            async with runtime.lock:
                runtime.record["root_pid"] = proc.pid
                runtime.record["job_assigned"] = job.assigned
            if self._remaining_seconds(runtime) <= 0:
                raise OperationDeadlineExceeded
            await self._await_before_deadline(
                runtime,
                asyncio.to_thread(ready_path.write_text, "ready", encoding="utf-8"),
            )

            stdout_task = asyncio.create_task(
                self._drain_stream(runtime, "stdout", proc.stdout, self._stdout_path(runtime.record["operation_id"]), max_output_bytes)
            )
            stderr_task = asyncio.create_task(
                self._drain_stream(runtime, "stderr", proc.stderr, self._stderr_path(runtime.record["operation_id"]), max_output_bytes)
            )
            process_task = asyncio.create_task(proc.wait())
            timeout_task = asyncio.create_task(
                asyncio.sleep(self._remaining_seconds(runtime))
            )
            cancel_task = asyncio.create_task(runtime.cancel_event.wait())
            shutdown_task = asyncio.create_task(runtime.shutdown_event.wait())
            done, pending = await asyncio.wait(
                {process_task, timeout_task, cancel_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if process_task in done:
                terminal_state: OperationState = "succeeded" if proc.returncode == 0 else "failed"
                error_code = None if proc.returncode == 0 else "command_failed"
                error_message = None if proc.returncode == 0 else f"PowerShell exited with code {proc.returncode}."
                if runtime.job:
                    runtime.job.terminate(0)
            elif cancel_task in done and runtime.cancel_event.is_set():
                terminal_state = "canceled"
                error_code = "command_canceled"
                error_message = "Command was canceled."
                await _terminate_process_tree(proc, runtime.job, self.settings.workspace_command_kill_grace_seconds)
            elif shutdown_task in done and runtime.shutdown_event.is_set():
                terminal_state = "interrupted"
                error_code = "gateway_shutdown"
                error_message = "Gateway shutdown interrupted the command."
                await _terminate_process_tree(proc, runtime.job, self.settings.workspace_command_kill_grace_seconds)
            else:
                terminal_state = "timed_out"
                error_code = "command_timeout"
                error_message = f"Command exceeded {timeout_seconds} seconds."
                await _terminate_process_tree(proc, runtime.job, self.settings.workspace_command_kill_grace_seconds)

            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=0.5)
            if not process_task.done():
                _, process_pending = await asyncio.wait(
                    {process_task},
                    timeout=self.settings.workspace_command_kill_grace_seconds,
                )
                if process_pending:
                    process_task.cancel()
            _, reader_pending = await asyncio.wait(
                {stdout_task, stderr_task},
                timeout=self.settings.workspace_command_reader_grace_seconds,
            )
            for task in reader_pending:
                task.cancel()

            await self._finish(
                runtime,
                state=terminal_state,
                exit_code=proc.returncode,
                duration_ms=round((time.monotonic() - started_monotonic) * 1000),
                error_code=error_code,
                error_message=error_message,
            )
        except OperationDeadlineExceeded:
            if runtime.process is not None and runtime.process.returncode is None:
                await _terminate_process_tree(
                    runtime.process,
                    runtime.job,
                    self.settings.workspace_command_kill_grace_seconds,
                )
            await self._finish(
                runtime,
                state="timed_out",
                exit_code=(runtime.process.returncode if runtime.process is not None else None),
                duration_ms=round((time.monotonic() - started_monotonic) * 1000),
                error_code="command_timeout",
                error_message=f"Command exceeded {timeout_seconds} seconds during startup.",
            )
        except asyncio.CancelledError:
            if runtime.process is not None:
                await _terminate_process_tree(
                    runtime.process,
                    runtime.job,
                    self.settings.workspace_command_kill_grace_seconds,
                )
            await self._finish(
                runtime,
                state="interrupted",
                duration_ms=round((time.monotonic() - started_monotonic) * 1000),
                error_code="operation_task_canceled",
                error_message="Command task was interrupted.",
            )
            raise
        except Exception as exc:
            await self._finish(
                runtime,
                state="failed",
                duration_ms=round((time.monotonic() - started_monotonic) * 1000),
                error_code=(exc.error_code if isinstance(exc, ApiError) else "command_start_failed"),
                error_message=(exc.message if isinstance(exc, ApiError) else str(exc)),
            )
        finally:
            if runtime.process is not None and runtime.process.returncode is None:
                await _terminate_process_tree(
                    runtime.process,
                    runtime.job,
                    self.settings.workspace_command_kill_grace_seconds,
                )
            try:
                ready_path.unlink()
            except FileNotFoundError:
                pass
            if runtime.job:
                runtime.job.close()
            self._runtimes.pop(str(runtime.record["operation_id"]), None)

    async def _drain_stream(
        self,
        runtime: OperationRuntime,
        stream_name: Literal["stdout", "stderr"],
        stream: asyncio.StreamReader | None,
        path: Path,
        max_output_bytes: int,
    ) -> None:
        if stream is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = path.open("ab")
        except OSError:
            handle = None
        try:
            while True:
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    return
                async with runtime.lock:
                    byte_field = f"{stream_name}_bytes"
                    truncated_field = f"{stream_name}_truncated"
                    runtime.record[byte_field] = int(runtime.record.get(byte_field) or 0) + len(chunk)
                    remaining = max(0, max_output_bytes - runtime.stored_bytes)
                    accepted = chunk[:remaining]
                    if accepted and handle is not None:
                        try:
                            handle.write(accepted)
                            handle.flush()
                            runtime.stored_bytes += len(accepted)
                        except OSError:
                            handle.close()
                            handle = None
                    if len(accepted) < len(chunk):
                        runtime.record[truncated_field] = True
                    if handle is None:
                        runtime.record[truncated_field] = True
                    await self._persist_progress_if_due(runtime)
        finally:
            if handle is not None:
                handle.close()

    async def _finish(
        self,
        runtime: OperationRuntime,
        *,
        state: OperationState,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        async with runtime.lock:
            if runtime.record.get("state") in _TERMINAL_STATES:
                return
            runtime.record["state"] = state
            runtime.record["finished_at"] = _utc_now()
            runtime.record["exit_code"] = exit_code
            if duration_ms is not None:
                runtime.record["duration_ms"] = duration_ms
            runtime.record["error_code"] = error_code
            runtime.record["error_message"] = error_message
            try:
                await asyncio.to_thread(self._write_record, dict(runtime.record))
            except OSError:
                pass

    def _require_operation(self, workspace_id: str, operation_id: str) -> dict[str, Any]:
        record = self._records.get(operation_id)
        if record is None or record.get("workspace_id") != workspace_id:
            raise ApiError(
                ErrorCode.WORKSPACE_NOT_FOUND,
                "Workspace command operation was not found.",
                status_code=404,
                details={"workspace_id": workspace_id, "operation_id": operation_id},
            )
        return record

    def _write_record(self, record: dict[str, Any]) -> None:
        _atomic_write_json(self._state_path(str(record["operation_id"])), record)

    def _state_path(self, operation_id: str) -> Path:
        return self.root / operation_id / "state.json"

    def _stdout_path(self, operation_id: str) -> Path:
        return self.root / operation_id / "stdout.log"

    def _stderr_path(self, operation_id: str) -> Path:
        return self.root / operation_id / "stderr.log"

    @staticmethod
    def _public_record(record: dict[str, Any]) -> dict[str, Any]:
        hidden = {"request_hash", "idempotency_key", "plain_output", "max_output_bytes"}
        return {key: value for key, value in record.items() if key not in hidden}


async def _terminate_process_tree(
    proc: asyncio.subprocess.Process,
    job: WindowsJob | None,
    grace_seconds: int,
) -> None:
    if job is not None:
        job.terminate(1)
    if os.name == "nt" and proc.pid:
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            killer_task = asyncio.create_task(killer.wait())
            _, killer_pending = await asyncio.wait(
                {killer_task},
                timeout=max(1, grace_seconds),
            )
            if killer_pending:
                killer.kill()
                killer_task.cancel()
        except FileNotFoundError:
            pass
    elif proc.returncode is None:
        try:
            killpg = getattr(os, "killpg", None)
            getpgid = getattr(os, "getpgid", None)
            if callable(killpg) and callable(getpgid):
                killpg(getpgid(proc.pid), getattr(signal, "SIGKILL", 9))
        except (ProcessLookupError, PermissionError):
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    process_wait = asyncio.create_task(proc.wait())
    _, process_pending = await asyncio.wait(
        {process_wait},
        timeout=max(1, grace_seconds),
    )
    for task in process_pending:
        task.cancel()


def _script_summary(script: str) -> str:
    compact = " ".join(script.strip().split())
    return compact[:200]


def _read_log(path: Path, offset: int, max_bytes: int) -> tuple[str, int]:
    if not path.is_file():
        return "", offset
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(max_bytes)
        next_offset = handle.tell()
    return data.decode("utf-8", errors="replace"), next_offset


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
