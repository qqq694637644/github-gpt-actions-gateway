import pytest
from pydantic import ValidationError

from app.config.settings import Settings
from app.models.branches import CreateWorkBranchRequest
from app.models.workspaces import PrepareWorkspaceRequest
from app.workspace.ids import WORKSPACE_ID_PATTERN


def make_settings(tmp_path, **kwargs) -> Settings:
    return Settings(workspace_root=str(tmp_path / "w"), workspace_mirror_root=str(tmp_path / "m"), audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}", **kwargs)


def test_workspace_python_settings_describe_current_bootstrap_surface(tmp_path):
    settings = make_settings(tmp_path)

    assert settings.workspace_python_venv_enabled is True
    assert settings.workspace_python_venv_dir == ".venv"
    assert settings.workspace_python_venv_python == "py -3.13"
    assert settings.workspace_python_auto_gitignore is True
    assert settings.workspace_python_auto_activate is True
    assert {name for name in Settings.model_fields if name.startswith("workspace_python_")} == {
        "workspace_python_venv_enabled",
        "workspace_python_venv_dir",
        "workspace_python_venv_python",
        "workspace_python_auto_gitignore",
        "workspace_python_auto_activate",
    }


def test_default_read_branch_allowlist_allows_all_refs(tmp_path):
    settings = make_settings(tmp_path)

    assert settings.read_branch_allowlist == "*"
    assert settings.read_branch_patterns == ["*"]


def test_create_work_branch_request_has_current_base_ref_shape():
    schema = CreateWorkBranchRequest.model_json_schema()
    properties = schema["properties"]

    assert "base_ref" in properties
    assert "base_sha" in properties
    assert "purpose_slug" in properties


def test_prepare_workspace_request_workspace_id_schema_requires_ws_prefix():
    schema = PrepareWorkspaceRequest.model_json_schema()
    workspace_id = schema["properties"]["workspace_id"]
    string_branch = next(item for item in workspace_id["anyOf"] if item.get("type") == "string")
    assert string_branch["pattern"] == WORKSPACE_ID_PATTERN


@pytest.mark.parametrize(
    "value",
    [
        r"C:\temp\venv",
        "C:/temp/venv",
        "C:temp/venv",
        "/tmp/venv",
        r"\\server\share\venv",
        "tools//venv",
        "tools:venv",
        "../venv",
    ],
)
def test_workspace_python_venv_dir_rejects_absolute_drive_and_invalid_paths(tmp_path, value: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, workspace_python_venv_dir=value)


def test_workspace_python_venv_dir_normalizes_relative_trailing_slash(tmp_path) -> None:
    settings = make_settings(tmp_path, workspace_python_venv_dir="tools/.venv/")

    assert settings.workspace_python_venv_dir == "tools/.venv"
