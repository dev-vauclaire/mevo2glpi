"""Tests for the shared cooperative wait primitive."""

from __future__ import annotations

import pytest

from glpi_ticketing_common.cooperative_wait import (
    wait_with_periodic_checkpoint,
)


def test_wait_is_split_and_checkpointed_without_real_sleep() -> None:
    waits: list[float] = []
    checkpoints: list[str] = []

    wait_with_periodic_checkpoint(
        25,
        checkpoint_interval_seconds=10,
        wait_once=waits.append,
        should_stop=lambda: False,
        checkpoint=lambda: checkpoints.append("pulse"),
    )

    assert waits == [10.0, 10.0, 5.0]
    assert checkpoints == ["pulse", "pulse", "pulse"]


def test_exact_decimal_multiple_does_not_add_a_near_zero_slice() -> None:
    waits: list[float] = []
    checkpoints: list[str] = []

    wait_with_periodic_checkpoint(
        1.0,
        checkpoint_interval_seconds=0.1,
        wait_once=waits.append,
        should_stop=lambda: False,
        checkpoint=lambda: checkpoints.append("pulse"),
    )

    assert len(waits) == 10
    assert waits == pytest.approx([0.1] * 10)
    assert checkpoints == ["pulse"] * 10


def test_stop_during_slice_prevents_checkpoint_and_remaining_wait() -> None:
    stop_requested = False
    waits: list[float] = []
    checkpoints: list[str] = []

    def wait_once(seconds: float) -> None:
        nonlocal stop_requested
        waits.append(seconds)
        stop_requested = True

    wait_with_periodic_checkpoint(
        25,
        checkpoint_interval_seconds=10,
        wait_once=wait_once,
        should_stop=lambda: stop_requested,
        checkpoint=lambda: checkpoints.append("pulse"),
    )

    assert waits == [10.0]
    assert checkpoints == []


def test_checkpoint_failure_is_propagated() -> None:
    expected_error = RuntimeError("synthetic ownership loss")

    def fail_checkpoint() -> None:
        raise expected_error

    with pytest.raises(RuntimeError) as captured_error:
        wait_with_periodic_checkpoint(
            10,
            checkpoint_interval_seconds=10,
            wait_once=lambda _seconds: None,
            should_stop=lambda: False,
            checkpoint=fail_checkpoint,
        )

    assert captured_error.value is expected_error


@pytest.mark.parametrize(
    ("overrides", "exception"),
    [
        ({"duration_seconds": True}, TypeError),
        ({"duration_seconds": -1}, ValueError),
        ({"duration_seconds": float("inf")}, ValueError),
        ({"checkpoint_interval_seconds": 0}, ValueError),
        ({"checkpoint_interval_seconds": float("nan")}, ValueError),
        ({"wait_once": None}, TypeError),
        ({"should_stop": None}, TypeError),
        ({"checkpoint": None}, TypeError),
    ],
)
def test_invalid_contract_is_rejected(
    overrides: dict[str, object],
    exception: type[Exception],
) -> None:
    arguments: dict[str, object] = {
        "duration_seconds": 1,
        "checkpoint_interval_seconds": 1,
        "wait_once": lambda _seconds: None,
        "should_stop": lambda: False,
        "checkpoint": lambda: None,
    }
    arguments.update(overrides)

    with pytest.raises(exception):
        wait_with_periodic_checkpoint(**arguments)  # type: ignore[arg-type]


def test_should_stop_must_return_boolean() -> None:
    def invalid_should_stop() -> object:
        return 0

    with pytest.raises(TypeError, match="return a boolean"):
        wait_with_periodic_checkpoint(
            1,
            checkpoint_interval_seconds=1,
            wait_once=lambda _seconds: None,
            should_stop=invalid_should_stop,  # type: ignore[arg-type]
            checkpoint=lambda: None,
        )
