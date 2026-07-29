from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError

from gpt_stock_monitor.models import (
    Availability,
    EmptyCandidate,
    HealthRecord,
    PendingEvent,
    Product,
    Snapshot,
    StateDocument,
    state_key,
)
from gpt_stock_monitor.state import (
    PublishResult,
    PublishStatus,
    StateError,
    StateRepository,
    StateRepositoryError,
    VersionedState,
    load_state,
    prune_unconfigured,
    serialize_state,
    write_state_atomic,
)
from gpt_stock_monitor.state_git import StateGitError


def make_snapshot(monitor_id: str, category: str, product_key: str) -> Snapshot:
    return Snapshot(
        monitor_id=monitor_id,
        category=category,
        products=(
            Product(
                key=product_key,
                name="GPT 成品号",
                price="13.50",
                price_text="￥13.50",
                availability=Availability.IN_STOCK,
                stock_text="有货",
            ),
        ),
    )


def make_populated_state() -> StateDocument:
    active_key = state_key("monitor-a", "GPT Plus")
    stale_key = state_key("monitor-b", "GPT Pro")
    return StateDocument(
        snapshots={
            stale_key: make_snapshot("monitor-b", "GPT Pro", "product-b"),
            active_key: make_snapshot("monitor-a", "GPT Plus", "product-a"),
        },
        health={
            stale_key: HealthRecord(consecutive_failures=2, last_error="safe summary"),
            active_key: HealthRecord(consecutive_failures=1),
        },
        empty_candidates={
            stale_key: EmptyCandidate(consecutive_observations=3),
            active_key: EmptyCandidate(consecutive_observations=2),
        },
        pending_events=(
            PendingEvent(sequence=4, event_id="event-z", message_parts=("后",)),
            PendingEvent(sequence=2, event_id="event-a", message_parts=("先",)),
        ),
        delivered_event_ids=("delivered-b", "delivered-a"),
        next_event_sequence=5,
    )


def test_load_state_returns_empty_document_when_file_does_not_exist(tmp_path: Path) -> None:
    assert load_state(tmp_path / "missing.json") == StateDocument()


@pytest.mark.parametrize(
    "contents",
    [
        b'{"schema_version":2}',
        b"not json",
        b"[]",
    ],
    ids=["unknown-schema", "malformed-json", "non-object-json"],
)
def test_load_state_rejects_invalid_documents_without_echoing_contents(
    tmp_path: Path, contents: bytes
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(contents)

    with pytest.raises(StateError, match=r"^invalid state document$") as caught:
        load_state(path)

    assert contents.decode("utf-8", errors="ignore") not in str(caught.value)


def test_load_state_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"schema_version": 1, "snapshosts": {}}', encoding="utf-8")

    with pytest.raises(StateError, match=r"^invalid state document$"):
        load_state(path)


def test_serialize_state_is_identical_for_semantically_identical_mapping_order() -> None:
    first = make_populated_state()
    second = StateDocument(
        snapshots=dict(reversed(tuple(first.snapshots.items()))),
        health=dict(reversed(tuple(first.health.items()))),
        empty_candidates=dict(reversed(tuple(first.empty_candidates.items()))),
        pending_events=tuple(reversed(first.pending_events)),
        delivered_event_ids=first.delivered_event_ids,
        next_event_sequence=first.next_event_sequence,
    )

    assert serialize_state(first) == serialize_state(second)


def test_serialize_state_has_explicit_utf8_and_json_format_contract() -> None:
    state = StateDocument(
        pending_events=(PendingEvent(sequence=1, event_id="事件-1", message_parts=("有货",)),),
        next_event_sequence=2,
    )

    serialized = serialize_state(state)

    assert (
        serialized
        == (
            "{\n"
            '  "delivered_event_ids": [],\n'
            '  "empty_candidates": {},\n'
            '  "health": {},\n'
            '  "next_event_sequence": 2,\n'
            '  "pending_events": [\n'
            "    {\n"
            '      "event_id": "事件-1",\n'
            '      "message_parts": [\n'
            '        "有货"\n'
            "      ],\n"
            '      "sequence": 1\n'
            "    }\n"
            "  ],\n"
            '  "schema_version": 1,\n'
            '  "snapshots": {}\n'
            "}\n"
        ).encode()
    )
    assert b"\\u" not in serialized
    assert json.loads(serialized) == state.model_dump(mode="json")


def test_load_state_rebuilds_validated_deeply_immutable_document(tmp_path: Path) -> None:
    expected = make_populated_state()
    path = tmp_path / "state.json"
    path.write_bytes(serialize_state(expected))

    loaded = load_state(path)

    assert loaded == expected
    with pytest.raises(TypeError):
        loaded.snapshots["other"] = make_snapshot("other", "other", "other")  # type: ignore[index]
    with pytest.raises(ValidationError):
        loaded.pending_events[0].event_id = "replacement"  # type: ignore[misc]


def test_write_state_atomic_replaces_from_same_directory_without_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "nested" / "state.json"
    observed_source: Path | None = None
    real_replace = os.replace

    def recording_replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        nonlocal observed_source
        observed_source = Path(source)
        assert observed_source.parent == target.parent
        assert observed_source.is_file()
        real_replace(source, destination)

    monkeypatch.setattr("gpt_stock_monitor.state.os.replace", recording_replace)

    state = make_populated_state()
    write_state_atomic(target, state)

    assert target.read_bytes() == serialize_state(state)
    assert observed_source is not None
    assert list(target.parent.iterdir()) == [target]


def test_write_state_atomic_preserves_old_file_and_cleans_temp_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    old_contents = b"old state"
    target.write_bytes(old_contents)
    observed_source: Path | None = None

    def failing_replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        del destination
        nonlocal observed_source
        observed_source = Path(source)
        assert observed_source.parent == target.parent
        assert observed_source.is_file()
        raise OSError("replace failed")

    monkeypatch.setattr("gpt_stock_monitor.state.os.replace", failing_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_state_atomic(target, make_populated_state())

    assert target.read_bytes() == old_contents
    assert observed_source is not None
    assert not observed_source.exists()
    assert list(tmp_path.iterdir()) == [target]


def test_write_state_atomic_closes_raw_descriptor_when_fdopen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SentinelError(Exception):
        pass

    target = tmp_path / "state.json"
    old_contents = b"old state"
    target.write_bytes(old_contents)
    observed_descriptor: int | None = None

    def failing_fdopen(descriptor: int, mode: str) -> None:
        nonlocal observed_descriptor
        observed_descriptor = descriptor
        assert mode == "wb"
        raise SentinelError("fdopen sentinel")

    monkeypatch.setattr("gpt_stock_monitor.state.os.fdopen", failing_fdopen)

    with pytest.raises(SentinelError, match="fdopen sentinel"):
        write_state_atomic(target, make_populated_state())

    assert observed_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(observed_descriptor)
    assert target.read_bytes() == old_contents
    assert list(tmp_path.iterdir()) == [target]


def test_prune_unconfigured_filters_scoped_state_and_preserves_event_bookkeeping() -> None:
    original = make_populated_state()
    active_key = state_key("monitor-a", "GPT Plus")

    pruned = prune_unconfigured(original, {active_key})

    assert tuple(pruned.snapshots) == (active_key,)
    assert tuple(pruned.health) == (active_key,)
    assert tuple(pruned.empty_candidates) == (active_key,)
    assert pruned.pending_events == original.pending_events
    assert pruned.delivered_event_ids == original.delivered_event_ids
    assert pruned.next_event_sequence == original.next_event_sequence
    assert len(original.snapshots) == 2


def test_prune_unconfigured_is_semantically_unchanged_when_all_keys_are_active() -> None:
    state = make_populated_state()
    active_keys = set(state.snapshots) | set(state.health) | set(state.empty_candidates)

    assert prune_unconfigured(state, active_keys) == state


def test_repository_value_types_have_fixed_protocol_contract() -> None:
    state = StateDocument()
    versioned = VersionedState(version=None, document=state)
    result = PublishResult(status=PublishStatus.CONFLICT, remote_version="remote-sha")

    assert PublishStatus.PUBLISHED == "published"
    assert PublishStatus.UNCHANGED == "unchanged"
    assert PublishStatus.CONFLICT == "conflict"
    assert versioned == VersionedState(None, state)
    assert result == PublishResult(PublishStatus.CONFLICT, "remote-sha")
    assert StateRepository.__name__ == "StateRepository"
    with pytest.raises(FrozenInstanceError):
        versioned.version = "new"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.remote_version = None  # type: ignore[misc]


def test_git_repository_error_uses_the_public_repository_error_boundary() -> None:
    assert issubclass(StateRepositoryError, RuntimeError)
    assert issubclass(StateGitError, StateRepositoryError)
