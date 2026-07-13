from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.models.workspaces import WorkspaceCommandStartRequest
from app.workspace import operations as operations_module
from app.workspace.exec import build_pwsh_script, strip_ansi_escape_sequences
from app.workspace.operations import OperationRuntime, WorkspaceOperationManager


def run(coro):
    return asyncio.run(coro)


def make_settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        workspace_root=str(tmp_path / "workspaces"),
        workspace_operation_root=str(tmp_path / "operations"),
        audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}",
        workspace_python_venv_enabled=False,
        **overrides,
    )


async def wait_terminal(manager: WorkspaceOperationManager, workspace_id: str, operation_id: str, *, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = await manager.get(workspace_id, operation_id)
        if record["state"] != "running":
            return record
        await asyncio.sleep(0.05)
    raise AssertionError(f"operation {operation_id} did not finish")


def test_workspace_command_start_request_defaults_to_utf8_only() -> None:
    request = WorkspaceCommandStartRequest(
        action="start",
        idempotency_key="command_0001",
        script="Write-Output ok",
    )

    assert request.plain_output is False
    assert request.utf8_output is True


def test_build_pwsh_script_injects_only_requested_preludes() -> None:
    script = build_pwsh_script("Write-Output 'ok'", plain_output=True, utf8_output=True)

    assert "$PSStyle.OutputRendering = 'PlainText'" in script
    assert "[Console]::OutputEncoding" in script
    assert "$env:PYTHONIOENCODING = 'utf-8'" in script
    assert script.endswith("Write-Output 'ok'")


def test_build_pwsh_script_utf8_prelude_configures_python_child_output() -> None:
    script = build_pwsh_script("python -c \"print('中文')\"", plain_output=False, utf8_output=True)

    assert "[Console]::OutputEncoding" in script
    assert "$OutputEncoding" in script
    assert "$env:PYTHONIOENCODING = 'utf-8'" in script
    assert "$env:PYTHONUTF8 = '1'" in script


def test_build_pwsh_script_can_auto_activate_workspace_python_venv() -> None:
    script = build_pwsh_script(
        "python --version",
        plain_output=False,
        utf8_output=True,
        activate_python_venv=True,
        python_venv_dir=".venv",
    )

    assert "$env:VIRTUAL_ENV = $__resolvedPythonVenv" in script
    assert "pyvenv.cfg" in script
    assert "& $__venvPython --version" in script


def test_build_pwsh_script_does_not_change_default_script() -> None:
    assert build_pwsh_script("Write-Output 'ok'", plain_output=False, utf8_output=False) == "Write-Output 'ok'"


def test_strip_ansi_escape_sequences_removes_display_noise() -> None:
    assert strip_ansi_escape_sequences("\x1b[32;1mMode \x1b[0mName") == "Mode Name"


def test_job_object_creation_failure_never_starts_pwsh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        process_started = False

        class BrokenJob:
            def __init__(self) -> None:
                raise OSError("job creation failed")

        async def fake_create_subprocess_exec(*args, **kwargs):
            nonlocal process_started
            process_started = True
            raise AssertionError("PowerShell must not start when Job Object creation fails")

        monkeypatch.setattr(operations_module, "WindowsJob", BrokenJob)
        monkeypatch.setattr(
            operations_module.asyncio,
            "create_subprocess_exec",
            fake_create_subprocess_exec,
        )
        manager = WorkspaceOperationManager(make_settings(tmp_path))
        operation = await manager.start(
            workspace_id="ws_job_failure",
            repo_dir=repo_dir,
            idempotency_key="command_job_failure",
            script="Write-Output unreachable",
            timeout_seconds=10,
            max_output_bytes=20_000,
            allow_network=False,
            plain_output=True,
            utf8_output=True,
            activate_python_venv=False,
            python_venv_dir=".venv",
        )
        terminal = await wait_terminal(
            manager,
            "ws_job_failure",
            operation["operation_id"],
        )
        assert terminal["state"] == "failed"
        assert terminal["error_code"] == "command_start_failed"
        assert process_started is False
        await manager.shutdown()

    run(scenario())


def test_finally_terminates_started_process_after_startup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        terminated: list[bool] = []

        class FakeJob:
            assigned = False

            def close(self) -> None:
                pass

        class FakeProcess:
            pid = 1234
            returncode: int | None = None

        async def create_job(runtime):
            return FakeJob()

        async def create_process(runtime, *args, **kwargs):
            return FakeProcess()

        async def fail_assignment(runtime, job, process):
            raise RuntimeError("assignment setup failed")

        async def capture_termination(process, job, grace_seconds):
            process.returncode = -9
            terminated.append(True)

        manager = WorkspaceOperationManager(make_settings(tmp_path))
        monkeypatch.setattr(manager, "_create_job_before_deadline", create_job)
        monkeypatch.setattr(manager, "_create_process_before_deadline", create_process)
        monkeypatch.setattr(manager, "_assign_job_before_deadline", fail_assignment)
        monkeypatch.setattr(
            operations_module,
            "_terminate_process_tree",
            capture_termination,
        )
        operation = await manager.start(
            workspace_id="ws_finally_cleanup",
            repo_dir=repo_dir,
            idempotency_key="command_finally_cleanup",
            script="Write-Output unreachable",
            timeout_seconds=10,
            max_output_bytes=20_000,
            allow_network=False,
            plain_output=True,
            utf8_output=True,
            activate_python_venv=False,
            python_venv_dir=".venv",
        )
        terminal = await wait_terminal(
            manager,
            "ws_finally_cleanup",
            operation["operation_id"],
        )
        assert terminal["state"] == "failed"
        assert terminated == [True]
        await manager.shutdown()

    run(scenario())


def test_startup_uses_remaining_end_to_end_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        process_started = False
        closed_jobs: list[bool] = []

        class SlowJob:
            def __init__(self) -> None:
                time.sleep(0.25)
                self.assigned = False

            def close(self) -> None:
                closed_jobs.append(True)

        async def fake_create_subprocess_exec(*args, **kwargs):
            nonlocal process_started
            process_started = True
            raise AssertionError("Process creation must not start after the deadline")

        monkeypatch.setattr(operations_module, "WindowsJob", SlowJob)
        monkeypatch.setattr(
            operations_module.asyncio,
            "create_subprocess_exec",
            fake_create_subprocess_exec,
        )
        manager = WorkspaceOperationManager(make_settings(tmp_path))
        started = time.monotonic()
        operation = await manager.start(
            workspace_id="ws_startup_timeout",
            repo_dir=repo_dir,
            idempotency_key="command_startup_timeout",
            script="Write-Output unreachable",
            timeout_seconds=0.05,
            max_output_bytes=20_000,
            allow_network=False,
            plain_output=True,
            utf8_output=True,
            activate_python_venv=False,
            python_venv_dir=".venv",
        )
        terminal = await wait_terminal(
            manager,
            "ws_startup_timeout",
            operation["operation_id"],
            timeout=1,
        )
        elapsed = time.monotonic() - started
        assert terminal["state"] == "timed_out"
        assert elapsed < 0.2
        assert process_started is False
        await asyncio.sleep(0.25)
        assert closed_jobs == [True]
        await manager.shutdown()

    run(scenario())


def test_progress_metadata_is_throttled_but_terminal_state_is_immediate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        manager = WorkspaceOperationManager(
            make_settings(
                tmp_path,
                workspace_operation_progress_flush_seconds=60,
            )
        )
        now = time.monotonic()
        runtime = OperationRuntime(
            record={
                "operation_id": "op_aaaaaaaaaaaaaaaa",
                "workspace_id": "ws_progress",
                "state": "running",
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "stdout_truncated": False,
                "stderr_truncated": False,
            },
            started_monotonic=now,
            deadline_monotonic=now + 60,
            last_progress_persist_monotonic=now,
        )
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * (64 * 1024 * 8))
        reader.feed_eof()
        persisted: list[dict] = []

        def capture_write(record: dict) -> None:
            persisted.append(dict(record))

        monkeypatch.setattr(manager, "_write_record", capture_write)
        await manager._drain_stream(
            runtime,
            "stdout",
            reader,
            tmp_path / "stdout.log",
            1024,
        )
        assert runtime.record["stdout_bytes"] == 64 * 1024 * 8
        assert runtime.record["stdout_truncated"] is True
        assert persisted == []

        await manager._finish(runtime, state="succeeded", exit_code=0)
        assert len(persisted) == 1
        assert persisted[0]["state"] == "succeeded"

    run(scenario())


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not available")
def test_operation_start_is_idempotent_and_logs_are_queryable(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        manager = WorkspaceOperationManager(make_settings(tmp_path))
        first = await manager.start(
            workspace_id="ws_test",
            repo_dir=repo_dir,
            idempotency_key="command_0001",
            script="Write-Output 'hello'",
            timeout_seconds=10,
            max_output_bytes=20_000,
            allow_network=False,
            plain_output=True,
            utf8_output=True,
            activate_python_venv=False,
            python_venv_dir=".venv",
        )
        second = await manager.start(
            workspace_id="ws_test",
            repo_dir=repo_dir,
            idempotency_key="command_0001",
            script="Write-Output 'hello'",
            timeout_seconds=10,
            max_output_bytes=20_000,
            allow_network=False,
            plain_output=True,
            utf8_output=True,
            activate_python_venv=False,
            python_venv_dir=".venv",
        )
        assert second["operation_id"] == first["operation_id"]
        terminal = await wait_terminal(manager, "ws_test", first["operation_id"])
        assert terminal["state"] == "succeeded"
        logs = await manager.logs(
            "ws_test",
            first["operation_id"],
            stdout_offset=0,
            stderr_offset=0,
            max_bytes=20_000,
        )
        assert "hello" in logs["stdout"]
        assert logs["stdout_eof"] is True
        await manager.shutdown()

    run(scenario())


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not available")
def test_same_workspace_commands_run_concurrently(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        manager = WorkspaceOperationManager(make_settings(tmp_path))
        operations = []
        for index in range(3):
            operations.append(
                await manager.start(
                    workspace_id="ws_shared",
                    repo_dir=repo_dir,
                    idempotency_key=f"command_{index:04d}",
                    script="Start-Sleep -Milliseconds 500; Write-Output done",
                    timeout_seconds=10,
                    max_output_bytes=20_000,
                    allow_network=False,
                    plain_output=True,
                    utf8_output=True,
                    activate_python_venv=False,
                    python_venv_dir=".venv",
                )
            )
        assert len(manager.active_for_workspace("ws_shared")) == 3
        results = await asyncio.gather(
            *(wait_terminal(manager, "ws_shared", item["operation_id"]) for item in operations)
        )
        assert {item["state"] for item in results} == {"succeeded"}
        await manager.shutdown()

    run(scenario())


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not available")
def test_cancel_terminates_operation_without_affecting_other_commands(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        manager = WorkspaceOperationManager(make_settings(tmp_path))
        long_running = await manager.start(
            workspace_id="ws_cancel",
            repo_dir=repo_dir,
            idempotency_key="command_cancel_long",
            script="Start-Sleep -Seconds 30",
            timeout_seconds=60,
            max_output_bytes=20_000,
            allow_network=False,
            plain_output=True,
            utf8_output=True,
            activate_python_venv=False,
            python_venv_dir=".venv",
        )
        short = await manager.start(
            workspace_id="ws_cancel",
            repo_dir=repo_dir,
            idempotency_key="command_cancel_short",
            script="Write-Output short-ok",
            timeout_seconds=10,
            max_output_bytes=20_000,
            allow_network=False,
            plain_output=True,
            utf8_output=True,
            activate_python_venv=False,
            python_venv_dir=".venv",
        )
        await manager.cancel("ws_cancel", long_running["operation_id"])
        canceled, succeeded = await asyncio.gather(
            wait_terminal(manager, "ws_cancel", long_running["operation_id"], timeout=8),
            wait_terminal(manager, "ws_cancel", short["operation_id"], timeout=8),
        )
        assert canceled["state"] == "canceled"
        assert succeeded["state"] == "succeeded"
        await manager.shutdown()

    run(scenario())


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not available")
def test_log_limit_truncates_storage_but_reader_keeps_draining(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        manager = WorkspaceOperationManager(make_settings(tmp_path))
        operation = await manager.start(
            workspace_id="ws_logs",
            repo_dir=repo_dir,
            idempotency_key="command_logs",
            script="1..5000 | ForEach-Object { Write-Output ('x' * 100) }",
            timeout_seconds=20,
            max_output_bytes=1024,
            allow_network=False,
            plain_output=True,
            utf8_output=True,
            activate_python_venv=False,
            python_venv_dir=".venv",
        )
        terminal = await wait_terminal(manager, "ws_logs", operation["operation_id"], timeout=15)
        assert terminal["state"] == "succeeded"
        assert terminal["stdout_truncated"] is True
        assert terminal["stdout_bytes"] > 1024
        stdout_path = Path(manager.settings.workspace_operation_root) / operation["operation_id"] / "stdout.log"
        assert stdout_path.stat().st_size <= 1024
        await manager.shutdown()

    run(scenario())


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not available")
def test_timeout_is_a_hard_deadline_and_process_tree_is_terminated(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        settings = make_settings(
            tmp_path,
            workspace_command_kill_grace_seconds=2,
            workspace_command_reader_grace_seconds=1,
        )
        manager = WorkspaceOperationManager(settings)
        started = time.monotonic()
        operation = await manager.start(
            workspace_id="ws_timeout",
            repo_dir=repo_dir,
            idempotency_key="command_timeout",
            script=(
                "python -c \"import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "time.sleep(30)\""
            ),
            timeout_seconds=1,
            max_output_bytes=20_000,
            allow_network=False,
            plain_output=True,
            utf8_output=True,
            activate_python_venv=False,
            python_venv_dir=".venv",
        )
        terminal = await wait_terminal(manager, "ws_timeout", operation["operation_id"], timeout=8)
        elapsed = time.monotonic() - started
        assert terminal["state"] == "timed_out"
        assert elapsed < 8
        await manager.shutdown()

    run(scenario())


def test_start_rejects_reused_idempotency_key_with_different_payload(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        manager = WorkspaceOperationManager(make_settings(tmp_path, workspace_shell="missing-pwsh"))
        first = await manager.start(
            workspace_id="ws_idem",
            repo_dir=repo_dir,
            idempotency_key="command_same",
            script="Write-Output one",
            timeout_seconds=10,
            max_output_bytes=20_000,
            allow_network=False,
            plain_output=False,
            utf8_output=True,
            activate_python_venv=False,
            python_venv_dir=".venv",
        )
        with pytest.raises(ApiError) as exc:
            await manager.start(
                workspace_id="ws_idem",
                repo_dir=repo_dir,
                idempotency_key="command_same",
                script="Write-Output two",
                timeout_seconds=10,
                max_output_bytes=20_000,
                allow_network=False,
                plain_output=False,
                utf8_output=True,
                activate_python_venv=False,
                python_venv_dir=".venv",
            )
        assert exc.value.error_code == ErrorCode.IDEMPOTENCY_KEY_REUSED
        await wait_terminal(manager, "ws_idem", first["operation_id"])
        await manager.shutdown()

    run(scenario())


def test_startup_marks_running_operation_interrupted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    operation_dir = Path(settings.workspace_operation_root) / "op_0123456789abcdef"
    operation_dir.mkdir(parents=True)
    (operation_dir / "state.json").write_text(
        json.dumps(
            {
                "operation_id": "op_0123456789abcdef",
                "workspace_id": "ws_recovered",
                "idempotency_key": "command_recovered",
                "state": "running",
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    manager = WorkspaceOperationManager(settings)
    recovered = run(manager.get("ws_recovered", "op_0123456789abcdef"))

    assert recovered["state"] == "interrupted"
    assert recovered["error_code"] == "gateway_restarted"


def test_running_operation_recovery_can_be_delayed_until_single_instance_acquired(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    operation_dir = Path(settings.workspace_operation_root) / "op_1111111111111111"
    operation_dir.mkdir(parents=True)
    (operation_dir / "state.json").write_text(
        json.dumps(
            {
                "operation_id": "op_1111111111111111",
                "workspace_id": "ws_delayed",
                "idempotency_key": "command_delayed",
                "state": "running",
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    manager = WorkspaceOperationManager(settings, recover_running=False)
    before = run(manager.get("ws_delayed", "op_1111111111111111"))
    assert before["state"] == "running"

    assert manager.recover_running_operations() == 1
    after = run(manager.get("ws_delayed", "op_1111111111111111"))
    assert after["state"] == "interrupted"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not available")
def test_shutdown_marks_active_operation_interrupted(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        manager = WorkspaceOperationManager(make_settings(tmp_path))
        operation = await manager.start(
            workspace_id="ws_shutdown",
            repo_dir=repo_dir,
            idempotency_key="command_shutdown",
            script="Start-Sleep -Seconds 30",
            timeout_seconds=60,
            max_output_bytes=20_000,
            allow_network=False,
            plain_output=True,
            utf8_output=True,
            activate_python_venv=False,
            python_venv_dir=".venv",
        )
        await manager.shutdown()
        terminal = await manager.get("ws_shutdown", operation["operation_id"])
        assert terminal["state"] == "interrupted"
        assert terminal["error_code"] == "gateway_shutdown"

    run(scenario())
