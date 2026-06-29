from pathlib import Path


def test_validation_workflow_push_runs_for_arbitrary_branches() -> None:
    workflow = Path(".github/workflows/gpt-validation.yml").read_text(encoding="utf-8")

    assert '"gpt/**"' not in workflow
    assert "branches:" not in workflow.split("pull_request:", 1)[0]
