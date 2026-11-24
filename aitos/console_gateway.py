"""Local-first authenticated, strictly read-only built-console gateway.

No exchange credentials, trading mutations, deployment or runtime launch routes.
Use HTTPS termination for non-loopback origins. This is a single-process operator
boundary, not an OS vault, multi-user identity system or security certification.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, TCPConnector, web
from yarl import URL

from aitos.config.durable_files import (
    StorageUnavailable,
    atomic_write_json,
    exclusive_file_lock,
    prepare_durable_directory,
)
from aitos.config.paths import get_config_dir
from aitos.logging_setup import get_logger

logger = get_logger("aitos.console_gateway")
READ_QUERIES = {
    "/console/v1/history/cursor": frozenset({"limit", "cursor"}),
    "/console/v1/protection": frozenset(),
    "/console/v1/account": frozenset(),
    "/console/v1/account/timeline": frozenset({"limit", "cursor"}),
    "/console/v1/capabilities": frozenset(),
    "/console/v1/snapshot": frozenset(),
    "/console/v1/performance": frozenset(),
    "/console/v1/history": frozenset({"limit", "offset", "symbol"}),
    "/console/v1/research/plan": frozenset(
        {
            "start",
            "end",
            "train_days",
            "test_days",
            "holdout_days",
            "label_horizon_days",
            "embargo_days",
            "dataset_sha256",
        }
    ),
}
READ_ROUTES = frozenset(READ_QUERIES)
ROOT_ASSETS = frozenset(
    {
        "index.html",
        "icon.svg",
        "icon-192.png",
        "icon-512.png",
        "manifest.webmanifest",
        "sw.js",
    }
)
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; img-src 'self' data:; connect-src 'self'; manifest-src 'self'; "
    "worker-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
)


def normalize_origin(value: str) -> str:
    try:
        if (
            not isinstance(value, str)
            or value != value.strip()
            or re.search(r"[\s\\]", value)
        ):
            raise ValueError()
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError()
        host = parsed.hostname.lower()
        if ":" in host:
            if "%" in host:
                raise ValueError()
            ipaddress.IPv6Address(host)
        elif not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
            host,
        ):
            raise ValueError()
        if len(host) > 253:
            raise ValueError()
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        host = f"[{host}]" if ":" in host else host
        netloc = (
            host
            if port in (None, 80 if parsed.scheme == "http" else 443)
            else f"{host}:{port}"
        )
        return urlunsplit((parsed.scheme, netloc, "", "", ""))
    except (ValueError, TypeError):
        raise ValueError(
            "expected a canonical HTTP(S) origin without credentials or path"
        ) from None


def _loopback(origin: str) -> bool:
    host = urlsplit(origin).hostname
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_valid_operator_password(password: object) -> bool:
    """Apply the same UTF-8 byte contract in native setup and gateway startup."""
    if not isinstance(password, str):
        return False
    try:
        size = len(password.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return 24 <= size <= 1024


@dataclass(frozen=True)
class GatewayConfig:
    public_origin: str
    upstream_origin: str
    dist_dir: Path
    upstream_token: str | None = field(default=None, repr=False)
    session_seconds: int = 900
    max_sessions: int = 4
    workspace_file: Path | None = None

    def __post_init__(self):
        for key in ("public_origin", "upstream_origin"):
            origin = normalize_origin(getattr(self, key))
            if origin.startswith("http:") and not _loopback(origin):
                raise ValueError("non-loopback gateway origins require HTTPS")
            object.__setattr__(self, key, origin)
        if self.public_origin == self.upstream_origin:
            raise ValueError("gateway cannot proxy to itself")
        if self.upstream_token is not None and (
            len(self.upstream_token) < 32
            or "\r" in self.upstream_token
            or "\n" in self.upstream_token
        ):
            raise ValueError(
                "upstream bearer must be at least 32 characters without line breaks"
            )
        if (
            isinstance(self.session_seconds, bool)
            or not isinstance(self.session_seconds, int)
            or not 60 <= self.session_seconds <= 3600
        ):
            raise ValueError("session lifetime must be 60–3600 seconds")
        if (
            isinstance(self.max_sessions, bool)
            or not isinstance(self.max_sessions, int)
            or not 1 <= self.max_sessions <= 16
        ):
            raise ValueError("bounded session inventory required")
        root = Path(self.dist_dir).resolve()
        if not root.is_dir() or not (root / "index.html").is_file():
            raise ValueError("build the desktop bundle before starting the gateway")
        object.__setattr__(self, "dist_dir", root)
        workspace_file = self.workspace_file
        if workspace_file is None:
            workspace_file = get_config_dir() / "desktop" / "workspace-layout.json"
        object.__setattr__(
            self, "workspace_file", Path(workspace_file).expanduser().resolve()
        )


class OperatorSessions:
    """Hash-only, bounded sessions and MFA pre-authentication with stable subjects."""

    def __init__(
        self,
        config: GatewayConfig,
        password: str | None,
        *,
        default_user: str,
        identities: dict[str, str] | None = None,
        clock=time.monotonic,
    ):
        self.config = config
        self.clock = clock
        self.default_user = default_user
        source = identities or {default_user: password}
        if (
            not isinstance(source, dict)
            or not source
            or len(source) > 32
            or default_user not in source
        ):
            raise ValueError("bounded operator identity directory is required")
        self.credentials: dict[str, tuple[bytes, bytes]] = {}
        for user_id, secret in source.items():
            if (
                not isinstance(user_id, str)
                or not user_id.strip()
                or user_id != user_id.strip()
                or len(user_id) > 256
                or not is_valid_operator_password(secret)
            ):
                raise ValueError("each operator requires a bounded identity and password")
            salt = secrets.token_bytes(16)
            self.credentials[user_id] = (salt, self._derive(secret, salt))
        self.sessions: dict[bytes, tuple[float, str]] = {}
        self.pending: dict[bytes, tuple[float, str]] = {}
        self.attempts: deque[float] = deque()
        self.hash_lock = asyncio.Lock()

    @staticmethod
    def _derive(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=16384,
            r=8,
            p=1,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )

    def _key(self, value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
            return None
        return hashlib.sha256(value.encode("ascii")).digest()

    def prune(self):
        now = self.clock()
        self.sessions = {
            key: record for key, record in self.sessions.items() if record[0] > now
        }
        self.pending = {
            key: record for key, record in self.pending.items() if record[0] > now
        }
        while self.attempts and self.attempts[0] <= now - 60:
            self.attempts.popleft()

    def remaining(self, cookie):
        self.prune()
        record = self.sessions.get(self._key(cookie))
        return max(0, int(record[0] - self.clock())) if record else 0

    def identity(self, cookie):
        self.prune()
        record = self.sessions.get(self._key(cookie))
        return record[1] if record else None

    def revoke(self, cookie):
        self.sessions.pop(self._key(cookie), None)

    def issue_pending(self, token: str, user_id: str) -> None:
        self.prune()
        if len(self.pending) >= self.config.max_sessions:
            del self.pending[min(self.pending, key=lambda key: self.pending[key][0])]
        self.pending[self._key(token)] = (self.clock() + 180, user_id)

    def pending_identity(self, token: str | None) -> str | None:
        self.prune()
        key = self._key(token)
        record = self.pending.get(key) if key else None
        return record[1] if record else None

    def has_pending(self, token: str | None) -> bool:
        return self.pending_identity(token) is not None

    def revoke_pending(self, token: str | None) -> None:
        self.pending.pop(self._key(token), None)

    def issue(self, user_id: str | None = None):
        user = user_id or self.default_user
        if user not in self.credentials:
            raise ValueError("unknown operator identity")
        self.prune()
        if len(self.sessions) >= self.config.max_sessions:
            del self.sessions[min(self.sessions, key=lambda key: self.sessions[key][0])]
        token = secrets.token_urlsafe(32)
        self.sessions[self._key(token)] = (self.clock() + self.config.session_seconds, user)
        return token

    def admit_attempt(self):
        self.prune()
        if len(self.attempts) >= 10 or self.hash_lock.locked():
            return False
        self.attempts.append(self.clock())
        return True

    async def verify(self, password: str, user_id: str) -> bool:
        record = self.credentials.get(user_id) if isinstance(user_id, str) else None
        if record is None:
            # Perform a dummy KDF for unknown identities to reduce timing leakage.
            salt, expected = next(iter(self.credentials.values()))
        else:
            salt, expected = record
        async with self.hash_lock:
            derived = await asyncio.to_thread(self._derive, password, salt)
            return bool(record) and hmac.compare_digest(derived, expected)


WORKSPACE_IDS = frozenset({"trading", "ai", "quant", "developer", "data", "risk"})
WORKSPACE_PANELS = frozenset(
    {
        "watchlist",
        "charts",
        "orderflow",
        "execution",
        "copilot",
        "conversations",
        "experiments",
        "regimes",
        "strategy",
        "terminal",
        "datasets",
        "dataquality",
        "exposure",
        "audit",
    }
)
DEFAULT_WORKSPACE_LAYOUT = {
    "version": 2,
    "activeWorkspace": "trading",
    "panels": {
        "trading": ["watchlist", "charts", "orderflow", "execution", "copilot"],
        "ai": ["copilot", "conversations", "datasets"],
        "quant": ["experiments", "regimes", "datasets", "copilot"],
        "developer": ["strategy", "terminal", "audit"],
        "data": ["datasets", "dataquality", "watchlist"],
        "risk": ["exposure", "audit", "execution"],
    },
    "panelGeometry": {
        "trading": {},
        "ai": {},
        "quant": {},
        "developer": {},
        "data": {},
        "risk": {},
    },
}


def validate_workspace_layout(value):
    """Validate bounded workspace visibility and dock geometry preferences."""
    required = {"version", "activeWorkspace", "panels"}
    legacy = isinstance(value, dict) and set(value) == required
    current = isinstance(value, dict) and set(value) == required | {"panelGeometry"}
    if not (legacy or current):
        raise ValueError("workspace_layout_shape_invalid")
    version = value["version"]
    if (
        type(version) is not int
        or (legacy and version != 1)
        or (current and version != 2)
    ):
        raise ValueError("workspace_layout_version_invalid")
    if (
        not isinstance(value["activeWorkspace"], str)
        or value["activeWorkspace"] not in WORKSPACE_IDS
    ):
        raise ValueError("workspace_active_id_invalid")
    raw_panels = value["panels"]
    if not isinstance(raw_panels, dict) or set(raw_panels) != WORKSPACE_IDS:
        raise ValueError("workspace_panels_invalid")
    panels = {}
    for workspace_id, items in raw_panels.items():
        if not isinstance(items, list) or len(items) > 24:
            raise ValueError("workspace_panel_count_invalid")
        if any(
            not isinstance(item, str) or item not in WORKSPACE_PANELS for item in items
        ):
            raise ValueError("workspace_panel_id_invalid")
        if len(items) != len(set(items)):
            raise ValueError("workspace_panel_duplicate")
        panels[workspace_id] = list(items)

    raw_geometry = value.get(
        "panelGeometry", {workspace_id: {} for workspace_id in WORKSPACE_IDS}
    )
    if not isinstance(raw_geometry, dict) or set(raw_geometry) != WORKSPACE_IDS:
        raise ValueError("workspace_geometry_invalid")
    geometry = {}
    for workspace_id, entries in raw_geometry.items():
        if not isinstance(entries, dict) or len(entries) > 24:
            raise ValueError("workspace_geometry_invalid")
        if not set(entries).issubset(set(panels[workspace_id])):
            raise ValueError("workspace_geometry_panel_invalid")
        normalized = {}
        for panel_id, dimensions in entries.items():
            if not isinstance(dimensions, dict) or set(dimensions) != {
                "width",
                "height",
            }:
                raise ValueError("workspace_geometry_shape_invalid")
            width, height = dimensions["width"], dimensions["height"]
            if type(width) is not int or width not in {1, 2}:
                raise ValueError("workspace_geometry_width_invalid")
            if type(height) is not int or height not in {1, 2}:
                raise ValueError("workspace_geometry_height_invalid")
            normalized[panel_id] = {"width": width, "height": height}
        geometry[workspace_id] = normalized
    return {
        "version": 2,
        "activeWorkspace": value["activeWorkspace"],
        "panels": panels,
        "panelGeometry": geometry,
    }


def workspace_layout_etag(layout, revision):
    canonical = json.dumps(
        {"layout": layout, "revision": revision},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return '"' + hashlib.sha256(canonical.encode("utf-8")).hexdigest() + '"'


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_field")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError(f"nonfinite JSON constant: {value}")


GATEWAY_STATE = web.AppKey("operator_sessions", OperatorSessions)


def create_gateway(
    config: GatewayConfig,
    *,
    password: str | None,
    clock=time.monotonic,
    webauthn_rp=None,
    approval_control_plane=None,
    operator_id: str = "local-operator",
    identities: dict[str, str] | None = None,
    webauthn_enrollment_token: str | None = None,
) -> web.Application:
    if (
        not isinstance(operator_id, str)
        or not operator_id.strip()
        or len(operator_id) > 256
    ):
        raise ValueError("bounded operator identity is required")
    if webauthn_rp is None and not _loopback(config.public_origin):
        raise ValueError("WebAuthn MFA is mandatory for non-loopback operator gateways")
    if webauthn_rp is not None:
        host = urlsplit(config.public_origin).hostname or ""
        rp_id = getattr(webauthn_rp, "rp_id", "")
        origins = frozenset(getattr(webauthn_rp, "origins", ()))
        if (
            not isinstance(rp_id, str)
            or not rp_id
            or not (host == rp_id or host.endswith("." + rp_id))
            or origins != frozenset({config.public_origin})
        ):
            raise ValueError("WebAuthn RP ID and origin must match the gateway origin")
    if webauthn_enrollment_token is not None and len(webauthn_enrollment_token) < 32:
        raise ValueError("WebAuthn enrollment token must contain at least 256 bits")
    state = OperatorSessions(
        config, password, default_user=operator_id, identities=identities, clock=clock
    )
    secure = config.public_origin.startswith("https:")
    cookie_name = "__Host-aitos_operator" if secure else "aitos_local_operator"
    pending_cookie = "__Host-aitos_operator_pending" if secure else "aitos_local_operator_pending"
    upstream: ClientSession | None = None
    upstream_slots = asyncio.Semaphore(4)

    @web.middleware
    async def boundary(request, handler):
        try:
            try:
                actual = normalize_origin(
                    f"{urlsplit(config.public_origin).scheme}://{request.host}"
                )
            except ValueError:
                raise web.HTTPForbidden(text="Invalid operator host") from None
            if actual != config.public_origin:
                raise web.HTTPForbidden(text="Invalid operator host")
            if request.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
                raise web.HTTPForbidden(text="Cross-origin operator access denied")
            origin = request.headers.get("Origin")
            if origin is not None:
                try:
                    allowed = normalize_origin(origin) == config.public_origin
                except ValueError:
                    allowed = False
                if not allowed:
                    raise web.HTTPForbidden(text="Cross-origin operator access denied")
            if request.method not in {"GET", "HEAD"} and origin is None:
                raise web.HTTPForbidden(text="Operator origin required")
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({"error": exc.reason}, status=exc.status)
        except Exception:
            logger.warning("operator gateway request failed")
            response = web.json_response(
                {"error": "Operator gateway unavailable"}, status=503
            )
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": CSP,
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Vary": "Cookie, Origin",
            }
        )
        if secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    app = web.Application(middlewares=[boundary], client_max_size=96 * 1024)
    app[GATEWAY_STATE] = state

    async def transport(app):
        nonlocal upstream
        async with ClientSession(
            timeout=ClientTimeout(total=6, connect=2, sock_read=5),
            connector=TCPConnector(limit=4),
            trust_env=False,
            cookie_jar=DummyCookieJar(),
            auto_decompress=False,
        ) as session:
            upstream = session
            yield
        state.sessions.clear()

    app.cleanup_ctx.append(transport)

    def set_pending_cookie(response, token):
        response.set_cookie(
            pending_cookie,
            token,
            path="/",
            httponly=True,
            secure=secure,
            samesite="Strict",
            max_age=180,
        )
        return response

    def clear_pending_cookie(response):
        response.set_cookie(
            pending_cookie,
            "",
            max_age=0,
            expires="Thu, 01 Jan 1970 00:00:00 GMT",
            path="/",
            secure=secure,
            httponly=True,
            samesite="Strict",
        )
        return response

    def require_pending(request):
        token = request.cookies.get(pending_cookie)
        user_id = state.pending_identity(token)
        if user_id is None:
            raise web.HTTPUnauthorized(text="MFA pre-authentication expired")
        return user_id

    def issue_operator_session(response, request, user_id):
        state.revoke(request.cookies.get(cookie_name))
        state.revoke_pending(request.cookies.get(pending_cookie))
        token = state.issue(user_id)
        response.set_cookie(
            cookie_name,
            token,
            path="/",
            httponly=True,
            secure=secure,
            samesite="Strict",
            max_age=config.session_seconds,
        )
        clear_pending_cookie(response)
        return response

    def clear_cookie(response):
        # set_cookie supports these flags across the project's aiohttp>=3.10
        # range; del_cookie gained some keyword flags only in newer releases.
        response.set_cookie(
            cookie_name,
            "",
            max_age=0,
            expires="Thu, 01 Jan 1970 00:00:00 GMT",
            path="/",
            secure=secure,
            httponly=True,
            samesite="Strict",
        )
        return response

    def require_operator_session(request):
        user_id = state.identity(request.cookies.get(cookie_name))
        if user_id is None:
            raise web.HTTPUnauthorized(text="Operator session required")
        return user_id

    async def read_json_body(request, *, max_bytes: int):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        raw = await request.read()
        if len(raw) > max_bytes:
            raise web.HTTPRequestEntityTooLarge(
                max_size=max_bytes, actual_size=len(raw)
            )
        try:
            return json.loads(
                raw,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text="Invalid JSON request") from None

    def workspace_layout_path():
        return config.workspace_file

    def read_workspace_document():
        path = workspace_layout_path()
        prepare_durable_directory(path.parent, reserve_bytes=1024 * 1024)
        if path.is_symlink():
            raise StorageUnavailable("workspace layout symlink forbidden")
        if not path.exists():
            return DEFAULT_WORKSPACE_LAYOUT, 0
        if path.stat().st_size > 4096:
            raise StorageUnavailable("workspace layout exceeds size limit")
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        # Read legacy v1 layout files once; subsequent saves atomically store
        # the layout and monotonic revision together to prevent ABA conflicts.
        if isinstance(raw, dict) and set(raw) == {"layout", "revision"}:
            revision = raw["revision"]
            if type(revision) is not int or not 1 <= revision < 2**63:
                raise StorageUnavailable("workspace revision invalid")
            return validate_workspace_layout(raw["layout"]), revision
        return validate_workspace_layout(raw), 0

    async def get_workspace_layout(request):
        require_operator_session(request)
        try:
            layout, revision = await asyncio.to_thread(read_workspace_document)
        except (OSError, ValueError, StorageUnavailable):
            return web.json_response(
                {"error": "Workspace preferences unavailable"},
                status=503,
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response(
            layout,
            headers={
                "Cache-Control": "no-store",
                "ETag": workspace_layout_etag(layout, revision),
            },
        )

    async def put_workspace_layout(request):
        require_operator_session(request)
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        expected_etag = request.headers.get("If-Match", "")
        if not re.fullmatch(r'"[0-9a-f]{64}"', expected_etag):
            raise web.HTTPPreconditionRequired(text="Workspace revision required")
        try:
            raw = await request.read()
            if len(raw) > 4096:
                raise ValueError("workspace layout exceeds size limit")
            value = json.loads(
                raw,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
            layout = validate_workspace_layout(value)
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text="Invalid workspace preferences") from None

        def persist():
            path = workspace_layout_path()
            prepare_durable_directory(path.parent, reserve_bytes=1024 * 1024)
            if path.is_symlink():
                raise StorageUnavailable("workspace layout symlink forbidden")
            # Compare-and-swap is serialized across all local desktop windows.
            with exclusive_file_lock(path.with_name(path.name + ".revision.lock")):
                current, revision = read_workspace_document()
                current_etag = workspace_layout_etag(current, revision)
                if not hmac.compare_digest(expected_etag, current_etag):
                    return current, current_etag, False
                if revision >= 2**63 - 1:
                    raise StorageUnavailable("workspace revision exhausted")
                next_revision = revision + 1
                atomic_write_json(path, {"layout": layout, "revision": next_revision})
                return layout, workspace_layout_etag(layout, next_revision), True

        try:
            saved_layout, saved_etag, did_save = await asyncio.to_thread(persist)
        except (OSError, ValueError, StorageUnavailable):
            return web.json_response(
                {"error": "Workspace preferences could not be saved"},
                status=503,
                headers={"Cache-Control": "no-store"},
            )
        if not did_save:
            return web.json_response(
                saved_layout,
                status=409,
                headers={"Cache-Control": "no-store", "ETag": saved_etag},
            )
        return web.json_response(
            saved_layout,
            headers={"Cache-Control": "no-store", "ETag": saved_etag},
        )

    async def session_status(request):
        remaining = state.remaining(request.cookies.get(cookie_name))
        response = web.json_response(
            {
                "schema_version": 1,
                "gateway": True,
                "authenticated": remaining > 0,
                "expires_in_seconds": remaining,
                "controls": "read_only",
                "identity": state.identity(request.cookies.get(cookie_name)),
                "default_identity": operator_id,
                "mfa": bool(webauthn_rp) if remaining else False,
            }
        )
        return (
            clear_cookie(response)
            if not remaining and cookie_name in request.cookies
            else response
        )

    async def login(request):
        if not state.admit_attempt():
            return web.json_response(
                {"error": "Login rate limit reached"},
                status=429,
                headers={"Retry-After": "60"},
            )
        try:
            body = await read_json_body(request, max_bytes=4096)
            if (
                not isinstance(body, dict)
                or "password" not in body
                or set(body)
                not in (
                    {"password"},
                    {"password", "enrollment_token"},
                    {"password", "user_id"},
                    {"password", "user_id", "enrollment_token"},
                )
                or not isinstance(body["password"], str)
                or not 1 <= len(body["password"].encode("utf-8")) <= 1024
                or (
                    "enrollment_token" in body
                    and not isinstance(body["enrollment_token"], str)
                )
                or (
                    "user_id" in body
                    and (
                        not isinstance(body["user_id"], str)
                        or not body["user_id"].strip()
                        or len(body["user_id"]) > 256
                    )
                )
            ):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text="Invalid operator sign-in") from None
        user_id = body.get("user_id", operator_id)
        if not await state.verify(body["password"], user_id):
            logger.info("operator sign-in rejected")
            raise web.HTTPUnauthorized(text="Operator sign-in rejected")
        state.revoke(request.cookies.get(cookie_name))
        if webauthn_rp is not None:
            try:
                credentials = webauthn_rp.credential_count(user_id=user_id)
                if not credentials and not _loopback(config.public_origin):
                    if not webauthn_enrollment_token or not (
                        webauthn_rp.consume_enrollment_bootstrap_token(
                            user_id=user_id,
                            configured_token=webauthn_enrollment_token,
                            supplied_token=body.get("enrollment_token", ""),
                            now=int(time.time()),
                        )
                    ):
                        raise web.HTTPForbidden(
                            text="First WebAuthn enrollment requires an unused out-of-band bootstrap token"
                        )
                pending = secrets.token_urlsafe(32)
                state.issue_pending(pending, user_id)
                if credentials:
                    options = webauthn_rp.authentication_options(
                        user_id=user_id, now=int(time.time())
                    )
                    response = web.json_response(
                        {"authenticated": False, "mfa_required": True, "options": options}
                    )
                else:
                    options = webauthn_rp.registration_options(
                        user_id=user_id, now=int(time.time())
                    )
                    response = web.json_response(
                        {
                            "authenticated": False,
                            "mfa_enrollment_required": True,
                            "options": options,
                        }
                    )
                return set_pending_cookie(response, pending)
            except web.HTTPException:
                raise
            except Exception as exc:
                logger.warning("operator MFA challenge could not be created: %s", type(exc).__name__)
                raise web.HTTPServiceUnavailable(text="WebAuthn MFA unavailable") from None
        response = web.json_response(
            {"authenticated": True, "expires_in_seconds": config.session_seconds}
        )
        logger.warning("password-only operator session issued; WebAuthn MFA is disabled")
        return issue_operator_session(response, request, user_id)

    async def finish_mfa_enrollment(request):
        if webauthn_rp is None:
            raise web.HTTPNotFound()
        user_id = require_pending(request)
        body = await read_json_body(request, max_bytes=96 * 1024)
        if not isinstance(body, dict) or set(body) != {"credential"}:
            raise web.HTTPBadRequest(text="Invalid WebAuthn enrollment")
        if not state.admit_attempt():
            raise web.HTTPTooManyRequests(
                headers={"Retry-After": "60"}, text="MFA enrollment rate limit reached"
            )
        try:
            webauthn_rp.verify_registration(
                user_id=user_id, credential=body["credential"], now=int(time.time())
            )
            options = webauthn_rp.authentication_options(
                user_id=user_id, now=int(time.time())
            )
            return web.json_response(
                {"authenticated": False, "mfa_required": True, "options": options}
            )
        except Exception as exc:
            logger.info("operator WebAuthn enrollment rejected: %s", type(exc).__name__)
            raise web.HTTPUnauthorized(text="WebAuthn enrollment rejected") from None

    async def finish_mfa_login(request):
        if webauthn_rp is None:
            raise web.HTTPNotFound()
        user_id = require_pending(request)
        if not state.admit_attempt():
            raise web.HTTPTooManyRequests(
                headers={"Retry-After": "60"}, text="MFA verification rate limit reached"
            )
        body = await read_json_body(request, max_bytes=96 * 1024)
        if (
            not isinstance(body, dict)
            or set(body) != {"challenge", "assertion"}
            or not isinstance(body["challenge"], str)
            or not isinstance(body["assertion"], dict)
        ):
            raise web.HTTPBadRequest(text="Invalid WebAuthn assertion")
        try:
            claims = webauthn_rp.verify_assertion(
                assertion=json.dumps(body["assertion"], separators=(",", ":")).encode(),
                challenge=body["challenge"],
                expected_origin=webauthn_rp.origins,
                expected_rp_id=webauthn_rp.rp_id,
                require_user_verification=True,
            )
            if claims.get("verified") is not True or claims.get("user_id") != user_id:
                raise PermissionError("WebAuthn identity mismatch")
        except Exception as exc:
            logger.info("operator WebAuthn assertion rejected: %s", type(exc).__name__)
            raise web.HTTPUnauthorized(text="WebAuthn assertion rejected") from None
        response = web.json_response(
            {
                "authenticated": True,
                "expires_in_seconds": config.session_seconds,
                "identity": user_id,
                "mfa": True,
                "controls": "read_only",
            }
        )
        logger.info("operator MFA sign-in accepted")
        return issue_operator_session(response, request, user_id)

    async def list_operator_credentials(request):
        user_id = require_operator_session(request)
        if webauthn_rp is None:
            raise web.HTTPServiceUnavailable(text="WebAuthn is not configured")
        return web.json_response(
            {"credentials": webauthn_rp.list_credentials(user_id=user_id)}
        )

    async def begin_add_authenticator(request):
        user_id = require_operator_session(request)
        if webauthn_rp is None:
            raise web.HTTPServiceUnavailable(text="WebAuthn is not configured")
        if not state.admit_attempt():
            raise web.HTTPTooManyRequests(
                headers={"Retry-After": "60"}, text="Authenticator enrollment rate limit reached"
            )
        options = webauthn_rp.authentication_options(
            user_id=user_id, now=int(time.time())
        )
        return web.json_response({"options": options})

    async def verify_add_authenticator_step_up(request):
        user_id = require_operator_session(request)
        if webauthn_rp is None:
            raise web.HTTPServiceUnavailable(text="WebAuthn is not configured")
        if not state.admit_attempt():
            raise web.HTTPTooManyRequests(
                headers={"Retry-After": "60"}, text="Authenticator step-up rate limit reached"
            )
        body = await read_json_body(request, max_bytes=96 * 1024)
        if (
            not isinstance(body, dict)
            or set(body) != {"challenge", "assertion"}
            or not isinstance(body["challenge"], str)
            or not isinstance(body["assertion"], dict)
        ):
            raise web.HTTPBadRequest(text="Invalid WebAuthn step-up")
        try:
            claims = webauthn_rp.verify_assertion(
                assertion=json.dumps(body["assertion"], separators=(",", ":")).encode(),
                challenge=body["challenge"],
                expected_origin=webauthn_rp.origins,
                expected_rp_id=webauthn_rp.rp_id,
                require_user_verification=True,
            )
            if claims.get("verified") is not True or claims.get("user_id") != user_id:
                raise PermissionError("WebAuthn identity mismatch")
            options = webauthn_rp.registration_options(
                user_id=user_id, now=int(time.time())
            )
        except Exception as exc:
            logger.info("authenticator enrollment step-up rejected: %s", type(exc).__name__)
            raise web.HTTPUnauthorized(text="WebAuthn step-up rejected") from None
        return web.json_response({"options": options})

    async def finish_add_authenticator(request):
        user_id = require_operator_session(request)
        if webauthn_rp is None:
            raise web.HTTPServiceUnavailable(text="WebAuthn is not configured")
        body = await read_json_body(request, max_bytes=96 * 1024)
        if not isinstance(body, dict) or set(body) != {"credential"}:
            raise web.HTTPBadRequest(text="Invalid WebAuthn registration")
        try:
            credential_id = webauthn_rp.verify_registration(
                user_id=user_id,
                credential=body["credential"],
                now=int(time.time()),
            )
        except Exception as exc:
            logger.info("additional authenticator registration rejected: %s", type(exc).__name__)
            raise web.HTTPUnauthorized(text="Authenticator registration rejected") from None
        return web.json_response(
            {"registered": True, "credential_id": credential_id}
        )

    async def approval_capabilities(request):
        user_id = require_operator_session(request)
        enabled = (
            approval_control_plane is not None
            and webauthn_rp is not None
            and approval_control_plane.role_authorizer(user_id, "live_trader")
        )
        return web.json_response(
            {
                "enabled": enabled,
                "requires_webauthn_step_up": enabled,
                "execution": "not_exposed_by_operator_gateway",
            }
        )

    async def create_approval_challenge(request):
        user_id = require_operator_session(request)
        if approval_control_plane is None or webauthn_rp is None:
            raise web.HTTPServiceUnavailable(text="Approval control plane is not configured")
        if not state.admit_attempt():
            raise web.HTTPTooManyRequests(
                headers={"Retry-After": "60"}, text="Approval request rate limit reached"
            )
        try:
            body = await read_json_body(request, max_bytes=4096)
            if not isinstance(body, dict) or set(body) != {"action"}:
                raise ValueError()
            action = body["action"]
            if not isinstance(action, dict) or not action:
                raise ValueError()
            options = webauthn_rp.authentication_options(
                user_id=user_id, now=int(time.time())
            )
            challenge = approval_control_plane.issue_challenge(
                user_id=user_id,
                action=action,
                webauthn_challenge=options["challenge"],
            )
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid approval action") from None
        except PermissionError:
            raise web.HTTPForbidden(text="Approval role is not authorized") from None
        except Exception as exc:
            logger.warning("approval challenge creation failed: %s", type(exc).__name__)
            raise web.HTTPServiceUnavailable(text="Approval challenge unavailable") from None
        return web.json_response(
            {
                "challenge_id": challenge.challenge_id,
                "action_digest": challenge.action_digest,
                "expires_at": challenge.expires_at,
                "options": options,
            }
        )

    async def approve_action(request):
        user_id = require_operator_session(request)
        if approval_control_plane is None or webauthn_rp is None:
            raise web.HTTPServiceUnavailable(text="Approval control plane is not configured")
        if not state.admit_attempt():
            raise web.HTTPTooManyRequests(
                headers={"Retry-After": "60"}, text="Approval verification rate limit reached"
            )
        try:
            body = await read_json_body(request, max_bytes=96 * 1024)
            if (
                not isinstance(body, dict)
                or set(body) != {"challenge_id", "assertion"}
                or not isinstance(body["challenge_id"], str)
                or not isinstance(body["assertion"], dict)
            ):
                raise ValueError()
            challenge = approval_control_plane.load_challenge(
                body["challenge_id"], user_id=user_id
            )
            token = approval_control_plane.approve(
                challenge,
                assertion=json.dumps(
                    body["assertion"], separators=(",", ":")
                ).encode(),
                verifier=webauthn_rp.verify_assertion,
            )
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid approval response") from None
        except PermissionError:
            raise web.HTTPForbidden(text="Approval verification rejected") from None
        except Exception as exc:
            logger.warning("approval verification failed: %s", type(exc).__name__)
            raise web.HTTPUnauthorized(text="Approval verification rejected") from None
        return web.json_response(asdict(token))

    async def logout(request):
        state.revoke(request.cookies.get(cookie_name))
        state.revoke_pending(request.cookies.get(pending_cookie))
        logger.info("operator session revoked")
        return clear_pending_cookie(clear_cookie(web.Response(status=204)))

    async def proxy(request):
        if request.path not in READ_ROUTES:
            raise web.HTTPNotFound()
        if request.method != "GET":
            raise web.HTTPMethodNotAllowed(request.method, ["GET"])
        if not state.remaining(request.cookies.get(cookie_name)):
            raise web.HTTPUnauthorized(text="Operator session required")
        if (
            len(request.query_string) > 2048
            or set(request.query) - READ_QUERIES[request.path]
            or any(len(request.query.getall(key)) != 1 for key in request.query)
        ):
            raise web.HTTPBadRequest()
        if upstream_slots.locked():
            raise web.HTTPTooManyRequests()
        url = (
            URL(config.upstream_origin)
            .with_path(request.path)
            .with_query(request.query)
        )
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if config.upstream_token:
            headers["Authorization"] = f"Bearer {config.upstream_token}"
        try:
            async with upstream_slots, upstream.get(
                url, headers=headers, allow_redirects=False
            ) as result:
                if result.status != 200:
                    status = result.status if result.status in {400, 429, 503} else 502
                    return web.json_response(
                        {"error": "Runtime evidence unavailable"}, status=status
                    )
                if (
                    result.content_type != "application/json"
                    or result.headers.get("Content-Encoding", "identity") != "identity"
                    or result.content_length is not None
                    and result.content_length > 2 * 1024 * 1024
                ):
                    raise ValueError()
                body = bytearray()
                async for chunk in result.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > 2 * 1024 * 1024:
                        raise ValueError()

                def unique_object(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("duplicate upstream JSON field")
                        result[key] = value
                    return result

                def reject_constant(value):
                    raise ValueError("nonfinite upstream JSON")

                payload = json.loads(
                    body,
                    object_pairs_hook=unique_object,
                    parse_constant=reject_constant,
                )
                if not isinstance(payload, dict):
                    raise ValueError()
                return web.json_response(payload)
        except Exception:
            logger.warning("operator runtime evidence request failed")
            return web.json_response(
                {"error": "Runtime evidence unavailable"}, status=502
            )

    async def static_file(request):
        path = request.match_info.get("path", "") or "index.html"
        if path not in ROOT_ASSETS and not (
            path.startswith("assets/")
            and len(Path(path).parts) == 2
            and Path(path).suffix.lower()
            in {
                ".js",
                ".mjs",
                ".css",
                ".woff",
                ".woff2",
                ".png",
                ".svg",
                ".jpg",
                ".jpeg",
                ".webp",
                ".ico",
                ".avif",
            }
        ):
            raise web.HTTPNotFound()
        candidate = (config.dist_dir / path).resolve()
        if (
            not candidate.is_relative_to(config.dist_dir)
            or not candidate.is_file()
            or any(part.startswith(".") for part in Path(path).parts)
        ):
            raise web.HTTPNotFound()
        return web.FileResponse(candidate)

    app.router.add_get("/operator/v1/session", session_status)
    app.router.add_get("/operator/v1/workspace-layout", get_workspace_layout)
    app.router.add_put("/operator/v1/workspace-layout", put_workspace_layout)
    app.router.add_post("/operator/v1/login", login)
    app.router.add_post("/operator/v1/mfa/register", finish_mfa_enrollment)
    app.router.add_post("/operator/v1/mfa/verify", finish_mfa_login)
    app.router.add_get("/operator/v1/mfa/credentials", list_operator_credentials)
    app.router.add_post("/operator/v1/mfa/add/options", begin_add_authenticator)
    app.router.add_post("/operator/v1/mfa/add/verify", verify_add_authenticator_step_up)
    app.router.add_post("/operator/v1/mfa/add/finish", finish_add_authenticator)
    app.router.add_get("/operator/v1/approval/capabilities", approval_capabilities)
    app.router.add_post("/operator/v1/approval/challenge", create_approval_challenge)
    app.router.add_post("/operator/v1/approval/approve", approve_action)
    app.router.add_post("/operator/v1/logout", logout)
    app.router.add_route("*", "/console/{path:.*}", proxy)
    app.router.add_get("/{path:.*}", static_file)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--origin",
        required=True,
        help="Exact browser-visible origin; HTTPS required outside loopback",
    )
    parser.add_argument("--upstream", default="http://127.0.0.1:8765")
    parser.add_argument("--dist", type=Path, default=Path("desktop/dist"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--webauthn-db", type=Path, default=os.environ.get("AITOS_WEBAUTHN_DB"))
    parser.add_argument("--operator-id", default=os.environ.get("AITOS_OPERATOR_ID", "local-operator"))
    args = parser.parse_args()
    try:
        config = GatewayConfig(
            args.origin, args.upstream, args.dist, os.environ.get("AITOS_CONSOLE_TOKEN")
        )
        if args.webauthn_db is None:
            parser.exit(
                2,
                "AITOS_WEBAUTHN_DB or --webauthn-db is required; production gateway refuses password-only startup.\n",
            )
        from aitos.security.webauthn_rp import WebAuthnRelyingParty

        rp_id = os.environ.get("AITOS_WEBAUTHN_RP_ID") or urlsplit(
            config.public_origin
        ).hostname
        webauthn_rp = WebAuthnRelyingParty(
            args.webauthn_db,
            rp_id=rp_id,
            rp_name="AITOS Operator Console",
            allowed_origins={config.public_origin},
        )
        approval_key = os.environ.get("AITOS_APPROVAL_SIGNING_KEY")
        role_map_raw = os.environ.get("AITOS_OPERATOR_ROLE_MAP")
        approval_control_plane = None
        if approval_key or role_map_raw:
            if not approval_key or not role_map_raw:
                parser.exit(
                    2,
                    "Approval requires both AITOS_APPROVAL_SIGNING_KEY and AITOS_OPERATOR_ROLE_MAP.\\n",
                )
            role_map = json.loads(role_map_raw)
            if not isinstance(role_map, dict) or any(
                not isinstance(user, str)
                or not isinstance(roles, list)
                or any(not isinstance(role, str) for role in roles)
                for user, roles in role_map.items()
            ):
                parser.exit(2, "AITOS_OPERATOR_ROLE_MAP must map identities to role arrays.\\n")
            from aitos.security.live_approval import LiveApprovalControlPlane

            approval_control_plane = LiveApprovalControlPlane(
                args.webauthn_db.with_name("live-approvals.sqlite"),
                signing_key=approval_key.encode("utf-8"),
                allowed_origins={config.public_origin},
                rp_id=rp_id,
                role_authorizer=lambda user, role: role in role_map.get(user, []),
            )
        identities_raw = os.environ.get("AITOS_OPERATOR_IDENTITIES")
        identities = json.loads(identities_raw) if identities_raw else None
        app = create_gateway(
            config,
            password=(
                None
                if identities is not None
                else os.environ.get("AITOS_OPERATOR_PASSWORD", "")
            ),
            identities=identities,
            webauthn_rp=webauthn_rp,
            approval_control_plane=approval_control_plane,
            operator_id=args.operator_id,
            webauthn_enrollment_token=os.environ.get(
                "AITOS_WEBAUTHN_ENROLLMENT_TOKEN"
            ),
        )
    except (ValueError, OSError):
        parser.exit(
            2,
            "Gateway configuration rejected. Check origins, built bundle, operator credential and WebAuthn settings.\n",
        )
    web.run_app(app, host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
