from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.workspace.git import kill_process_tree
from app.workspace.models import CommandResult
from app.workspace.security import sanitized_environment, validate_script

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class PwshExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def execute(
        self,
        repo_dir: Path,
        *,
        script: str,
        timeout_seconds: int,
        max_output_bytes: int,
        allow_network: bool,
        plain_output: bool = False,
        utf8_output: bool = False,
        activate_python_venv: bool = False,
        python_venv_dir: str = ".venv",
    ) -> CommandResult:
        validate_script(script, allow_network=allow_network, settings=self.settings)
        started = time.perf_counter()
        preexec_fn = os.setsid if os.name != "nt" else None
        effective_script = build_pwsh_script(
            script,
            plain_output=plain_output,
            utf8_output=utf8_output,
            activate_python_venv=activate_python_venv,
            python_venv_dir=python_venv_dir,
        )
        args = [self.settings.workspace_shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", effective_script]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(repo_dir),
                env=sanitized_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=preexec_fn,
            )
        except FileNotFoundError as exc:
            raise ApiError(
                ErrorCode.WORKSPACE_EXEC_FAILED,
                f"Workspace shell was not found: {self.settings.workspace_shell}",
                status_code=500,
                suggestion="Install PowerShell 7+ or set WORKSPACE_SHELL to an approved pwsh executable.",
            ) from exc
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except TimeoutError as exc:
            await kill_process_tree(proc)
            stdout_b, stderr_b = await proc.communicate()
            result = decode_command_result(proc.returncode or -9, stdout_b, stderr_b, started, max_output_bytes, timed_out=True, strip_ansi=plain_output)
            raise ApiError(
                ErrorCode.WORKSPACE_TIMEOUT,
                "PowerShell command timed out and was terminated.",
                status_code=408,
                details={"timeout_seconds": timeout_seconds, "stdout": result.stdout, "stderr": result.stderr},
            ) from exc
        return decode_command_result(proc.returncode or 0, stdout_b, stderr_b, started, max_output_bytes, timed_out=False, strip_ansi=plain_output)


def build_pwsh_script(
    script: str,
    *,
    plain_output: bool,
    utf8_output: bool,
    activate_python_venv: bool = False,
    python_venv_dir: str = ".venv",
) -> str:
    prelude: list[str] = []
    if plain_output:
        prelude.extend(
            [
                "$ProgressPreference = 'SilentlyContinue'",
                "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }",
            ]
        )
    if utf8_output:
        prelude.extend(
            [
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)",
                "$OutputEncoding = [System.Text.UTF8Encoding]::new($false)",
                "$env:PYTHONIOENCODING = 'utf-8'",
                "$env:PYTHONUTF8 = '1'",
                "$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'",
                "$PSDefaultParameterValues['Set-Content:Encoding'] = 'utf8'",
                "$PSDefaultParameterValues['Add-Content:Encoding'] = 'utf8'",
            ]
        )
    if activate_python_venv:
        quoted_venv_dir = quote_pwsh_string(python_venv_dir)
        prelude.extend(
            [
                f"$__workspacePythonVenv = Join-Path (Get-Location) {quoted_venv_dir}",
                "if (-not (Test-Path -LiteralPath $__workspacePythonVenv -PathType Container)) {",
                "  throw \"Workspace Python virtual environment is missing: $__workspacePythonVenv\"",
                "}",
                "$__resolvedPythonVenv = (Resolve-Path -LiteralPath $__workspacePythonVenv).Path",
                "$__pyvenvCfg = Join-Path $__resolvedPythonVenv 'pyvenv.cfg'",
                "if (-not (Test-Path -LiteralPath $__pyvenvCfg -PathType Leaf)) {",
                "  throw \"Workspace Python virtual environment is incomplete: pyvenv.cfg is missing at $__pyvenvCfg\"",
                "}",
                "$env:VIRTUAL_ENV = $__resolvedPythonVenv",
                "$__venvScripts = Join-Path $__resolvedPythonVenv 'Scripts'",
                "$__venvBin = Join-Path $__resolvedPythonVenv 'bin'",
                "$__venvPython = $null",
                "if (Test-Path -LiteralPath $__venvScripts -PathType Container) {",
                "  $__venvPythonExe = Join-Path $__venvScripts 'python.exe'",
                "  $__venvPythonPlain = Join-Path $__venvScripts 'python'",
                "  if (Test-Path -LiteralPath $__venvPythonExe -PathType Leaf) { $__venvPython = $__venvPythonExe }",
                "  elseif (Test-Path -LiteralPath $__venvPythonPlain -PathType Leaf) { $__venvPython = $__venvPythonPlain }",
                "} elseif (Test-Path -LiteralPath $__venvBin -PathType Container) {",
                "  $__venvPythonPlain = Join-Path $__venvBin 'python'",
                "  if (Test-Path -LiteralPath $__venvPythonPlain -PathType Leaf) { $__venvPython = $__venvPythonPlain }",
                "}",
                "if (-not $__venvPython) {",
                "  throw \"Workspace Python virtual environment is incomplete: no python executable under Scripts or bin in $__resolvedPythonVenv\"",
                "}",
                "try { & $__venvPython --version *> $null }",
                "catch { throw \"Workspace Python virtual environment Python is not executable: $__venvPython ($($_.Exception.Message))\" }",
                "if ($LASTEXITCODE -ne 0) {",
                "  throw \"Workspace Python virtual environment Python failed validation with exit code ${LASTEXITCODE}: $__venvPython\"",
                "}",
                "if (Test-Path -LiteralPath $__venvScripts -PathType Container) {",
                "  $env:PATH = $__venvScripts + [System.IO.Path]::PathSeparator + $env:PATH",
                "} else {",
                "  $env:PATH = $__venvBin + [System.IO.Path]::PathSeparator + $env:PATH",
                "}",
            ]
        )
    if not prelude:
        return script
    return "\n".join([*prelude, script])


def decode_command_result(
    exit_code: int,
    stdout_b: bytes,
    stderr_b: bytes,
    started: float,
    max_output_bytes: int,
    *,
    timed_out: bool,
    strip_ansi: bool = False,
) -> CommandResult:
    total = len(stdout_b) + len(stderr_b)
    truncated = total > max_output_bytes
    if truncated:
        stdout_limit = max_output_bytes // 2
        stderr_limit = max_output_bytes - stdout_limit
        stdout_b = stdout_b[:stdout_limit]
        stderr_b = stderr_b[:stderr_limit]
    suffix = "\n...[truncated]" if truncated else ""
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if strip_ansi:
        stdout = strip_ansi_escape_sequences(stdout)
        stderr = strip_ansi_escape_sequences(stderr)
    return CommandResult(
        exit_code=exit_code,
        stdout=stdout + (suffix if truncated and stdout_b else ""),
        stderr=stderr + (suffix if truncated and stderr_b else ""),
        duration_ms=round((time.perf_counter() - started) * 1000),
        truncated=truncated,
        timed_out=timed_out,
    )


def strip_ansi_escape_sequences(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def quote_pwsh_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
