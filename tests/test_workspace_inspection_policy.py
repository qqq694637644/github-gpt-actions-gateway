from app.services.workspaces import _path_is_excluded_from_inspection


def test_dist_paths_are_visible_to_workspace_inspection():
    assert _path_is_excluded_from_inspection("dist/GPT_INSTRUCTIONS.md") is False


def test_other_generated_paths_remain_excluded():
    assert _path_is_excluded_from_inspection("build/app.js") is True
