from __future__ import annotations

import re

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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


def strip_ansi_escape_sequences(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def quote_pwsh_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
