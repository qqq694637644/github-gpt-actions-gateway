from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Iterable
from pathlib import Path

from app.errors import ApiError, ErrorCode
from app.models.common import ChangedFile
from app.policy.rules import normalize_path
from app.workspace.models import CommandResult
from app.workspace.security import sanitized_environment


class GitRunner:
    def __init__(self, *, default_timeout: int = 60) -> None:
        self.default_timeout = default_timeout

    async def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        input_data: bytes | None = None,
        check: bool = True,
        allowed_exit_codes: Iterable[int] = (0,),
        max_output_bytes: int = 200_000,
    ) -> CommandResult:
        started = time.perf_counter()
        proc_env = sanitized_environment()
        if env:
            proc_env.update(env)
        preexec_fn = getattr(os, "setsid", None) if os.name != "nt" else None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(cwd) if cwd else None,
                env=proc_env,
                stdin=asyncio.subprocess.PIPE if input_data is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=preexec_fn,
            )
        except FileNotFoundError as exc:
            raise ApiError(ErrorCode.WORKSPACE_EXEC_FAILED, f"Executable not found: {args[0]}", status_code=500) from exc

        if input_data is not None and proc.stdin is not None:
            proc.stdin.write(input_data)
            await proc.stdin.drain()
            proc.stdin.close()

        stdout_task = asyncio.create_task(_drain_bounded(proc.stdout, max_output_bytes))
        stderr_task = asyncio.create_task(_drain_bounded(proc.stderr, max_output_bytes))
        wait_task = asyncio.create_task(proc.wait())
        timeout_task = asyncio.create_task(asyncio.sleep(timeout or self.default_timeout))
        done, _ = await asyncio.wait({wait_task, timeout_task}, return_when=asyncio.FIRST_COMPLETED)
        timed_out = timeout_task in done and wait_task not in done
        if timed_out:
            await kill_process_tree(proc)
            _, wait_pending = await asyncio.wait({wait_task}, timeout=5)
            if wait_pending:
                wait_task.cancel()
        else:
            timeout_task.cancel()
        reader_done, reader_pending = await asyncio.wait(
            {stdout_task, stderr_task},
            timeout=2,
        )
        for task in reader_pending:
            task.cancel()
        stdout_b = stdout_task.result() if stdout_task in reader_done and not stdout_task.cancelled() else b""
        stderr_b = stderr_task.result() if stderr_task in reader_done and not stderr_task.cancelled() else b""
        if timed_out:
            result = _decode_result(proc.returncode or -9, stdout_b, stderr_b, started, max_output_bytes, timed_out=True)
            raise ApiError(
                ErrorCode.WORKSPACE_TIMEOUT,
                "Command timed out and was terminated.",
                status_code=408,
                details={"args": _redact_args(args), "timeout_seconds": timeout or self.default_timeout, "stdout": result.stdout, "stderr": result.stderr},
            )

        result = _decode_result(proc.returncode or 0, stdout_b, stderr_b, started, max_output_bytes, timed_out=timed_out)
        allowed = set(allowed_exit_codes)
        if check and result.exit_code not in allowed:
            raise ApiError(
                ErrorCode.WORKSPACE_EXEC_FAILED,
                "Command failed.",
                status_code=500,
                details={"args": _redact_args(args), "exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr},
            )
        return result


async def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if os.name != "nt":
        try:
            killpg = getattr(os, "killpg", None)
            getpgid = getattr(os, "getpgid", None)
            if callable(killpg) and callable(getpgid):
                killpg(getpgid(proc.pid), getattr(signal, "SIGKILL", 9))
        except ProcessLookupError:
            return
    else:
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
            _, killer_pending = await asyncio.wait({killer_task}, timeout=5)
            if killer_pending:
                killer.kill()
                killer_task.cancel()
        except FileNotFoundError:
            pass
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


async def _drain_bounded(stream: asyncio.StreamReader | None, max_bytes: int) -> bytes:
    if stream is None:
        return b""
    collected = bytearray()
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return bytes(collected)
        remaining = max(0, max_bytes - len(collected))
        if remaining:
            collected.extend(chunk[:remaining])


def _decode_result(exit_code: int, stdout_b: bytes, stderr_b: bytes, started: float, max_output_bytes: int, *, timed_out: bool) -> CommandResult:
    combined_len = len(stdout_b) + len(stderr_b)
    truncated = combined_len > max_output_bytes
    if truncated:
        stdout_limit = max_output_bytes // 2
        stderr_limit = max_output_bytes - stdout_limit
        stdout_b = stdout_b[:stdout_limit]
        stderr_b = stderr_b[:stderr_limit]
    suffix = "\n...[truncated]" if truncated else ""
    return CommandResult(
        exit_code=exit_code,
        stdout=stdout_b.decode("utf-8", errors="replace") + (suffix if truncated and stdout_b else ""),
        stderr=stderr_b.decode("utf-8", errors="replace") + (suffix if truncated and stderr_b else ""),
        duration_ms=round((time.perf_counter() - started) * 1000),
        truncated=truncated,
        timed_out=timed_out,
    )


def _redact_args(args: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        if arg == "-c" and redacted and redacted[-1] == "git":
            redacted.append(arg)
            skip_next = True
            continue
        if arg.startswith("http.extraHeader="):
            redacted.append("http.extraHeader=<redacted>")
        elif "Authorization:" in arg:
            redacted.append("<redacted>")
        else:
            redacted.append(arg)
    return redacted


def parse_porcelain_z(raw: str) -> tuple[list[ChangedFile], list[str], list[str]]:
    entries = [part for part in raw.split("\0") if part]
    changed: list[ChangedFile] = []
    untracked: list[str] = []
    conflicts: list[str] = []
    idx = 0
    while idx < len(entries):
        item = entries[idx]
        if len(item) < 4:
            idx += 1
            continue
        status = item[:2]
        path = item[3:]
        previous_path = None
        if status[0] == "R" or status[1] == "R":
            idx += 1
            if idx < len(entries):
                previous_path = entries[idx]
        operation = porcelain_operation(status)
        changed_file = ChangedFile(path=path, operation=operation, status=status.strip(), previous_path=previous_path)
        changed.append(changed_file)
        if status == "??":
            untracked.append(path)
        if "U" in status or status in {"AA", "DD"}:
            conflicts.append(path)
        idx += 1
    return changed, untracked, conflicts


def porcelain_operation(status: str) -> str:
    if status == "??":
        return "untracked"
    if "U" in status or status in {"AA", "DD"}:
        return "conflicted"
    if status[0] == "R" or status[1] == "R":
        return "renamed"
    if status[0] == "A" or status[1] == "A":
        return "added"
    if status[0] == "D" or status[1] == "D":
        return "deleted"
    return "modified"


def parse_numstat(raw: str) -> dict[str, tuple[int, int]]:
    stats: dict[str, tuple[int, int]] = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_s, del_s, path = parts[0], parts[1], parts[-1]
        additions = 0 if add_s == "-" else int(add_s)
        deletions = 0 if del_s == "-" else int(del_s)
        stats[path] = (additions, deletions)
    return stats


def attach_numstat(files: list[ChangedFile], stats: dict[str, tuple[int, int]]) -> list[ChangedFile]:
    enriched: list[ChangedFile] = []
    for item in files:
        additions, deletions = stats.get(item.path, (item.additions, item.deletions))
        enriched.append(item.model_copy(update={"additions": additions, "deletions": deletions}))
    return enriched


def normalize_git_paths(paths: list[str]) -> list[str]:
    return [normalize_path(path) for path in paths]
