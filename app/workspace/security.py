from __future__ import annotations

import os
import re
from collections.abc import Mapping

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode

BLOCKED_ALWAYS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgit\s+push\b", re.IGNORECASE), "git push is only allowed through workspaceCommitAndPush."),
    (re.compile(r"\bgit\s+remote\s+set-url\b", re.IGNORECASE), "Changing git remotes is not allowed."),
    (re.compile(r"\bgh\s+auth\b", re.IGNORECASE), "GitHub CLI authentication is not allowed."),
    (re.compile(r"\bgh\s+secret\b", re.IGNORECASE), "GitHub secret operations are not allowed."),
    (re.compile(r"\bGet-ChildItem\s+Env:", re.IGNORECASE), "Enumerating process environment variables is not allowed."),
    (re.compile(r"\bGet-Content\s+\$env:", re.IGNORECASE), "Reading environment variables as files is not allowed."),
    (re.compile(r"\bssh\b", re.IGNORECASE), "ssh is not allowed from workspaceCommand."),
    (re.compile(r"\bscp\b", re.IGNORECASE), "scp is not allowed from workspaceCommand."),
]

NETWORK_BLOCKED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bInvoke-WebRequest\b", re.IGNORECASE), "Network downloads are disabled."),
    (re.compile(r"\bInvoke-RestMethod\b", re.IGNORECASE), "Network requests are disabled."),
    (re.compile(r"\bcurl\b", re.IGNORECASE), "curl is disabled when network is not allowed."),
    (re.compile(r"\bwget\b", re.IGNORECASE), "wget is disabled when network is not allowed."),
]

SENSITIVE_ENV_EXACT = {
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_APP_PRIVATE_KEY",
    "GPT_ACTION_SECRET",
    "GITHUB_APP_ID",
    "GITHUB_INSTALLATION_ID",
}
SENSITIVE_ENV_FRAGMENTS = ("TOKEN", "SECRET", "PRIVATE_KEY", "PASSWORD", "CREDENTIAL")
ENV_ALLOWLIST = {
    "PATH",
    "Path",
    "HOME",
    "USERPROFILE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PSModulePath",
    "TERM",
}


def validate_script(script: str, *, allow_network: bool, settings: Settings) -> None:
    for pattern, message in BLOCKED_ALWAYS:
        if pattern.search(script):
            raise ApiError(ErrorCode.WORKSPACE_SCRIPT_REJECTED, message, status_code=403, details={"pattern": pattern.pattern})
    if allow_network and not settings.workspace_allow_network:
        raise ApiError(
            ErrorCode.WORKSPACE_SCRIPT_REJECTED,
            "Network access is disabled by server configuration.",
            status_code=403,
            suggestion="Keep allow_network=false or enable WORKSPACE_ALLOW_NETWORK only after risk review.",
        )
    if not allow_network:
        for pattern, message in NETWORK_BLOCKED:
            if pattern.search(script):
                raise ApiError(ErrorCode.WORKSPACE_SCRIPT_REJECTED, message, status_code=403, details={"pattern": pattern.pattern})


def sanitized_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    source = source or os.environ
    clean: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if key not in ENV_ALLOWLIST and upper not in ENV_ALLOWLIST:
            continue
        if upper in SENSITIVE_ENV_EXACT or any(fragment in upper for fragment in SENSITIVE_ENV_FRAGMENTS):
            continue
        clean[key] = value
    clean.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GITHUB_TOKEN": "",
            "GH_TOKEN": "",
            "GITHUB_APP_PRIVATE_KEY": "",
            "GPT_ACTION_SECRET": "",
        }
    )
    return clean
