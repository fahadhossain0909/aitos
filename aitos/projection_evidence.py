"""Public read-only projection observations, not financial/runtime certification."""

from datetime import datetime, timezone

STATUSES = {
    "unavailable",
    "running",
    "empty",
    "verified",
    "conflict",
    "fenced",
    "timeout",
    "failed",
    "cancelled",
}


def unavailable(reason="not_attached", status="unavailable", observed_at=None):
    return dict(
        schema_version=1,
        scope="local_projection_observation",
        available=False,
        status=status,
        reason=reason,
        observed_at=observed_at,
        counts=None,
        last_batch=None,
        source="unavailable",
        controls="read_only",
        live_authorized=False,
        production_certified=False,
    )


def public_projection(value, *, now=None):
    """Whitelist fields, validate conservation and freshness; fail closed."""
    now = now or datetime.now(timezone.utc)
    try:
        if (
            type(value) is not dict
            or value["schema_version"] != 1
            or type(value["schema_version"]) is not int
            or value["scope"] != "local_projection_observation"
            or value["controls"] != "read_only"
            or value["live_authorized"] is not False
            or value["production_certified"] is not False
        ):
            raise ValueError()
        status = value["status"]
        available = value["available"]
        if (
            type(status) is not str
            or status not in STATUSES
            or type(available) is not bool
        ):
            raise ValueError()
        stamp = value["observed_at"]
        if stamp is not None:
            if type(stamp) is not str or len(stamp) > 64 or "T" not in stamp:
                raise ValueError()
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                raise ValueError()
            age = (now - parsed).total_seconds()
            if age > 60 or age < -5:
                return unavailable("stale")
        if not available:
            if (
                value["counts"] is not None
                or value["last_batch"] is not None
                or value["source"] != "unavailable"
                or status in {"empty", "verified"}
            ):
                raise ValueError()
            reason = value["reason"]
            if status != "unavailable" and (
                stamp is None or reason != "operational_error"
            ):
                raise ValueError()
            if reason not in {
                "not_attached",
                "not_observed",
                "stale",
                "invalid_observation",
                "operational_error",
            }:
                raise ValueError()
            return unavailable(reason, status, stamp)
        if (
            status not in {"empty", "verified"}
            or stamp is None
            or value["source"] != "local_journal"
            or value["reason"] != "none"
        ):
            raise ValueError()
        counts = value["counts"]
        batch = value["last_batch"]
        if (
            type(counts) is not dict
            or set(counts) != {"total", "pending", "delivered"}
            or type(batch) is not dict
            or set(batch) != {"checked", "inserted", "acknowledged"}
        ):
            raise ValueError()
        for number in [*counts.values(), *batch.values()]:
            if type(number) is not int or not 0 <= number <= 2**53 - 1:
                raise ValueError()
        if (
            counts["total"] != counts["pending"] + counts["delivered"]
            or not 0 <= batch["inserted"] <= batch["checked"] <= 100
            or batch["acknowledged"] != batch["checked"]
            or counts["delivered"] < batch["acknowledged"]
        ):
            raise ValueError()
        if status == "empty" and (any(batch.values()) or counts["pending"] != 0):
            raise ValueError()
        if status == "verified" and batch["checked"] == 0:
            raise ValueError()
        return dict(
            schema_version=1,
            scope="local_projection_observation",
            available=True,
            status=status,
            reason="none",
            observed_at=stamp,
            counts={k: counts[k] for k in ("total", "pending", "delivered")},
            last_batch={k: batch[k] for k in ("checked", "inserted", "acknowledged")},
            source="local_journal",
            controls="read_only",
            live_authorized=False,
            production_certified=False,
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return unavailable("invalid_observation")
