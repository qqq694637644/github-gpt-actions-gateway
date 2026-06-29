from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.models.workspaces import WorkspaceExecPwshRequest
from app.workspace.exec import PwshExecutor, build_pwsh_script, decode_command_result, strip_ansi_escape_sequences


def run(coro):
    return asyncio.run(coro)


def test_workspace_exec_request_output_options_default_to_utf8_only() -> None:
    request = WorkspaceExecPwshRequest(script="Write-Output ok")

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
    assert script.endswith("python -c \"print('中文')\"")


def test_build_pwsh_script_can_auto_activate_workspace_python_venv() -> None:
    script = build_pwsh_script("python --version", plain_output=False, utf8_output=True, activate_python_venv=True, python_venv_dir=".venv")

    assert "$env:VIRTUAL_ENV = $__resolvedPythonVenv" in script
    assert "pyvenv.cfg" in script
    assert "throw \"Workspace Python virtual environment is missing" in script
    assert "Join-Path $__resolvedPythonVenv 'Scripts'" in script
    assert "Join-Path $__resolvedPythonVenv 'bin'" in script
    assert "& $__venvPython --version" in script
    assert script.endswith("python --version")


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not available")
def test_workspace_exec_fails_when_auto_activate_venv_is_missing(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    executor = PwshExecutor(Settings(workspace_shell="pwsh"))

    result = run(
        executor.execute(
            repo_dir,
            script="Write-Output 'ran-after-missing-venv'",
            timeout_seconds=30,
            max_output_bytes=20_000,
            allow_network=False,
            utf8_output=True,
            activate_python_venv=True,
            python_venv_dir=".venv",
        )
    )

    assert result.exit_code != 0
    assert "ran-after-missing-venv" not in result.stdout
    assert "Workspace Python virtual environment is missing" in result.stderr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not available")
def test_workspace_exec_fails_when_auto_activate_venv_has_no_interpreter_dir(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    venv_dir = repo_dir / ".venv"
    venv_dir.mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    executor = PwshExecutor(Settings(workspace_shell="pwsh"))

    result = run(
        executor.execute(
            repo_dir,
            script="Write-Output 'ran-after-partial-venv'",
            timeout_seconds=30,
            max_output_bytes=20_000,
            allow_network=False,
            utf8_output=True,
            activate_python_venv=True,
            python_venv_dir=".venv",
        )
    )

    assert result.exit_code != 0
    assert "ran-after-partial-venv" not in result.stdout
    assert "no python executable under Scripts or bin" in result.stderr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not available")
def test_workspace_exec_fails_when_auto_activate_venv_has_no_pyvenv_cfg(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    (repo_dir / ".venv").mkdir(parents=True)
    executor = PwshExecutor(Settings(workspace_shell="pwsh"))

    result = run(
        executor.execute(
            repo_dir,
            script="Write-Output 'ran-after-missing-pyvenv-cfg'",
            timeout_seconds=30,
            max_output_bytes=20_000,
            allow_network=False,
            utf8_output=True,
            activate_python_venv=True,
            python_venv_dir=".venv",
        )
    )

    assert result.exit_code != 0
    assert "ran-after-missing-pyvenv-cfg" not in result.stdout
    assert "pyvenv.cfg is missing" in result.stderr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not available")
@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX-style executable bits for this check")
def test_workspace_exec_fails_when_auto_activate_venv_python_is_not_executable(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    venv_dir = repo_dir / ".venv"
    interpreter_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    interpreter_dir.mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    python_path = interpreter_dir / ("python.exe" if os.name == "nt" else "python")
    python_path.write_text("not a python executable\n", encoding="utf-8")
    python_path.chmod(0o644)
    executor = PwshExecutor(Settings(workspace_shell="pwsh"))

    result = run(
        executor.execute(
            repo_dir,
            script="Write-Output 'ran-after-bad-python'",
            timeout_seconds=30,
            max_output_bytes=20_000,
            allow_network=False,
            utf8_output=True,
            activate_python_venv=True,
            python_venv_dir=".venv",
        )
    )

    assert result.exit_code != 0
    assert "ran-after-bad-python" not in result.stdout
    assert "Workspace Python virtual environment Python" in result.stderr


def test_build_pwsh_script_does_not_change_default_script() -> None:
    assert build_pwsh_script("Write-Output 'ok'", plain_output=False, utf8_output=False) == "Write-Output 'ok'"


def test_strip_ansi_escape_sequences_removes_display_noise() -> None:
    assert strip_ansi_escape_sequences("\x1b[32;1mMode \x1b[0mName") == "Mode Name"


def test_decode_command_result_strips_ansi_only_when_requested() -> None:
    raw = b"\x1b[32;1mMode \x1b[0mName"

    plain = decode_command_result(0, raw, b"", time.perf_counter(), 10_000, timed_out=False, strip_ansi=True)
    raw_result = decode_command_result(0, raw, b"", time.perf_counter(), 10_000, timed_out=False, strip_ansi=False)

    assert plain.stdout == "Mode Name"
    assert raw_result.stdout == "\x1b[32;1mMode \x1b[0mName"
