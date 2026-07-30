from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PYPROJECT = ROOT / "pyproject.toml"
APPROVED_ACTIONS: dict[str, tuple[str, str]] = {
    "actions/checkout": ("df4cb1c069e1874edd31b4311f1884172cec0e10", "v6.0.3"),
    "actions/setup-python": ("a309ff8b426b58ec0e2a45f0f869d46889d02405", "v6.2.0"),
}
EXPECTED_MONITOR_JOB_ENV = {
    "GIT_AUTHOR_NAME": "github-actions[bot]",
    "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "github-actions[bot]",
    "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
}
RUNNER_TMPDIR = "${{ runner.temp }}"
PRIVILEGED_RUNTIME_SOCKETS = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/containerd/containerd.sock",
    "/var/run/podman/podman.sock",
)
RESTRICTED_SETPRIV = "setpriv --clear-groups --no-new-privs"
EXPECTED_INSTALL_COMMANDS = {
    "ci.yml": (
        'python -m pip install -e ".[dev]"',
        "python -m playwright install --with-deps chromium",
    ),
    "monitor.yml": (
        "python -m pip install .",
        "python -m playwright install --with-deps chromium",
    ),
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


def all_steps(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for job in data["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step, dict)
    ]


def nested_values(node: Any, key: str) -> list[str]:
    values: list[str] = []
    if isinstance(node, dict):
        for nested_key, value in node.items():
            if nested_key == key:
                assert isinstance(value, str)
                values.append(value)
            values.extend(nested_values(value, key))
    elif isinstance(node, list):
        for value in node:
            values.extend(nested_values(value, key))
    return values


def assert_action_contract(text: str) -> None:
    data = yaml.load(text, Loader=yaml.BaseLoader)
    parsed_uses = nested_values(data, "uses")
    line_pattern = re.compile(
        r"^\s*(?:-\s*)?uses:\s*"
        r"(?P<action>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
        r"@(?P<sha>[0-9a-f]{40})\s+#\s+(?P<version>v\d+\.\d+\.\d+)\s*$"
    )
    validated: list[str] = []
    for line in text.splitlines():
        match = line_pattern.fullmatch(line)
        if match is None:
            continue
        action = match.group("action")
        assert action in APPROVED_ACTIONS
        expected_sha, expected_version = APPROVED_ACTIONS[action]
        assert match.group("sha") == expected_sha
        assert match.group("version") == expected_version
        validated.append(f"{action}@{match.group('sha')}")

    assert parsed_uses
    assert len(validated) == len(parsed_uses)
    assert validated == parsed_uses


def assert_monitor_secret_contract(data: dict[str, Any], text: str) -> None:
    webhook_key = "FEISHU_WEBHOOK_URL"
    secret_reference = "${{ secrets.FEISHU_WEBHOOK_URL }}"
    top_level_env = data.get("env") or {}
    assert webhook_key not in top_level_env

    job = data["jobs"]["monitor"]
    assert job["env"] == EXPECTED_MONITOR_JOB_ENV
    assert text.count("secrets.FEISHU_WEBHOOK_URL") == 1

    steps = steps_for(data, "monitor")
    live = next(step for step in steps if step.get("run", "").strip() == "gpt-stock-monitor")
    dry = next(
        step for step in steps if step.get("run", "").strip() == "gpt-stock-monitor --dry-run"
    )
    assert live.get("env") == {"TMPDIR": RUNNER_TMPDIR, webhook_key: secret_reference}
    assert dry.get("env") == {"TMPDIR": RUNNER_TMPDIR}


def assert_monitor_cli_contract(data: dict[str, Any]) -> None:
    command_steps = [step for step in all_steps(data) if "gpt-stock-monitor" in step.get("run", "")]
    commands = [step["run"].strip() for step in command_steps]
    assert commands == ["gpt-stock-monitor", "gpt-stock-monitor --dry-run"]

    live, dry = command_steps
    assert live["if"] == "github.event_name == 'schedule' || inputs.dry_run == false"
    assert dry["if"] == "github.event_name == 'workflow_dispatch' && inputs.dry_run == true"


def assert_install_contract(name: str, data: dict[str, Any]) -> None:
    install_commands = [
        step["run"].strip()
        for step in all_steps(data)
        if re.search(r"\binstall\b", step.get("run", ""))
    ]
    assert install_commands == list(EXPECTED_INSTALL_COMMANDS[name])


def assert_windows_tzdata_contract(text: str) -> None:
    metadata = tomllib.loads(text)
    dependencies = metadata["project"]["dependencies"]
    expected = "tzdata; platform_system == 'Windows'"
    assert dependencies.count(expected) == 1
    assert all(
        not dependency.startswith("tzdata") or dependency == expected for dependency in dependencies
    )


def assert_ruff_toolchain_contract(text: str) -> None:
    metadata = tomllib.loads(text)
    dev_dependencies = metadata["project"]["optional-dependencies"]["dev"]
    assert dev_dependencies.count("ruff==0.16.0") == 1
    assert all(
        not dependency.startswith("ruff") or dependency == "ruff==0.16.0"
        for dependency in dev_dependencies
    )
    assert metadata["tool"]["ruff"]["extend-exclude"] == ["docs/superpowers"]


def assert_ci_egress_guard(data: dict[str, Any]) -> None:
    steps = steps_for(data, "quality")
    install_indexes = [
        index
        for index, step in enumerate(steps)
        if step.get("run", "").strip() in EXPECTED_INSTALL_COMMANDS["ci.yml"]
    ]
    test_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if "python -m pytest -q" in step.get("run", "")
    ]
    assert len(test_steps) == 1
    test_index, test_step = test_steps[0]
    assert install_indexes and max(install_indexes) < test_index
    assert test_step.get("shell") == "bash"

    script = test_step["run"]
    assert script.splitlines()[0] == "set -euo pipefail"
    assert script.count("trap cleanup EXIT") == 1
    pytest_command = f"{RESTRICTED_SETPRIV} python -m pytest -q"
    socket_check = f'{RESTRICTED_SETPRIV} test ! -r "$socket"'
    assert script.count(pytest_command) == 1
    assert script.count(socket_check) == 1
    assert script.count('for socket in "${runtime_sockets[@]}"; do') == 1
    assert script.count('if [[ -e "$socket" ]]; then') == 1
    script_lines = [line.strip() for line in script.splitlines()]
    for socket in PRIVILEGED_RUNTIME_SOCKETS:
        assert script_lines.count(socket) == 1
    assert script.index(socket_check) < script.index(pytest_command)

    for command in (
        "sudo iptables -I OUTPUT 1 -j REJECT",
        "sudo iptables -I OUTPUT 1 -o lo -j ACCEPT",
        "sudo iptables -I OUTPUT 1 -m conntrack --ctstate ESTABLISHED -j ACCEPT",
        "sudo ip6tables -I OUTPUT 1 -j REJECT",
        "sudo ip6tables -I OUTPUT 1 -o lo -j ACCEPT",
        "sudo ip6tables -I OUTPUT 1 -m conntrack --ctstate ESTABLISHED -j ACCEPT",
        "sudo iptables -D OUTPUT -m conntrack --ctstate ESTABLISHED -j ACCEPT",
        "sudo iptables -D OUTPUT -o lo -j ACCEPT",
        "sudo iptables -D OUTPUT -j REJECT",
        "sudo ip6tables -D OUTPUT -m conntrack --ctstate ESTABLISHED -j ACCEPT",
        "sudo ip6tables -D OUTPUT -o lo -j ACCEPT",
        "sudo ip6tables -D OUTPUT -j REJECT",
    ):
        assert script.count(command) == 1

    for family in ("iptables", "ip6tables"):
        reject = script.index(f"sudo {family} -I OUTPUT 1 -j REJECT")
        loopback = script.index(f"sudo {family} -I OUTPUT 1 -o lo -j ACCEPT")
        established = script.index(
            f"sudo {family} -I OUTPUT 1 -m conntrack --ctstate ESTABLISHED -j ACCEPT"
        )
        assert reject < loopback < established < script.index(pytest_command)


def assert_no_force_push(data: dict[str, Any]) -> None:
    for step in all_steps(data):
        command = step.get("run", "")
        assert not re.search(r"\bgit\s+config\s+--global\b", command)
        for match in re.finditer(r"(?im)\bgit[ \t]+push\b(?P<args>[^\r\n;&|]*)", command):
            tokens = shlex.split(match.group("args"), posix=True)
            assert not any(token.startswith("--force") for token in tokens)
            assert not any(
                token.startswith("-") and not token.startswith("--") and "f" in token[1:]
                for token in tokens
            )
            assert not any(token.startswith("+") for token in tokens)


@pytest.mark.parametrize("name", ["ci.yml", "monitor.yml"])
def test_actions_are_allowlisted_sha_pinned_and_version_commented(name: str) -> None:
    data, text = load_workflow(name)
    assert_action_contract(text)
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


def test_windows_installs_tzdata_from_main_dependencies() -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    assert_windows_tzdata_contract(text)


def test_ruff_version_and_historical_plan_exclusion_are_reproducible() -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    assert_ruff_toolchain_contract(text)


def test_ci_uses_python_312_pip_cache_and_expected_quality_commands() -> None:
    data, _ = load_workflow("ci.yml")
    steps = steps_for(data, "quality")
    setup_python = next(
        step for step in steps if step.get("uses", "").startswith("actions/setup-python@")
    )

    assert setup_python["with"] == {"python-version": "3.12", "cache": "pip"}

    run_steps = [(index, step["run"].strip()) for index, step in enumerate(steps) if "run" in step]
    run_commands = [command for _, command in run_steps]
    assert 'python -m pip install -e ".[dev]"' in run_commands
    assert "python -m playwright install --with-deps chromium" in run_commands
    quality_commands = [
        "ruff format --check .",
        "ruff check .",
        "mypy src",
    ]
    quality_indexes = [
        next(index for index, run in run_steps if run == command) for command in quality_commands
    ]
    pytest_index = next(
        index for index, run in run_steps if f"{RESTRICTED_SETPRIV} python -m pytest -q" in run
    )
    assert [*quality_indexes, pytest_index] == sorted([*quality_indexes, pytest_index])


def test_ci_checkout_drops_credentials_and_pytest_has_os_egress_guard() -> None:
    data, _ = load_workflow("ci.yml")
    checkout = next(
        step
        for step in steps_for(data, "quality")
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout.get("with") == {"persist-credentials": "false"}
    assert_ci_egress_guard(data)


def test_workflows_install_only_through_approved_python_and_playwright_paths() -> None:
    for name in ("ci.yml", "monitor.yml"):
        data, _ = load_workflow(name)
        assert_install_contract(name, data)


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
    assert job["environment"] == "monitor-production"
    assert job["env"] == EXPECTED_MONITOR_JOB_ENV

    steps = steps_for(data, "monitor")
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout.get("with", {}).get("persist-credentials", "true") == "true"
    setup_python = next(
        step for step in steps if step.get("uses", "").startswith("actions/setup-python@")
    )
    assert setup_python["with"] == {"python-version": "3.12", "cache": "pip"}
    run_commands = [step["run"].strip() for step in steps if "run" in step]
    assert "python -m pip install ." in run_commands
    assert "python -m playwright install --with-deps chromium" in run_commands


def test_static_security_contracts_reject_metadata_and_workflow_mutants() -> None:
    pyproject_text = PYPROJECT.read_text(encoding="utf-8")
    with pytest.raises(AssertionError):
        assert_windows_tzdata_contract(
            pyproject_text.replace("tzdata; platform_system == 'Windows'", "tzdata", 1)
        )

    ci_data, ci_text = load_workflow("ci.yml")
    assert_ci_egress_guard(ci_data)
    mutations = (
        ("sudo ip6tables -I OUTPUT 1 -j REJECT", "true"),
        ("-o lo -j ACCEPT", "-o eth0 -j ACCEPT"),
        ("--ctstate ESTABLISHED", "--ctstate NEW"),
        ("-j REJECT", "-j DROP"),
        (f"{RESTRICTED_SETPRIV} python -m pytest -q", "python -m pytest -q"),
        ("trap cleanup EXIT", "true"),
        ("sudo iptables -D OUTPUT -j REJECT", "true"),
    )
    for original, replacement in mutations:
        mutant = ci_text.replace(original, replacement, 1)
        with pytest.raises(AssertionError):
            assert_ci_egress_guard(yaml.load(mutant, Loader=yaml.BaseLoader))


def test_ci_runtime_socket_contract_rejects_clear_group_path_and_order_mutants() -> None:
    data, text = load_workflow("ci.yml")
    assert_ci_egress_guard(data)
    socket_check = f'{RESTRICTED_SETPRIV} test ! -r "$socket"'
    pytest_command = f"{RESTRICTED_SETPRIV} python -m pytest -q"
    mutations = [
        text.replace(socket_check, 'setpriv --no-new-privs test ! -r "$socket"', 1),
        text.replace(pytest_command, "setpriv --no-new-privs python -m pytest -q", 1),
        text.replace(socket_check, "true", 1),
        text.replace(socket_check, "__SOCKET_CHECK__", 1)
        .replace(pytest_command, socket_check, 1)
        .replace("__SOCKET_CHECK__", pytest_command, 1),
    ]
    mutations.extend(
        text.replace(f"            {socket}\n", "            /missing.sock\n", 1)
        for socket in PRIVILEGED_RUNTIME_SOCKETS
    )
    for mutant in mutations:
        with pytest.raises(AssertionError):
            assert_ci_egress_guard(yaml.load(mutant, Loader=yaml.BaseLoader))


def test_monitor_executes_exactly_one_of_live_and_dry_run_commands() -> None:
    data, text = load_workflow("monitor.yml")
    assert_monitor_cli_contract(data)
    assert_monitor_secret_contract(data, text)


@pytest.mark.parametrize("name", ["ci.yml", "monitor.yml"])
def test_workflows_forbid_global_git_config_and_force_push(name: str) -> None:
    data, _ = load_workflow(name)
    assert_no_force_push(data)


def test_action_contract_accepts_dash_uses_and_rejects_pin_mutations() -> None:
    _, text = load_workflow("ci.yml")
    dash_uses = text.replace("        uses:", "      - uses:", 1)
    assert_action_contract(dash_uses)

    mutants = [
        text.replace("# v6.0.3", "# v6.0.4", 1),
        text.replace(
            "    steps:\n",
            "    steps:\n"
            "      - {uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10}\n",
            1,
        ),
    ]
    for mutant in mutants:
        with pytest.raises(AssertionError):
            assert_action_contract(mutant)


def test_monitor_secret_contract_rejects_scope_mutations() -> None:
    data, text = load_workflow("monitor.yml")
    assert_monitor_secret_contract(data, text)

    mutants = [
        text.replace(
            "permissions:\n",
            "env:\n  FEISHU_WEBHOOK_URL: ${{ secrets.FEISHU_WEBHOOK_URL }}\n\npermissions:\n",
            1,
        ),
        text.replace(
            "permissions:\n",
            "env:\n  ALIAS: ${{secrets.FEISHU_WEBHOOK_URL}}\n\npermissions:\n",
            1,
        ),
        text.replace(
            "      GIT_AUTHOR_NAME: github-actions[bot]\n",
            "      GIT_AUTHOR_NAME: github-actions[bot]\n"
            "      FEISHU_WEBHOOK_URL: ${{ secrets.FEISHU_WEBHOOK_URL }}\n",
            1,
        ),
        text.replace(
            "        run: gpt-stock-monitor --dry-run\n"
            "        env:\n"
            "          TMPDIR: ${{ runner.temp }}\n",
            "        run: gpt-stock-monitor --dry-run\n"
            "        env:\n"
            "          TMPDIR: ${{ runner.temp }}\n"
            "          FEISHU_WEBHOOK_URL: ${{ secrets.FEISHU_WEBHOOK_URL }}\n",
            1,
        ),
        text.replace(
            "    env:\n",
            "    env:\n      TMPDIR: ${{ runner.temp }}\n",
            1,
        ),
        text.replace(
            "        env:\n"
            "          TMPDIR: ${{ runner.temp }}\n"
            "          FEISHU_WEBHOOK_URL: ${{ secrets.FEISHU_WEBHOOK_URL }}\n",
            "        env:\n          FEISHU_WEBHOOK_URL: ${{ secrets.FEISHU_WEBHOOK_URL }}\n",
            1,
        ),
        text.replace(
            "        run: gpt-stock-monitor --dry-run\n"
            "        env:\n"
            "          TMPDIR: ${{ runner.temp }}\n",
            "        run: gpt-stock-monitor --dry-run\n",
            1,
        ),
    ]
    for mutant in mutants:
        mutant_data = yaml.load(mutant, Loader=yaml.BaseLoader)
        with pytest.raises(AssertionError):
            assert_monitor_secret_contract(mutant_data, mutant)


def test_monitor_cli_contract_rejects_extra_invocations() -> None:
    data, text = load_workflow("monitor.yml")
    assert_monitor_cli_contract(data)

    mutants = [
        text
        + "\n      - name: Hidden second invocation\n"
        + "        run: echo duplicate && gpt-stock-monitor\n",
        text
        + "\n  hidden-job:\n"
        + "    runs-on: ubuntu-latest\n"
        + "    steps:\n"
        + "      - run: gpt-stock-monitor\n",
    ]
    for mutant in mutants:
        with pytest.raises(AssertionError):
            assert_monitor_cli_contract(yaml.load(mutant, Loader=yaml.BaseLoader))


@pytest.mark.parametrize("name", ["ci.yml", "monitor.yml"])
def test_install_contract_rejects_unapproved_install_commands(name: str) -> None:
    data, text = load_workflow(name)
    assert_install_contract(name, data)
    mutant = (
        text
        + "\n      - name: Install unapproved package\n"
        + "        run: python -m pip install unapproved-package\n"
    )

    with pytest.raises(AssertionError):
        assert_install_contract(name, yaml.load(mutant, Loader=yaml.BaseLoader))


@pytest.mark.parametrize("name", ["ci.yml", "monitor.yml"])
def test_force_push_contract_rejects_variants_without_false_positives(name: str) -> None:
    data, text = load_workflow(name)
    assert_no_force_push(data)

    safe_text = (
        text
        + "\n      - name: Pip option is not Git\n"
        + "        run: python -m pip install --force-reinstall .\n"
    )
    assert_no_force_push(yaml.load(safe_text, Loader=yaml.BaseLoader))

    for command in (
        "git push --force origin HEAD",
        "git push --force-with-lease origin HEAD",
        "git push -f origin HEAD",
        "git push origin +HEAD:main",
    ):
        mutant = text + f"\n      - name: Unsafe push\n        run: {command}\n"
        with pytest.raises(AssertionError):
            assert_no_force_push(yaml.load(mutant, Loader=yaml.BaseLoader))
