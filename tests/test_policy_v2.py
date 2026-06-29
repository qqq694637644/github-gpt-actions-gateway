import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.policy.rules import Policy


@pytest.fixture
def policy():
    return Policy(Settings(allow_all_repos=True))


@pytest.mark.parametrize("branch", ["main", "master", "develop", "release/1.0", "production/x", "hotfix/x", "feature/x", "gpt/fix-ci"])
def test_write_branch_allows_arbitrary_non_empty_branch(policy, branch):
    policy.assert_write_branch_allowed(branch)


def test_write_branch_rejects_empty_branch(policy):
    with pytest.raises(ApiError) as exc:
        policy.assert_write_branch_allowed("")
    assert exc.value.error_code == ErrorCode.BRANCH_NOT_ALLOWED


def test_read_branch_default_allows_arbitrary_refs(policy):
    policy.assert_read_ref_allowed("feature/task")
    policy.assert_read_ref_allowed("release/1.0")


@pytest.mark.parametrize("path", [".env", ".env.local", "secrets/prod.json", "credentials/github.json", "node_modules/a.js", "dist/app.js", "build/app.js", "coverage/report.xml", ".git/config", "key.pem", "cert.crt"])
def test_sensitive_paths_are_blocked(policy, path):
    with pytest.raises(ApiError):
        policy.assert_write_path_allowed(path)


def test_workflow_edit_default_blocked(policy):
    with pytest.raises(ApiError) as exc:
        policy.assert_write_path_allowed(".github/workflows/ci.yml")
    assert exc.value.error_code == ErrorCode.WORKFLOW_EDIT_NOT_ALLOWED
