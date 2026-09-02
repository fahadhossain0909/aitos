from aitos.trading.lifecycle_safety import is_idempotent_closed_position_error


def test_known_closed_position_race_is_idempotent() -> None:
    assert is_idempotent_closed_position_error(
        RuntimeError("trade has no remaining position to close")
    )


def test_unrelated_failure_is_not_suppressed() -> None:
    assert not is_idempotent_closed_position_error(RuntimeError("redis timeout"))


def test_other_closed_position_wording_is_supported() -> None:
    assert is_idempotent_closed_position_error(RuntimeError("position already closed"))
