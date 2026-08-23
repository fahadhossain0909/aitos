"""Dataset-level availability policy used by incremental ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .dataset_layout import DatasetLayout


@dataclass(frozen=True)
class DatasetAvailability:
    dataset: str
    partition: Path
    canonical_present: bool


class DatasetGate:
    """Prevent raw downloads when the requested canonical partition exists."""

    def __init__(self, layout: DatasetLayout):
        self.layout = layout

    def available(
        self, dataset: str, exchange: str, market: str, symbol: str, day: date
    ) -> DatasetAvailability:
        path = self._path(dataset, exchange, market, symbol, day)
        return DatasetAvailability(
            dataset, path, self.layout.is_complete_partition(path)
        )

    def should_download_raw(
        self, dataset: str, exchange: str, market: str, symbol: str, day: date
    ) -> bool:
        return not self.available(
            dataset, exchange, market, symbol, day
        ).canonical_present

    def _path(
        self, dataset: str, exchange: str, market: str, symbol: str, day: date
    ) -> Path:
        methods = {
            "trades": self.layout.trades,
            "prices": self.layout.prices,
            "funding": self.layout.funding,
            "open_interest": self.layout.open_interest,
            "liquidations": self.layout.liquidations,
            "orderbook_snapshots": self.layout.orderbook_snapshots,
            "orderbook_updates": self.layout.orderbook_updates,
        }
        try:
            return methods[dataset](exchange, market, symbol, day)
        except KeyError as exc:
            raise ValueError(f"Unknown canonical dataset: {dataset}") from exc
