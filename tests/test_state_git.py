from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gpt_stock_monitor.models import PendingEvent, StateDocument
from gpt_stock_monitor.state import PublishResult, PublishStatus, VersionedState
from gpt_stock_monitor.state_git import GitStateRepository, StateGitError

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def git(
    cwd: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=False,
        shell=False,
        text=True,
        capture_output=True,
    )


@pytest.fixture
def repositories(tmp_path: Path) -> tuple[Path, Path, Path]:
    remote = tmp_path / "remote-with-secret-token.git"
    seed = tmp_path / "seed"
    first = tmp_path / "first"
    second = tmp_path / "second"
    assert git(tmp_path, "init", "--bare", str(remote)).returncode == 0
    assert git(tmp_path, "init", "-b", "main", str(seed)).returncode == 0
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    assert git(seed, "add", "README.md").returncode == 0
    identity = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    assert git(seed, "commit", "-m", "seed", env=identity).returncode == 0
    assert git(seed, "remote", "add", "origin", str(remote)).returncode == 0
    assert git(seed, "push", "-u", "origin", "main").returncode == 0
    assert git(remote, "symbolic-ref", "HEAD", "refs/heads/main").returncode == 0
    assert git(tmp_path, "clone", str(remote), str(first)).returncode == 0
    assert git(tmp_path, "clone", str(remote), str(second)).returncode == 0
    return remote, first, second


def populated_state(event_id: str) -> StateDocument:
    return StateDocument(
        pending_events=(PendingEvent(sequence=1, event_id=event_id, message_parts=("available",)),),
        next_event_sequence=2,
    )


def repository(repo: Path, temp_root: Path) -> GitStateRepository:
    return GitStateRepository(repo_path=repo, temp_root=temp_root)


def test_load_returns_unversioned_empty_state_when_remote_branch_is_absent(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, first, _ = repositories

    assert repository(first, tmp_path / "worktrees").load() == VersionedState(None, StateDocument())


def test_first_publish_creates_orphan_state_only_branch_with_bot_identity(
    repositories: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, first, _ = repositories
    temp_root = tmp_path / "isolated"
    state = populated_state("first")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = repository(first, temp_root)

    result = repo.publish(None, state, "publish first state")

    assert result.status is PublishStatus.PUBLISHED
    assert result.remote_version is not None
    assert repo.load() == VersionedState(result.remote_version, state)
    assert git(first, "rev-parse", "HEAD").stdout.strip() != result.remote_version
    assert git(first, "show", "--format=", "--name-only", result.remote_version).stdout.split() == [
        "state.json"
    ]
    assert git(first, "show", "-s", "--format=%P", result.remote_version).stdout.strip() == ""
    assert git(first, "show", "-s", "--format=%an <%ae>", result.remote_version).stdout.strip() == (
        f"{BOT_NAME} <{BOT_EMAIL}>"
    )
    assert git(first, "show", "-s", "--format=%cn <%ce>", result.remote_version).stdout.strip() == (
        f"{BOT_NAME} <{BOT_EMAIL}>"
    )
    assert temp_root.is_absolute()
    assert list(temp_root.iterdir()) == []


def test_first_publish_creates_branch_even_when_state_is_empty(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, first, _ = repositories
    repo = repository(first, tmp_path / "worktrees")

    result = repo.publish(None, StateDocument(), "publish empty state")

    assert result.status is PublishStatus.PUBLISHED
    assert result.remote_version is not None
    assert repo.load() == VersionedState(result.remote_version, StateDocument())


def test_first_publish_can_be_retried_after_a_non_conflict_push_failure(
    repositories: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, first, _ = repositories
    repo = repository(first, tmp_path / "worktrees")
    state = populated_state("retry")
    real_run = subprocess.run
    failed_once = False
    calls: list[list[str]] = []

    def fail_first_push(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal failed_once
        calls.append(args)
        if args[1:2] == ["push"] and not failed_once:
            failed_once = True
            return subprocess.CompletedProcess(args, 1, "", "secret-token")
        return real_run(args, **kwargs)

    monkeypatch.setattr("gpt_stock_monitor.state_git.subprocess.run", fail_first_push)

    with pytest.raises(StateGitError, match=r"^state git push failed$") as caught:
        repo.publish(None, state, "first attempt")
    result = repo.publish(None, state, "second attempt")

    assert "secret-token" not in str(caught.value)
    assert result.status is PublishStatus.PUBLISHED
    assert repo.load() == VersionedState(result.remote_version, state)
    assert sum(call[1:2] == ["push"] for call in calls) == 2
    orphan_adds = [call for call in calls if "--orphan" in call]
    assert len(orphan_adds) == 2
    assert orphan_adds[0][5] != orphan_adds[1][5]
    assert all(call[5].startswith("state-publish-") for call in orphan_adds)
    assert not any("secret-token" in " ".join(call) for call in calls)
    assert not any(call[1:2] in (["branch"], ["update-ref"]) for call in calls)
    push_calls = [call for call in calls if call[1:2] == ["push"]]
    assert not any("--force" in call or "-f" in call for call in push_calls)


def test_unchanged_publish_does_not_create_a_commit(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, first, _ = repositories
    repo = repository(first, tmp_path / "worktrees")
    state = populated_state("same")
    initial = repo.publish(None, state, "initial")

    result = repo.publish(initial.remote_version, state, "must not commit")

    assert result.status is PublishStatus.UNCHANGED
    assert result.remote_version == initial.remote_version
    assert repo.load().version == initial.remote_version


def test_changed_publish_is_one_atomic_commit(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, first, _ = repositories
    repo = repository(first, tmp_path / "worktrees")
    initial = repo.publish(None, populated_state("old"), "initial")

    changed = repo.publish(initial.remote_version, populated_state("new"), "change")

    assert changed.status is PublishStatus.PUBLISHED
    assert changed.remote_version is not None
    commit_count = git(
        first, "rev-list", "--count", f"{initial.remote_version}..{changed.remote_version}"
    ).stdout.strip()
    parent = git(
        first, "show", "-s", "--format=%P", changed.remote_version
    ).stdout.strip()
    assert commit_count == "1"
    assert parent == initial.remote_version
    assert repo.load() == VersionedState(changed.remote_version, populated_state("new"))


def test_expected_parent_conflict_reports_latest_without_force_or_retry(
    repositories: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, first, second = repositories
    first_repo = repository(first, tmp_path / "first-worktrees")
    second_repo = repository(second, tmp_path / "second-worktrees")
    base = first_repo.publish(None, populated_state("base"), "base")
    assert second_repo.load().version == base.remote_version
    winner = first_repo.publish(base.remote_version, populated_state("winner"), "winner")
    calls: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr("gpt_stock_monitor.state_git.subprocess.run", recording_run)

    loser = second_repo.publish(base.remote_version, populated_state("loser"), "loser")

    assert loser.status is PublishStatus.CONFLICT
    assert loser.remote_version == winner.remote_version
    assert sum(call[1:2] == ["fetch"] for call in calls) == 1
    assert not any("--force" in call or "-f" in call for call in calls)
    assert not any(call[1:2] == ["commit"] for call in calls)


def test_rejected_push_becomes_conflict_with_latest_remote_version(
    repositories: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, first, second = repositories
    first_repo = repository(first, tmp_path / "first-worktrees")
    second_repo = repository(second, tmp_path / "second-worktrees")
    base = first_repo.publish(None, populated_state("base"), "base")
    assert second_repo.load().version == base.remote_version
    real_run = subprocess.run
    winner_version: str | None = None
    triggered = False

    def racing_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal triggered, winner_version
        if args[1:2] == ["push"] and not triggered:
            triggered = True
            winner_version = first_repo.publish(
                base.remote_version, populated_state("winner"), "winner"
            ).remote_version
        return real_run(args, **kwargs)

    monkeypatch.setattr("gpt_stock_monitor.state_git.subprocess.run", racing_run)

    result = second_repo.publish(base.remote_version, populated_state("loser"), "loser")

    assert triggered
    assert result == PublishResult(PublishStatus.CONFLICT, winner_version)
    assert first_repo.load() == VersionedState(winner_version, populated_state("winner"))

def test_git_commands_use_safe_subprocess_contract_and_do_not_contain_remote_secret(
    repositories: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, first, _ = repositories
    calls: list[tuple[list[str], dict[str, object]]] = []
    real_run = subprocess.run

    def recording_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return real_run(args, **kwargs)

    monkeypatch.setattr("gpt_stock_monitor.state_git.subprocess.run", recording_run)

    repository(first, tmp_path / "worktrees").publish(None, populated_state("safe"), "safe")

    assert calls
    for args, kwargs in calls:
        assert isinstance(args, list)
        assert kwargs["shell"] is False
        assert kwargs["check"] is False
        assert kwargs["text"] is True
        assert "secret-token" not in " ".join(args)
    commit_call = next(item for item in calls if item[0][1:2] == ["commit"])
    commit_env = commit_call[1]["env"]
    assert isinstance(commit_env, dict)
    assert commit_env["GIT_AUTHOR_NAME"] == BOT_NAME
    assert commit_env["GIT_AUTHOR_EMAIL"] == BOT_EMAIL
    assert commit_env["GIT_COMMITTER_NAME"] == BOT_NAME
    assert commit_env["GIT_COMMITTER_EMAIL"] == BOT_EMAIL


def test_failure_uses_fixed_safe_error_without_git_output(
    repositories: tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, first, _ = repositories

    def failing_fetch(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert args == ["git", "fetch", "--prune", "origin"]
        return subprocess.CompletedProcess(args, 1, "", "https://secret-token.invalid/repo")

    monkeypatch.setattr("gpt_stock_monitor.state_git.subprocess.run", failing_fetch)

    with pytest.raises(StateGitError, match=r"^state git fetch failed$") as caught:
        repository(first, tmp_path / "worktrees").load()

    assert "secret-token" not in str(caught.value)


def test_partial_worktree_is_cleaned_when_worktree_add_fails(
    repositories: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, first, _ = repositories
    temp_root = tmp_path / "worktrees"
    real_run = subprocess.run

    def failing_worktree_add(
        args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if args[1:3] == ["worktree", "add"]:
            partial_path = Path(args[-1])
            partial_path.mkdir(parents=True)
            (partial_path / "partial").write_text("residue", encoding="utf-8")
            return subprocess.CompletedProcess(args, 1, "", "secret-token")
        return real_run(args, **kwargs)

    monkeypatch.setattr("gpt_stock_monitor.state_git.subprocess.run", failing_worktree_add)

    with pytest.raises(StateGitError, match=r"^state git worktree failed$"):
        repository(first, temp_root).publish(None, populated_state("state"), "message")

    assert list(temp_root.iterdir()) == []


def test_load_prunes_a_remote_tracking_branch_deleted_from_the_remote(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, first, second = repositories
    repo = repository(first, tmp_path / "worktrees")
    published = repo.publish(None, populated_state("deleted"), "publish")
    assert published.status is PublishStatus.PUBLISHED
    assert git(second, "push", "origin", "--delete", "monitor-state").returncode == 0

    assert repo.load() == VersionedState(None, StateDocument())
