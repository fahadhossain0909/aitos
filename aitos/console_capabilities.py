"""Fixed read-only API contract. Availability is not health or authority."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from aitos.console_gateway import _loopback, normalize_origin

API_VERSION = "1.0"


@dataclass(frozen=True)
class RuntimeEndpoint:
    origin: str

    def __post_init__(self):
        origin = normalize_origin(self.origin)
        if origin.startswith("http:") and not _loopback(origin):
            raise ValueError(
                "remote runtime requires HTTPS or an explicit local tunnel"
            )
        object.__setattr__(self, "origin", origin)

    @property
    def token_slot(self):
        # Tokens belong to an exact origin, never just a reused port or account.
        return "console/endpoint/" + hashlib.sha256(self.origin.encode()).hexdigest()


def describe_capabilities(
    modules, state_store, projection_observer, account_observer=None, history_pages=None
):
    def entry(name, path, available):
        return dict(id=name, path=path, available=bool(available))

    return dict(
        schema_version=1,
        api_version=API_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(),
        controls="read_only",
        live_authorized=False,
        production_certified=False,
        capabilities=[
            entry("runtime.snapshot", "/console/v1/snapshot", modules or state_store),
            entry(
                "ledger.history",
                "/console/v1/history",
                callable(getattr(state_store, "query_closed_history", None)),
            ),
            entry(
                "ledger.performance_window",
                "/console/v1/performance",
                callable(getattr(state_store, "query_closed_performance", None)),
            ),
            entry("research.plan", "/console/v1/research/plan", True),
            entry("persistence.shadow", None, projection_observer is not None),
            entry("account.margin", None, account_observer is not None),
            entry("market.mark_price", None, account_observer is not None),
            entry(
                "account.equity_timeline",
                None,
                getattr(account_observer, "timeline", None) is not None,
            ),
            entry("ledger.consistent_cursor", None, history_pages is not None),
            # These must not be inferred from position rows, page existence or
            # a healthy HTTP listener. No provider-backed service is wired yet.
            *[
                entry(name, None, False)
                for name in (
                    "orders.protection",
                    "alerts.delivery",
                    "configuration.approval",
                    "runtime.control",
                )
            ],
        ],
        modules=[
            m.module_id
            for m in modules
            if isinstance(getattr(m, "module_id", None), str)
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", m.module_id)
        ],
        limitations=[
            "performance_is_bounded_window",
            "history_offset_is_not_consistent_snapshot",
            "availability_is_not_runtime_health",
        ],
    )
