from datetime import datetime, timezone

import pytest

from aitos.market_data.deep_orderbook import DeepOrderBookGap, DeepOrderBookReplayer


class _Result:
    def __init__(self, rows):
        self.result_rows = rows


class _Client:
    def __init__(self, checkpoint_rows, delta_rows):
        self._checkpoint = _Result(checkpoint_rows)
        self._deltas = _Result(delta_rows)

    async def query(self, query, parameters=None):
        if "FROM deep_order_book_checkpoints" in query:
            return self._checkpoint
        return self._deltas


class _Repo:
    def __init__(self, client):
        self._client = client


def _replayer(deltas):
    target = datetime(2026, 9, 4, 0, 0, 2, tzinfo=timezone.utc)
    checkpoint = [
        (
            datetime(2026, 9, 4, 0, 0, 0, tzinfo=timezone.utc),
            100,
            "[[100.0, 1.0]]",
            "[[101.0, 2.0]]",
        )
    ]
    return DeepOrderBookReplayer(_Repo(_Client(checkpoint, deltas))), target


@pytest.mark.asyncio
async def test_replay_accepts_first_delta_that_bridges_checkpoint():
    replayer, target = _replayer(
        [
            (
                datetime(2026, 9, 4, 0, 0, 1, tzinfo=timezone.utc),
                99,
                101,
                98,
                "[[100.0, 3.0]]",
                "[]",
            ),
            (
                datetime(2026, 9, 4, 0, 0, 1, 500000, tzinfo=timezone.utc),
                102,
                103,
                101,
                "[]",
                "[[101.0, 0.0]]",
            ),
        ]
    )

    book = await replayer.reconstruct("BTCUSDT", target)

    assert book.update_id == 103
    assert book.bids[0].price == 100.0
    assert book.bids[0].quantity == 3.0
    assert not book.asks


@pytest.mark.asyncio
async def test_replay_fails_closed_on_subsequent_sequence_gap():
    replayer, target = _replayer(
        [
            (
                datetime(2026, 9, 4, 0, 0, 1, tzinfo=timezone.utc),
                100,
                101,
                100,
                "[]",
                "[]",
            ),
            (
                datetime(2026, 9, 4, 0, 0, 1, 500000, tzinfo=timezone.utc),
                102,
                103,
                999,
                "[]",
                "[]",
            ),
        ]
    )

    with pytest.raises(DeepOrderBookGap):
        await replayer.reconstruct("BTCUSDT", target)
