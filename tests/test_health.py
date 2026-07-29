from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from gpt_stock_monitor.health import (
    HealthEvent,
    HealthEventKind,
    Transition,
    observe_explicit_zero,
    record_failure,
    record_success,
)
from gpt_stock_monitor.models import (
    Availability,
    Change,
    ChangeKind,
    EmptyCandidate,
    HealthRecord,
    PendingEvent,
    Product,
    Snapshot,
    StateDocument,
    state_key,
)
from gpt_stock_monitor.state import serialize_state


def make_product(key: str = "product-1", *, name: str = "GPT Plus") -> Product:
    return Product(
        key=key,
        name=name,
        price="13.50",
        price_text="13.50 yuan",
        availability=Availability.IN_STOCK,
        stock_text="in stock",
        url=f"https://pay.ldxp.cn/goods/{key}",
    )


def make_snapshot(*products: Product) -> Snapshot:
    return Snapshot(monitor_id="demo", category="GPT Plus", products=products)


def make_state(**overrides: object) -> StateDocument:
    payload: dict[str, object] = {
        "snapshots": {},
        "health": {},
        "empty_candidates": {},
        "pending_events": (
            PendingEvent(sequence=1, event_id="pending-1", message_parts=("part",)),
        ),
        "delivered_event_ids": ("delivered-1",),
        "next_event_sequence": 2,
    }
    payload.update(overrides)
    return StateDocument.model_validate(payload)


def test_health_value_objects_are_frozen_and_events_are_tuples() -> None:
    event = HealthEvent(
        kind=HealthEventKind.FAILURE,
        key=state_key("demo", "GPT Plus"),
        count=1,
        reason="navigation failed",
    )
    transition = Transition(state=StateDocument(), events=(event,))

    assert event.kind == "failure"
    assert transition.events == (event,)
    assert isinstance(transition.events, tuple)
    with pytest.raises(FrozenInstanceError):
        event.count = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        transition.events = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    ("failure_count", "should_notify"),
    [(count, count in {1, 3, 12, 24, 36}) for count in range(1, 38)],
)
def test_record_failure_uses_fixed_reminder_schedule(
    failure_count: int, should_notify: bool
) -> None:
    key = state_key("demo", "GPT Plus")
    state = StateDocument()

    for count in range(1, failure_count + 1):
        transition = record_failure(state, key, f"safe reason {count}")
        state = transition.state

    record = state.health[key]
    assert record.consecutive_failures == failure_count
    assert record.last_error == f"safe reason {failure_count}"
    assert record.failure_notified is True
    if should_notify:
        assert transition.events == (
            HealthEvent(
                kind=HealthEventKind.FAILURE,
                key=key,
                count=failure_count,
                reason=f"safe reason {failure_count}",
            ),
        )
    else:
        assert transition.events == ()


def test_record_failure_clears_empty_candidate_without_changing_snapshot_or_input() -> None:
    key = state_key("demo", "GPT Plus")
    snapshot = make_snapshot(make_product())
    original = make_state(
        snapshots={key: snapshot},
        empty_candidates={key: EmptyCandidate(consecutive_observations=1)},
    )

    transition = record_failure(original, key, "site navigation failed")

    assert transition.state.snapshots[key] == snapshot
    assert key not in transition.state.empty_candidates
    assert key in original.empty_candidates
    assert original.health == {}
    with pytest.raises(TypeError):
        transition.state.health["other"] = HealthRecord()  # type: ignore[index]


def test_record_failure_converts_reason_once_for_consistent_pure_data() -> None:
    class ChangingReason:
        def __init__(self) -> None:
            self.calls = 0

        def __str__(self) -> str:
            self.calls += 1
            return f"safe reason {self.calls}"

    key = state_key("demo", "GPT Plus")
    reason = ChangingReason()

    transition = record_failure(StateDocument(), key, reason)  # type: ignore[arg-type]

    assert transition.state.health[key].last_error == "safe reason 1"
    assert transition.events[0].reason == "safe reason 1"  # type: ignore[union-attr]
    assert reason.calls == 1


@pytest.mark.parametrize(
    ("reason", "sentinel", "expected_reason"),
    [
        (
            "site navigation failed at "
            "https://open.feishu.cn/open-apis/bot/v2/hook/SENTINEL-WEBHOOK",
            "SENTINEL-WEBHOOK",
            "site navigation failed at <url>",
        ),
        (
            "site navigation failed; Authorization: Bearer SENTINEL-AUTH",
            "SENTINEL-AUTH",
            "monitor failure: <redacted>",
        ),
        (
            "site navigation failed; Cookie=session=SENTINEL-COOKIE",
            "SENTINEL-COOKIE",
            "monitor failure: <redacted>",
        ),
        (
            "site navigation failed; password=SENTINEL-PASSWORD",
            "SENTINEL-PASSWORD",
            "monitor failure: <redacted>",
        ),
        (
            "site navigation failed; ToKeN: SENTINEL-TOKEN",
            "SENTINEL-TOKEN",
            "monitor failure: <redacted>",
        ),
        (
            "site navigation failed; secret = SENTINEL-SECRET",
            "SENTINEL-SECRET",
            "monitor failure: <redacted>",
        ),
        (
            "site navigation failed; client_secret=SENTINEL-CLIENT",
            "SENTINEL-CLIENT",
            "monitor failure: <redacted>",
        ),
    ],
    ids=["url", "authorization", "cookie", "password", "token", "secret", "prefixed-secret"],
)
def test_record_failure_redacts_sensitive_reason_values(
    reason: str, sentinel: str, expected_reason: str
) -> None:
    key = state_key("demo", "GPT Plus")

    transition = record_failure(StateDocument(), key, reason)
    stored_reason = transition.state.health[key].last_error
    event_reason = transition.events[0].reason  # type: ignore[union-attr]

    assert stored_reason == event_reason
    assert stored_reason == expected_reason
    assert sentinel not in stored_reason


def test_record_failure_has_safe_bounded_reason_on_all_transition_surfaces() -> None:
    key = state_key("demo", "GPT Plus")
    sentinels = (
        "SENTINEL-WEBHOOK",
        "SENTINEL-AUTH",
        "SENTINEL-COOKIE",
        "SENTINEL-PASSWORD",
        "SENTINEL-TOKEN",
        "SENTINEL-SECRET",
    )
    webhook_url = (
        "https://open.feishu.cn/open-apis/bot/v2/hook/SENTINEL-WEBHOOK"
    )
    reason = (
        "site navigation failed\r\nforged log entry "
        f"{webhook_url} "
        "Authorization: Bearer SENTINEL-AUTH; "
        "Cookie=session=SENTINEL-COOKIE; "
        "password=SENTINEL-PASSWORD; token: SENTINEL-TOKEN; "
        "secret = SENTINEL-SECRET; "
        f"details={'x' * 1000}"
    )

    transition = record_failure(StateDocument(), key, reason)
    stored_reason = transition.state.health[key].last_error
    event_reason = transition.events[0].reason  # type: ignore[union-attr]
    serialized_state = serialize_state(transition.state).decode()
    text_surfaces = (
        str(transition.events),
        repr(transition.events),
        str(transition),
        repr(transition),
    )
    surfaces = (
        serialized_state,
        transition.state.model_dump_json(),
        *text_surfaces,
    )

    assert stored_reason == event_reason
    assert stored_reason == "monitor failure: <redacted>"
    assert "\r" not in stored_reason
    assert "\n" not in stored_reason
    assert len(stored_reason) <= 240
    assert "\\r" not in serialized_state
    assert "\\nforged" not in serialized_state
    assert all("\r" not in surface and "\n" not in surface for surface in text_surfaces)
    for surface in surfaces:
        assert webhook_url not in surface
        assert all(sentinel not in surface for sentinel in sentinels)


@pytest.mark.parametrize(
    ("reason", "sentinel"),
    [
        (
            "site navigation failed "
            "(headers={'Authorization': 'Bearer SENTINEL-AUTH WITH SPACE'}) retry pending",
            "SENTINEL-AUTH",
        ),
        (
            'site navigation failed '
            '(headers={"Cookie": "session=SENTINEL-COOKIE WITH SPACE"}) retry pending',
            "SENTINEL-COOKIE",
        ),
        (
            "site navigation failed "
            "(config={'client_secret': 'SENTINEL-CLIENT SECRET'}) retry pending",
            "SENTINEL-CLIENT",
        ),
    ],
    ids=["python-authorization", "json-cookie", "python-client-secret"],
)
def test_record_failure_redacts_quoted_mapping_values_on_all_surfaces(
    reason: str, sentinel: str
) -> None:
    key = state_key("demo", "GPT Plus")

    transition = record_failure(StateDocument(), key, reason)
    stored_reason = transition.state.health[key].last_error
    surfaces = (
        serialize_state(transition.state).decode(),
        transition.state.model_dump_json(),
        str(transition.events),
        repr(transition.events),
        str(transition),
        repr(transition),
    )

    assert stored_reason == "monitor failure: <redacted>"
    assert all(sentinel not in surface for surface in surfaces)


@pytest.mark.parametrize(
    ("reason", "forbidden_fragments"),
    [
        (
            'site navigation failed '
            '(headers={"X-API-Key": "SENTINEL-XAPI"}) retry pending',
            ("SENTINEL-XAPI",),
        ),
        (
            "site navigation failed "
            "(headers={'x-api-key': 'SENTINEL-XAPI-SINGLE'}) retry pending",
            ("SENTINEL-XAPI-SINGLE",),
        ),
        (
            "site navigation failed "
            "(headers={'Proxy-Authorization': 'Basic SENTINEL-PROXY WITHSPACE'}) "
            "retry pending",
            ("SENTINEL-PROXY", "WITHSPACE"),
        ),
        (
            'site navigation failed '
            '(Authorization: Bearer "SENTINEL-QUOTED WITHSPACE"); retry pending',
            ("SENTINEL-QUOTED", "WITHSPACE"),
        ),
    ],
    ids=["json-x-api-key", "python-x-api-key", "proxy-authorization", "quoted-bearer"],
)
def test_record_failure_redacts_sensitive_key_variants_and_full_values(
    reason: str, forbidden_fragments: tuple[str, ...]
) -> None:
    key = state_key("demo", "GPT Plus")

    transition = record_failure(StateDocument(), key, reason)
    stored_reason = transition.state.health[key].last_error
    surfaces = (
        serialize_state(transition.state).decode(),
        transition.state.model_dump_json(),
        str(transition.events),
        repr(transition.events),
        str(transition),
        repr(transition),
    )

    assert stored_reason == "monitor failure: <redacted>"
    assert all(
        fragment not in surface
        for surface in surfaces
        for fragment in forbidden_fragments
    )


@pytest.mark.parametrize(
    ("reason", "sentinel"),
    [
        (
            r'headers={\"Authorization\": \"Bearer \\\"SENTINEL-ESCAPED WITHSPACE\\\"\"}',
            "SENTINEL-ESCAPED",
        ),
        (r"failure {{'client_secret':: [[\\SENTINEL-NESTED]]", "SENTINEL-NESTED"),
        (r"failure path\\proxy_authorization\\SENTINEL-BACKSLASH", "SENTINEL-BACKSLASH"),
        (r"failure (((cookie\\=\\SENTINEL-MALFORMED", "SENTINEL-MALFORMED"),
    ],
    ids=["escaped-quotes", "nested", "backslashes", "unbalanced"],
)
def test_record_failure_uses_fixed_fallback_for_malformed_sensitive_reasons(
    reason: str, sentinel: str
) -> None:
    key = state_key("demo", "GPT Plus")

    transition = record_failure(StateDocument(), key, reason)
    stored_reason = transition.state.health[key].last_error
    surfaces = (
        serialize_state(transition.state).decode(),
        transition.state.model_dump_json(),
        str(transition.events),
        repr(transition.events),
        str(transition),
        repr(transition),
    )

    assert stored_reason == "monitor failure: <redacted>"
    assert all(sentinel not in surface for surface in surfaces)


@pytest.mark.parametrize(
    ("reason", "sentinel"),
    [
        ("request failed apikey=SENTINEL-APIKEY", "SENTINEL-APIKEY"),
        ("request failed passwd: SENTINEL-PASSWD", "SENTINEL-PASSWD"),
        ("request failed custom_credential=SENTINEL-CUSTOM", "SENTINEL-CUSTOM"),
        ("navigation timeout=30", "timeout=30"),
    ],
    ids=["apikey", "passwd", "unknown-credential", "structured-timeout"],
)
def test_record_failure_uses_fixed_fallback_for_any_structured_reason(
    reason: str, sentinel: str
) -> None:
    key = state_key("demo", "GPT Plus")

    transition = record_failure(StateDocument(), key, reason)
    stored_reason = transition.state.health[key].last_error
    surfaces = (
        serialize_state(transition.state).decode(),
        transition.state.model_dump_json(),
        str(transition.events),
        repr(transition.events),
        str(transition),
        repr(transition),
    )

    assert stored_reason == "monitor failure: <redacted>"
    assert all(sentinel not in surface for surface in surfaces)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (
            "navigation failed https://example.invalid/path?retry=1",
            "navigation failed <url>",
        ),
        ("navigation timeout", "navigation timeout"),
    ],
    ids=["url", "plain-timeout"],
)
def test_record_failure_preserves_safe_unstructured_reason(reason: str, expected: str) -> None:
    key = state_key("demo", "GPT Plus")

    transition = record_failure(StateDocument(), key, reason)

    assert transition.state.health[key].last_error == expected
    assert transition.events[0].reason == expected  # type: ignore[union-attr]


def test_record_success_is_silent_without_failures_and_preserves_other_state() -> None:
    key = state_key("demo", "GPT Plus")
    original = make_state()

    transition = record_success(original, key)

    assert transition == Transition(state=original, events=())
    assert transition.state is not original


def test_record_success_recovers_once_then_stays_silent() -> None:
    key = state_key("demo", "GPT Plus")
    failed = record_failure(StateDocument(), key, "temporary failure").state

    recovered = record_success(failed, key)
    later = record_success(recovered.state, key)

    assert key not in recovered.state.health
    assert recovered.events == (
        HealthEvent(
            kind=HealthEventKind.RECOVERY,
            key=key,
            count=1,
            reason=None,
        ),
    )
    assert later.events == ()
    assert key not in later.state.health


def test_record_success_clears_zero_candidate_for_non_empty_observation() -> None:
    key = state_key("demo", "GPT Plus")
    original = make_state(empty_candidates={key: EmptyCandidate()})

    transition = record_success(original, key)

    assert key not in transition.state.empty_candidates
    assert key in original.empty_candidates


def test_first_explicit_zero_is_success_but_keeps_snapshot_as_candidate() -> None:
    key = state_key("demo", "GPT Plus")
    snapshot = make_snapshot(make_product())
    failed = make_state(
        snapshots={key: snapshot},
        health={
            key: HealthRecord(
                consecutive_failures=2,
                last_error="temporary failure",
                failure_notified=True,
            )
        },
    )

    transition = observe_explicit_zero(failed, key)

    assert transition.state.snapshots[key] == snapshot
    assert transition.state.empty_candidates[key] == EmptyCandidate(consecutive_observations=1)
    assert key not in transition.state.health
    assert transition.events == (
        HealthEvent(
            kind=HealthEventKind.RECOVERY,
            key=key,
            count=2,
            reason=None,
        ),
    )


def test_second_zero_for_new_category_creates_empty_baseline_silently() -> None:
    key = state_key("new-monitor", "New category")

    first = observe_explicit_zero(StateDocument(), key)
    second = observe_explicit_zero(first.state, key)

    assert key not in second.state.empty_candidates
    assert second.state.snapshots[key] == Snapshot(
        monitor_id="new-monitor", category="New category", products=()
    )
    assert first.events == ()
    assert second.events == ()


def test_second_zero_removes_old_products_in_product_key_order_only_once() -> None:
    key = state_key("demo", "GPT Plus")
    original = make_state(
        snapshots={
            key: make_snapshot(
                make_product("zeta", name="Zeta"),
                make_product("alpha", name="Alpha"),
            )
        }
    )

    first = observe_explicit_zero(original, key)
    second = observe_explicit_zero(first.state, key)
    third = observe_explicit_zero(second.state, key)

    assert second.state.snapshots[key].products == ()
    assert second.events == (
        Change(
            kind=ChangeKind.REMOVED,
            product_key="alpha",
            product_name="Alpha",
            url="https://pay.ldxp.cn/goods/alpha",
        ),
        Change(
            kind=ChangeKind.REMOVED,
            product_key="zeta",
            product_name="Zeta",
            url="https://pay.ldxp.cn/goods/zeta",
        ),
    )
    assert third.events == ()
    assert key not in third.state.empty_candidates


def test_failure_between_zero_observations_restarts_confirmation() -> None:
    key = state_key("demo", "GPT Plus")
    snapshot = make_snapshot(make_product())

    first = observe_explicit_zero(make_state(snapshots={key: snapshot}), key)
    failed = record_failure(first.state, key, "interrupted")
    restarted = observe_explicit_zero(failed.state, key)

    assert key not in failed.state.empty_candidates
    assert restarted.state.snapshots[key] == snapshot
    assert restarted.state.empty_candidates[key] == EmptyCandidate(consecutive_observations=1)
    assert restarted.events == (
        HealthEvent(
            kind=HealthEventKind.RECOVERY,
            key=key,
            count=1,
            reason=None,
        ),
    )


@pytest.mark.parametrize(
    "invalid_key",
    ["missing-delimiter", "\x1fcategory", "monitor\x1f", "a\x1fb\x1fc"],
)
@pytest.mark.parametrize("operation", [record_success, observe_explicit_zero])
def test_operations_reject_invalid_keys_with_fixed_safe_error(
    invalid_key: str, operation: object
) -> None:
    with pytest.raises(ValueError, match=r"^invalid state key$") as caught:
        operation(StateDocument(), invalid_key)  # type: ignore[operator]

    assert invalid_key not in str(caught.value)


@pytest.mark.parametrize(
    "invalid_key",
    ["missing-delimiter", "\x1fcategory", "monitor\x1f", "a\x1fb\x1fc"],
)
def test_record_failure_rejects_invalid_key_with_fixed_safe_error(invalid_key: str) -> None:
    with pytest.raises(ValueError, match=r"^invalid state key$") as caught:
        record_failure(StateDocument(), invalid_key, "safe reason")

    assert invalid_key not in str(caught.value)


def test_functions_fully_revalidate_rebuilt_state() -> None:
    key = state_key("demo", "GPT Plus")
    state = StateDocument()
    object.__setattr__(state, "next_event_sequence", 0)

    with pytest.raises(ValidationError):
        record_failure(state, key, "failure")
    with pytest.raises(ValidationError):
        record_success(state, key)
    with pytest.raises(ValidationError):
        observe_explicit_zero(state, key)
