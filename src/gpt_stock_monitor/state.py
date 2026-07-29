"""Deterministic local persistence and repository contracts for monitor state."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from gpt_stock_monitor.models import StateDocument

__all__ = [
    "PublishResult",
    "PublishStatus",
    "StateError",
    "StateRepository",
    "StateRepositoryError",
    "VersionedState",
    "load_state",
    "prune_unconfigured",
    "serialize_state",
    "write_state_atomic",
]


class StateRepositoryError(RuntimeError):
    pass


class StateError(StateRepositoryError):
    """A state file could not be decoded or validated."""


def load_state(path: Path) -> StateDocument:
    """Load and validate a state document, or return an empty one if absent."""
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        return StateDocument()

    try:
        payload = json.loads(contents)
        if not isinstance(payload, dict):
            raise ValueError
        if set(payload) - set(StateDocument.model_fields):
            raise ValueError
        return StateDocument.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError, TypeError):
        raise StateError("invalid state document") from None


def serialize_state(state: StateDocument) -> bytes:
    """Serialize state using the repository's stable UTF-8 JSON format."""
    text = json.dumps(
        state.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return f"{text}\n".encode()


def write_state_atomic(path: Path, state: StateDocument) -> None:
    """Durably write state through a temporary file in the destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        try:
            temporary_file = os.fdopen(descriptor, "wb")
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        with temporary_file:
            temporary_file.write(serialize_state(state))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        has_primary_error = sys.exc_info()[0] is not None
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            if not has_primary_error:
                raise


def prune_unconfigured(state: StateDocument, active_keys: set[str]) -> StateDocument:
    """Remove monitor-scoped records whose configuration keys are no longer active."""
    payload = state.model_dump(mode="json")
    for field_name in ("snapshots", "health", "empty_candidates"):
        values = payload[field_name]
        if not isinstance(values, dict):
            raise TypeError("state mapping serialization must produce a dictionary")
        payload[field_name] = {key: value for key, value in values.items() if key in active_keys}
    return StateDocument.model_validate(payload)


@dataclass(frozen=True)
class VersionedState:
    """A validated state document and its repository version."""

    version: str | None
    document: StateDocument


class PublishStatus(StrEnum):
    """Outcome of one compare-and-swap publication attempt."""

    PUBLISHED = "published"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class PublishResult:
    """Publication outcome and current remote version, when available."""

    status: PublishStatus
    remote_version: str | None


class StateRepository(Protocol):
    """Storage boundary for loading and publishing versioned monitor state."""

    def load(self) -> VersionedState: ...

    def publish(
        self,
        expected_parent: str | None,
        state: StateDocument,
        message: str,
    ) -> PublishResult: ...
