import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.workspace.security import sanitized_environment, validate_script


@pytest.fixture
def settings(tmp_path):
    return Settings(
        allow_all_repos=True,
        workspace_root=str(tmp_path / "workspaces"),
        workspace_mirror_root=str(tmp_path / "mirrors"),
        audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}",
    )


@pytest.mark.parametrize(
    "script",
    [
        "git push origin HEAD:gpt/x",
        "git remote set-url origin https://example.com/x.git",
        "gh auth login",
        "gh secret set TOKEN",
        "Get-ChildItem Env:",
        "Get-Content $env:GITHUB_TOKEN",
        "ssh git@github.com",
        "scp a b",
    ],
)
def test_workspace_exec_rejects_high_risk_commands(settings, script):
    with pytest.raises(ApiError) as exc:
        validate_script(script, allow_network=False, settings=settings)
    assert exc.value.error_code == ErrorCode.WORKSPACE_SCRIPT_REJECTED


@pytest.mark.parametrize("script", ["Invoke-WebRequest https://example.com", "Invoke-RestMethod https://example.com", "curl https://example.com", "wget https://example.com"])
def test_workspace_exec_rejects_network_when_disabled(settings, script):
    with pytest.raises(ApiError) as exc:
        validate_script(script, allow_network=False, settings=settings)
    assert exc.value.error_code == ErrorCode.WORKSPACE_SCRIPT_REJECTED


def test_server_must_allow_requested_network(settings):
    with pytest.raises(ApiError) as exc:
        validate_script("python -c 'print(1)'", allow_network=True, settings=settings)
    assert exc.value.error_code == ErrorCode.WORKSPACE_SCRIPT_REJECTED


def test_sanitized_environment_removes_sensitive_values():
    env = {
        "PATH": "/usr/bin",
        "GITHUB_TOKEN": "secret",
        "GH_TOKEN": "secret",
        "GPT_ACTION_SECRET": "secret",
        "CUSTOM_PASSWORD": "secret",
        "HOME": "/tmp/home",
    }
    clean = sanitized_environment(env)
    assert clean["PATH"] == "/usr/bin"
    assert clean["HOME"] == "/tmp/home"
    assert clean["GITHUB_TOKEN"] == ""
    assert clean["GH_TOKEN"] == ""
    assert clean["GPT_ACTION_SECRET"] == ""
    assert "CUSTOM_PASSWORD" not in clean
