"""Pure, fail-closed validation for remote order intents.

This module deliberately has no network, credential, exchange, or signing code.
It is a policy preflight component, not an authorization server or live signer.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from typing import Any, FrozenSet, Mapping


class IntentRejected(ValueError):
    """An order intent failed the local schema or configured policy."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IntentRejected("duplicate_json_key")
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise IntentRejected("non_finite_json_number")


def parse_order_intent_json(
    raw: str | bytes, *, max_bytes: int = 4096
) -> Mapping[str, Any]:
    """Parse a bounded UTF-8 JSON object and reject duplicate keys/constants."""
    if not isinstance(raw, (str, bytes)):
        raise IntentRejected("invalid_json_payload")
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= 65_536
    ):
        raise IntentRejected("invalid_body_limit")
    try:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(encoded) > max_bytes:
            raise IntentRejected("intent_body_too_large")
        decoded = encoded.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except IntentRejected:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise IntentRejected("invalid_json_payload") from None
    if not isinstance(value, dict):
        raise IntentRejected("intent_must_be_json_object")
    return value


_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]{0,29})(?:\.[0-9]{1,18})?\Z")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SYMBOL_RE = re.compile(r"[A-Z0-9]{2,30}\Z")
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{22,86}\Z")

_ALLOWED_FIELDS = frozenset(
    {
        "schema_version",
        "intent_id",
        "nonce",
        "issued_at_ms",
        "expires_at_ms",
        "account_alias",
        "symbol",
        "side",
        "order_type",
        "quantity",
        "limit_price",
        "reduce_only",
        "client_order_id",
        "strategy_id",
        "candidate_id",
        "decision_id",
        "reason_code",
    }
)
_REQUIRED_FIELDS = _ALLOWED_FIELDS - {
    "limit_price",
    "strategy_id",
    "candidate_id",
    "decision_id",
    "reason_code",
}


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise IntentRejected(f"invalid_{field}")
    return value


def _decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value):
        raise IntentRejected(f"invalid_{field}")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise IntentRejected(f"invalid_{field}") from None
    if not parsed.is_finite() or parsed <= 0:
        raise IntentRejected(f"invalid_{field}")
    return parsed


def _timestamp(value: Any, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > 9_223_372_036_854_775_807
    ):
        raise IntentRejected(f"invalid_{field}")
    return value


def _nonce_has_128_bits(value: Any) -> bool:
    if not isinstance(value, str) or not _NONCE_RE.fullmatch(value):
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error):
        return False
    return len(decoded) >= 16


@dataclass(frozen=True)
class OrderIntentPolicy:
    """Signer-local allowlist and per-order ceiling.

    Portfolio exposure, daily loss, positions, replay persistence, authenticated
    client identity and exchange metadata must be enforced by the signer around
    this pure validator. This object is not remotely mutable by design.
    """

    account_alias: str
    allowed_symbols: FrozenSet[str]
    max_order_notional: Decimal
    allowed_sides: FrozenSet[str] = frozenset({"BUY", "SELL"})
    allowed_order_types: FrozenSet[str] = frozenset({"LIMIT"})
    allow_market_orders: bool = False
    allow_reduce_only: bool = True
    max_intent_ttl_ms: int = 30_000
    clock_skew_ms: int = 5_000

    def __post_init__(self) -> None:
        if not isinstance(self.account_alias, str) or not _ID_RE.fullmatch(self.account_alias):
            raise ValueError("account_alias_required")
        if (
            not isinstance(self.allowed_symbols, frozenset)
            or not self.allowed_symbols
            or any(
                not isinstance(s, str) or not _SYMBOL_RE.fullmatch(s)
                for s in self.allowed_symbols
            )
        ):
            raise ValueError("valid_symbol_allowlist_required")
        if not isinstance(self.max_order_notional, Decimal) or (
            not self.max_order_notional.is_finite() or self.max_order_notional <= 0
        ):
            raise ValueError("positive_decimal_notional_required")
        if (
            not isinstance(self.allowed_sides, frozenset)
            or not self.allowed_sides
            or not self.allowed_sides <= {"BUY", "SELL"}
        ):
            raise ValueError("invalid_side_policy")
        if (
            not isinstance(self.allowed_order_types, frozenset)
            or not self.allowed_order_types
            or not self.allowed_order_types <= {"LIMIT", "MARKET"}
        ):
            raise ValueError("invalid_order_type_policy")
        if type(self.allow_market_orders) is not bool or type(self.allow_reduce_only) is not bool:
            raise ValueError("boolean_policy_flags_required")
        if "MARKET" in self.allowed_order_types and not self.allow_market_orders:
            raise ValueError("market_order_requires_explicit_enablement")
        if (
            isinstance(self.max_intent_ttl_ms, bool)
            or not isinstance(self.max_intent_ttl_ms, int)
            or not 1 <= self.max_intent_ttl_ms <= 30_000
        ):
            raise ValueError("invalid_intent_ttl")
        if (
            isinstance(self.clock_skew_ms, bool)
            or not isinstance(self.clock_skew_ms, int)
            or not 0 <= self.clock_skew_ms <= 60_000
        ):
            raise ValueError("invalid_clock_skew")


@dataclass(frozen=True)
class ValidatedOrderIntent:
    schema_version: int
    intent_id: str
    nonce: str
    issued_at_ms: int
    expires_at_ms: int
    account_alias: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal | None
    reduce_only: bool
    client_order_id: str
    notional: Decimal
    strategy_id: str | None = None
    candidate_id: str | None = None
    decision_id: str | None = None
    reason_code: str | None = None


def validate_order_intent(
    payload: Mapping[str, Any],
    *,
    policy: OrderIntentPolicy,
    now_ms: int,
    trusted_market_price: Decimal | None = None,
) -> ValidatedOrderIntent:
    """Validate a JSON-like intent against signer-local, per-order policy.

    For MARKET orders, trusted_market_price must be sourced by the signer from
    a fresh trusted feed; a price supplied by the remote caller is rejected.
    This function does not consume/persist the nonce, authenticate the caller,
    check position state, sign, or dispatch.
    """
    if not isinstance(payload, Mapping):
        raise IntentRejected("invalid_payload")
    if set(payload) - _ALLOWED_FIELDS:
        raise IntentRejected("unknown_fields")
    if _REQUIRED_FIELDS - set(payload):
        raise IntentRejected("missing_fields")

    version = payload["schema_version"]
    if type(version) is not int or version != 1:
        raise IntentRejected("unsupported_schema_version")

    intent_id = _identifier(payload["intent_id"], "intent_id")
    client_order_id = _identifier(payload["client_order_id"], "client_order_id")
    nonce = payload["nonce"]
    if not _nonce_has_128_bits(nonce):
        raise IntentRejected("invalid_nonce")

    issued = _timestamp(payload["issued_at_ms"], "issued_at")
    expires = _timestamp(payload["expires_at_ms"], "expires_at")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise IntentRejected("invalid_signer_clock")
    if issued > now_ms + policy.clock_skew_ms:
        raise IntentRejected("intent_from_future")
    if expires <= now_ms or expires <= issued:
        raise IntentRejected("intent_expired")
    if expires - issued > policy.max_intent_ttl_ms:
        raise IntentRejected("intent_ttl_exceeded")

    account_alias = payload["account_alias"]
    if account_alias != policy.account_alias:
        raise IntentRejected("account_not_allowed")

    symbol = payload["symbol"]
    if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
        raise IntentRejected("invalid_symbol")
    if symbol not in policy.allowed_symbols:
        raise IntentRejected("symbol_not_allowed")

    side = payload["side"]
    if not isinstance(side, str) or side not in policy.allowed_sides:
        raise IntentRejected("side_not_allowed")
    order_type = payload["order_type"]
    if not isinstance(order_type, str) or order_type not in policy.allowed_order_types:
        raise IntentRejected("order_type_not_allowed")

    quantity = _decimal(payload["quantity"], "quantity")
    reduce_only = payload["reduce_only"]
    if not isinstance(reduce_only, bool):
        raise IntentRejected("invalid_reduce_only")
    if reduce_only and not policy.allow_reduce_only:
        raise IntentRejected("reduce_only_not_allowed")

    limit_price = None
    if order_type == "LIMIT":
        limit_price = _decimal(payload.get("limit_price"), "limit_price")
        if trusted_market_price is not None and (
            not isinstance(trusted_market_price, Decimal)
            or not trusted_market_price.is_finite()
            or trusted_market_price <= 0
        ):
            raise IntentRejected("invalid_trusted_market_price")
        valuation_price = limit_price
    else:
        if "limit_price" in payload:
            raise IntentRejected("market_intent_must_not_supply_price")
        if not policy.allow_market_orders:
            raise IntentRejected("market_order_disabled")
        if not isinstance(trusted_market_price, Decimal) or (
            not trusted_market_price.is_finite() or trusted_market_price <= 0
        ):
            raise IntentRejected("trusted_market_price_required")
        valuation_price = trusted_market_price

    try:
        with localcontext() as context:
            context.prec = 100
            notional = quantity * valuation_price
    except DecimalException:
        raise IntentRejected("invalid_order_notional") from None
    if notional > policy.max_order_notional:
        raise IntentRejected("order_notional_exceeded")

    optional = {}
    for field in ("strategy_id", "candidate_id", "decision_id", "reason_code"):
        value = payload.get(field)
        if value is not None:
            optional[field] = _identifier(value, field)

    return ValidatedOrderIntent(
        schema_version=1,
        intent_id=intent_id,
        nonce=nonce,
        issued_at_ms=issued,
        expires_at_ms=expires,
        account_alias=account_alias,
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit_price,
        reduce_only=reduce_only,
        client_order_id=client_order_id,
        notional=notional,
        **optional,
    )
