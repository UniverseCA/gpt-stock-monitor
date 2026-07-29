from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
APPROVED_ACTIONS = {
    "actions/checkout": "df4cb1c069e1874edd31b4311f1884172cec0e10",
    "actions/setup-python": "a309ff8b426b58ec0e2a45f0f869d46889d02405",
}


def load_workflow(name: str) -> tuple[dict[str, Any], str]:
    path = WORKFLOWS / name
    assert path.is_file(), f"missing workflow: {path}"
    text = path.read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    assert isinstance(data, dict)
    return data, text


def steps_for(data: dict[str, Any], job: str) -> list[dict[str, Any]]:
    steps = data["jobs"][job]["steps"]
    assert isinstance(steps, list)
    return steps


@pytest.mark.parametrize("name", ["ci.yml", "monitor.yml"])
def test_actions_are_allowlisted_sha_pinned_and_version_commented(name: str) -> None:
    data, text = load_workflow(name)
    uses_lines = [line for line in text.splitlines() if re.match(r"^\s*uses:", line)]
    assert uses_lines

    for line in uses_lines:
        match = re.fullmatch(
            r"\s*uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
            r"@([0-9a-f]{40})\s+#\s+v\d+\.\d+\.\d+\s*",
            line,
        )
        assert match, f"action must use a 40-character SHA and version comment: {line}"
        action, sha = match.groups()
        assert action in APPROVED_ACTIONS
        assert sha == APPROVED_ACTIONS[action]

    assert data["permissions"] in ({"contents": "read"}, {"contents": "write"})


def test_ci_has_read_only_permissions_and_safe_pull_request_triggers() -> None:
    data, text = load_workflow("ci.yml")

    assert set(data["on"]) == {"push", "pull_request"}
    assert data["permissions"] == {"contents": "read"}
    assert "secrets." not in text
    assert "${{ secrets." not in text

    job = data["jobs"]["quality"]
    assert job["runs-on"] == "ubuntu-latest"
    assert int(job["timeout-minutes"]) > 0


def test_ci_uses_python_312_pip_cache_and_expected_quality_commands() -> None:
    data, _ = load_workflow("ci.yml")
    steps = steps_for(data, "quality")
    setup_python = next(
        step for step in steps if step.get("uses", "").startswith("actions/setup-python@")
    )

    assert setup_python["with"] == {"python-version": "3.12", "cache": "pip"}

    run_commands = [step["run"].strip() for step in steps if "run" in step]
    assert 'python -m pip install -e ".[dev]"' in run_commands
    assert "python -m playwright install --with-deps chromium" in run_commands
    quality_commands = [
        "ruff format --check .",
        "ruff check .",
        "mypy src",
        "pytest -q",
    ]
    assert [run_commands.index(command) for command in quality_commands] == sorted(
        run_commands.index(command) for command in quality_commands
    )


def test_workflows_install_only_through_approved_python_and_playwright_paths() -> None:
    for name in ("ci.yml", "monitor.yml"):
        _, text = load_workflow(name)
        lowered = text.lower()
        assert "curl " not in lowered
        assert "wget " not in lowered
        assert "pip install git+" not in lowered
        assert "--index-url" not in lowered
        assert "--extra-index-url" not in lowered
        assert "playwright install chromium" not in lowered
        assert "python -m playwright install --with-deps chromium" in lowered


def test_monitor_has_only_scheduled_and_safe_manual_triggers() -> None:
    data, _ = load_workflow("monitor.yml")

    assert set(data["on"]) == {"schedule", "workflow_dispatch"}
    assert data["on"]["schedule"] == [{"cron": "*/5 * * * *"}]
    assert data["on"]["workflow_dispatch"]["inputs"]["dry_run"] == {
        "description": "Run without sending notifications",
        "required": "true",
        "type": "boolean",
        "default": "true",
    }
    assert "pull_request" not in data["on"]
    assert data["permissions"] == {"contents": "write"}
    assert data["concurrency"] == {
        "group": "gpt-stock-monitor",
        "cancel-in-progress": "false",
    }


def test_monitor_job_is_bounded_and_uses_isolated_temp_state_and_bot_identity() -> None:
    data, _ = load_workflow("monitor.yml")
    job = data["jobs"]["monitor"]

    assert job["runs-on"] == "ubuntu-latest"
    assert int(job["timeout-minutes"]) > 0
    assert job["env"] == {
        "TMPDIR": "${{ runner.temp }}",
        "GIT_AUTHOR_NAME": "github-actions[bot]",
        "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "github-actions[bot]",
        "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
    }

    steps = steps_for(data, "monitor")
    setup_python = next(
        step for step in steps if step.get("uses", "").startswith("actions/setup-python@")
    )
    assert setup_python["with"] == {"python-version": "3.12", "cache": "pip"}
    run_commands = [step["run"].strip() for step in steps if "run" in step]
    assert "python -m pip install ." in run_commands
    assert "python -m playwright install --with-deps chromium" in run_commands


def test_monitor_executes_exactly_one_of_live_and_dry_run_commands() -> None:
    data, _ = load_workflow("monitor.yml")
    command_steps = [
        step
        for step in steps_for(data, "monitor")
        if step.get("run", "").strip() in {"gpt-stock-monitor", "gpt-stock-monitor --dry-run"}
    ]
    assert len(command_steps) == 2

    by_command = {step["run"].strip(): step for step in command_steps}
    live = by_command["gpt-stock-monitor"]
    dry = by_command["gpt-stock-monitor --dry-run"]
    assert live["if"] == "github.event_name == 'schedule' || inputs.dry_run == false"
    assert dry["if"] == "github.event_name == 'workflow_dispatch' && inputs.dry_run == true"
    assert live["env"] == {"FEISHU_WEBHOOK_URL": "${{ secrets.FEISHU_WEBHOOK_URL }}"}
    assert "env" not in dry or "FEISHU_WEBHOOK_URL" not in dry["env"]


def test_monitor_forbids_global_git_config_and_force_push() -> None:
    _, text = load_workflow("monitor.yml")
    lowered = text.lower()

    assert "git config --global" not in lowered
    assert "git push --force" not in lowered
    assert "git push -f" not in lowered
    assert "--force-with-lease" not in lowered
