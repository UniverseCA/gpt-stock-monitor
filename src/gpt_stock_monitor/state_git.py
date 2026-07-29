"""Publish monitor state to an isolated Git branch."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from gpt_stock_monitor.models import StateDocument
from gpt_stock_monitor.state import (
    PublishResult,
    PublishStatus,
    StateRepository,
    VersionedState,
    load_state,
    write_state_atomic,
)

__all__ = ["GitStateRepository", "StateGitError"]


_BOT_NAME = "github-actions[bot]"
_BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
_SAFE_GIT_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class StateGitError(RuntimeError):
    """A Git operation for monitor state failed without exposing Git output."""


class GitStateRepository(StateRepository):
    """A compare-and-swap state repository backed by a dedicated Git branch."""

    def __init__(
        self,
        repo_path: Path,
        temp_root: Path,
        remote: str = "origin",
        branch: str = "monitor-state",
        state_filename: str = "state.json",
    ) -> None:
        if not _SAFE_GIT_NAME.fullmatch(remote) or not _SAFE_GIT_NAME.fullmatch(branch):
            raise ValueError("invalid state git name")
        filename = Path(state_filename)
        if filename.is_absolute() or filename.name != state_filename:
            raise ValueError("invalid state filename")
        self._repo_path = repo_path.resolve()
        self._temp_root = temp_root.resolve()
        self._remote = remote
        self._branch = branch
        self._state_filename = state_filename

    def load(self) -> VersionedState:
        self._fetch()
        version = self._remote_version()
        if version is None:
            return VersionedState(None, StateDocument())
        with self._worktree(version) as worktree:
            return VersionedState(version, load_state(worktree / self._state_filename))

    def publish(
        self,
        expected_parent: str | None,
        state: StateDocument,
        message: str,
    ) -> PublishResult:
        self._fetch()
        remote_version = self._remote_version()
        if remote_version != expected_parent:
            return PublishResult(PublishStatus.CONFLICT, remote_version)

        with self._worktree(remote_version) as worktree:
            state_path = worktree / self._state_filename
            if remote_version is not None and load_state(state_path) == state:
                return PublishResult(PublishStatus.UNCHANGED, remote_version)

            write_state_atomic(state_path, state)
            self._require(
                self._git("add", "--", self._state_filename, cwd=worktree),
                "state git add failed",
            )
            status = self._require(
                self._git("status", "--porcelain", cwd=worktree),
                "state git status failed",
            )
            if not status.stdout.strip():
                return PublishResult(PublishStatus.UNCHANGED, remote_version)
            self._require(
                self._git(
                    "commit",
                    "-F",
                    "-",
                    cwd=worktree,
                    env=self._commit_environment(),
                    input_text=message,
                ),
                "state git commit failed",
            )
            published_version = self._require(
                self._git("rev-parse", "HEAD", cwd=worktree),
                "state git revision failed",
            ).stdout.strip()
            pushed = self._git(
                "push",
                self._remote,
                f"HEAD:refs/heads/{self._branch}",
                cwd=worktree,
            )
            if pushed.returncode == 0:
                return PublishResult(PublishStatus.PUBLISHED, published_version)

            self._fetch()
            latest_version = self._remote_version()
            if latest_version != expected_parent:
                return PublishResult(PublishStatus.CONFLICT, latest_version)
            raise StateGitError("state git push failed")

    def _fetch(self) -> None:
        self._require(
            self._git("fetch", "--prune", self._remote, cwd=self._repo_path),
            "state git fetch failed",
        )

    def _remote_version(self) -> str | None:
        result = self._git(
            "rev-parse",
            "--verify",
            f"refs/remotes/{self._remote}/{self._branch}",
            cwd=self._repo_path,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _git(
        self,
        *args: str,
        cwd: Path,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            input=input_text,
            check=False,
            shell=False,
            text=True,
            capture_output=True,
        )

    @staticmethod
    def _require(
        result: subprocess.CompletedProcess[str], message: str
    ) -> subprocess.CompletedProcess[str]:
        if result.returncode != 0:
            raise StateGitError(message)
        return result

    def _commit_environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "GIT_AUTHOR_NAME": _BOT_NAME,
            "GIT_AUTHOR_EMAIL": _BOT_EMAIL,
            "GIT_COMMITTER_NAME": _BOT_NAME,
            "GIT_COMMITTER_EMAIL": _BOT_EMAIL,
        }

    def _worktree(self, version: str | None) -> _TemporaryWorktree:
        return _TemporaryWorktree(self, version)


class _TemporaryWorktree:
    def __init__(self, repository: GitStateRepository, version: str | None) -> None:
        self._repository = repository
        self._version = version
        self.path: Path | None = None

    def __enter__(self) -> Path:
        repository = self._repository
        repository._temp_root.mkdir(parents=True, exist_ok=True)
        candidate = Path(tempfile.mkdtemp(prefix="state-git-", dir=repository._temp_root))
        self.path = candidate
        try:
            temporary = candidate.resolve()
            if (
                not temporary.is_relative_to(repository._temp_root)
                or temporary == repository._temp_root
            ):
                raise StateGitError("state git temporary path failed")
            self.path = temporary
            temporary.rmdir()
        except StateGitError:
            self._discard_unregistered_candidate()
            raise
        except OSError:
            self._discard_unregistered_candidate()
            raise StateGitError("state git temporary path failed") from None
        if self._version is None:
            # A failed first push can leave its unborn local branch behind. Use the
            # verified unique worktree name so a later CAS attempt is independent;
            # Actions checkouts are temporary, and branch deletion is intentionally
            # excluded from this repository's allowed Git command set.
            orphan_branch = f"state-publish-{temporary.name.removeprefix('state-git-')}"
            result = repository._git(
                "worktree",
                "add",
                "--orphan",
                "-b",
                orphan_branch,
                str(temporary),
                cwd=repository._repo_path,
            )
        else:
            result = repository._git(
                "worktree",
                "add",
                "--detach",
                str(temporary),
                self._version,
                cwd=repository._repo_path,
            )
        if result.returncode != 0:
            self._cleanup()
            raise StateGitError("state git worktree failed")
        return temporary

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback
        if self.path is None:
            return
        self._cleanup()

    def _cleanup(self) -> None:
        if self.path is None:
            return
        repository = self._repository
        path = self.path.resolve()
        if not path.is_relative_to(repository._temp_root) or path == repository._temp_root:
            raise StateGitError("state git cleanup path failed")
        repository._git(
            "worktree",
            "remove",
            "--force",
            str(path),
            cwd=repository._repo_path,
        )
        if path.exists():
            shutil.rmtree(path)

    def _discard_unregistered_candidate(self) -> None:
        if self.path is None:
            return
        repository = self._repository
        try:
            path = self.path.resolve()
            if path.is_relative_to(repository._temp_root) and path != repository._temp_root:
                shutil.rmtree(path)
        except OSError:
            pass
