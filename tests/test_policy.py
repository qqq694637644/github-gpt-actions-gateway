from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.policy.rules import Policy, normalize_path, sanitize_purpose_slug


def make_policy(**kwargs) -> Policy:
    values = {"gpt_action_secret": "secret", "allowed_repos": "acme/demo"}
    values.update(kwargs)
    return Policy(Settings(**values))


def test_normalize_path_rejects_traversal() -> None:
    with pytest.raises(ApiError):
        normalize_path("../secret.txt")


def test_repo_allowlist() -> None:
    policy = make_policy()
    policy.assert_repo_allowed("acme", "demo")
    with pytest.raises(ApiError) as exc:
        policy.assert_repo_allowed("acme", "other")
    assert exc.value.error_code == ErrorCode.REPO_NOT_ALLOWED


def test_repo_allowlist_can_be_disabled() -> None:
    policy = make_policy(allow_all_repos=True)
    policy.assert_repo_allowed("acme", "other")


def test_empty_repo_allowlist_rejects_when_allow_all_repos_is_false() -> None:
    policy = make_policy(allowed_repos="", allow_all_repos=False)

    with pytest.raises(ApiError) as exc:
        policy.assert_repo_allowed("acme", "demo")

    assert exc.value.error_code == ErrorCode.REPO_NOT_ALLOWED
    assert exc.value.message == "No repositories are allowed by configuration."


def test_write_branch_policy() -> None:
    policy = make_policy()
    policy.assert_write_branch_allowed("gpt/fix-thing")
    policy.assert_write_branch_allowed("feature/fix-thing")
    policy.assert_write_branch_allowed("main")
    with pytest.raises(ApiError):
        policy.assert_write_branch_allowed("")


def test_workflow_edit_blocked_by_default() -> None:
    policy = make_policy()
    with pytest.raises(ApiError) as exc:
        policy.assert_write_path_allowed(".github/workflows/ci.yml")
    assert exc.value.error_code == ErrorCode.WORKFLOW_EDIT_NOT_ALLOWED


def test_binary_extension_blocked() -> None:
    policy = make_policy()
    with pytest.raises(ApiError) as exc:
        policy.assert_write_path_allowed("assets/logo.png")
    assert exc.value.error_code == ErrorCode.BINARY_FILE_NOT_ALLOWED


def test_local_python_env_writes_are_blocked_but_deletions_are_allowed() -> None:
    policy = make_policy()

    with pytest.raises(ApiError) as exc:
        policy.assert_write_path_allowed(".venv/Lib/site-packages/pkg.py", operation="modified")

    assert exc.value.error_code == ErrorCode.PATH_NOT_ALLOWED
    assert policy.assert_write_path_allowed(".venv/Lib/site-packages/pkg.py", operation="deleted") == ".venv/Lib/site-packages/pkg.py"


@pytest.mark.parametrize("path", [".env.example", ".env.sample", ".env.template"])
def test_safe_env_example_files_are_allowed(path: str) -> None:
    policy = make_policy()

    assert policy.assert_write_path_allowed(path) == path


@pytest.mark.parametrize("path", [".env", ".env.local", ".env.production", ".env.example.local"])
def test_real_env_files_remain_blocked(path: str) -> None:
    policy = make_policy()

    with pytest.raises(ApiError) as exc:
        policy.assert_write_path_allowed(path)

    assert exc.value.error_code == ErrorCode.PATH_NOT_ALLOWED


def test_tree_path_policy_allows_empty_and_normalizes_prefix() -> None:
    policy = make_policy()
    assert policy.assert_tree_path_allowed(None) is None
    assert policy.assert_tree_path_allowed("src/app") == "src/app"
    with pytest.raises(ApiError):
        policy.assert_tree_path_allowed("../secret")


def test_sanitize_purpose_slug() -> None:
    assert sanitize_purpose_slug("Fix Windows CI!!") == "fix-windows-ci"
