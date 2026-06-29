from pathlib import Path


def test_validation_workflow_push_runs_only_for_main_branch() -> None:
    workflow = Path(".github/workflows/gpt-validation.yml").read_text(encoding="utf-8")
    push_block = workflow.split("pull_request:", 1)[0]

    assert '"gpt/**"' not in workflow
    assert "push:" in push_block
    assert "branches:" in push_block
    assert "- main" in push_block
