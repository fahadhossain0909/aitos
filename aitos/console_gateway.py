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
from dataclasses import dataclass, field
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
    """Hash-only, bounded, absolute-expiry sessions; restart revokes everything."""

    def __init__(self, config: GatewayConfig, password: str, *, clock=time.monotonic):
        if not is_valid_operator_password(password):
            raise ValueError("operator password must be 24–1024 UTF-8 bytes")
        self.config = config
        self.clock = clock
        self.salt = secrets.token_bytes(16)
        self.digest = self._derive(password)
        self.sessions: dict[bytes, float] = {}
        self.attempts: deque[float] = deque()
        self.hash_lock = asyncio.Lock()

    def _derive(self, password):
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=self.salt,
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
        self.sessions = {key: end for key, end in self.sessions.items() if end > now}
        while self.attempts and self.attempts[0] <= now - 60:
            self.attempts.popleft()

    def remaining(self, cookie):
        self.prune()
        return max(0, int(self.sessions.get(self._key(cookie), 0) - self.clock()))

    def revoke(self, cookie):
        self.sessions.pop(self._key(cookie), None)

    def issue(self):
        self.prune()
        if len(self.sessions) >= self.config.max_sessions:
            del self.sessions[min(self.sessions, key=self.sessions.get)]
        token = secrets.token_urlsafe(32)
        self.sessions[self._key(token)] = self.clock() + self.config.session_seconds
        return token

    def admit_attempt(self):
        self.prune()
        if len(self.attempts) >= 10 or self.hash_lock.locked():
            return False
        self.attempts.append(self.clock())
        return True

    async def verify(self, password):
        async with self.hash_lock:
            derived = await asyncio.to_thread(self._derive, password)
            return hmac.compare_digest(derived, self.digest)


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
    "version": 1,
    "activeWorkspace": "trading",
    "panels": {
        "trading": ["watchlist", "charts", "orderflow", "execution", "copilot"],
        "ai": ["copilot", "conversations", "datasets"],
        "quant": ["experiments", "regimes", "datasets", "copilot"],
        "developer": ["strategy", "terminal", "audit"],
        "data": ["datasets", "dataquality", "watchlist"],
        "risk": ["exposure", "audit", "execution"],
    },
}


def validate_workspace_layout(value):
    """Validate a bounded, non-executable desktop preference document."""
    if not isinstance(value, dict) or set(value) != {
        "version",
        "activeWorkspace",
        "panels",
    }:
        raise ValueError("workspace_layout_shape_invalid")
    if type(value["version"]) is not int or value["version"] != 1:
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
    return {
        "version": 1,
        "activeWorkspace": value["activeWorkspace"],
        "panels": panels,
    }


def workspace_layout_etag(layout):
    canonical = json.dumps(layout, sort_keys=True, separators=(",", ":"), allow_nan=False)
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
    config: GatewayConfig, *, password: str, clock=time.monotonic
) -> web.Application:
    state = OperatorSessions(config, password, clock=clock)
    secure = config.public_origin.startswith("https:")
    cookie_name = "__Host-aitos_operator" if secure else "aitos_local_operator"
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

    app = web.Application(middlewares=[boundary], client_max_size=4096)
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
        if not state.remaining(request.cookies.get(cookie_name)):
            raise web.HTTPUnauthorized(text="Operator session required")

    def workspace_layout_path():
        return config.workspace_file

    def read_workspace_layout():
        path = workspace_layout_path()
        prepare_durable_directory(path.parent, reserve_bytes=1024 * 1024)
        if path.is_symlink():
            raise StorageUnavailable("workspace layout symlink forbidden")
        if not path.exists():
            return DEFAULT_WORKSPACE_LAYOUT
        if path.stat().st_size > 4096:
            raise StorageUnavailable("workspace layout exceeds size limit")
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        return validate_workspace_layout(raw)

    async def get_workspace_layout(request):
        require_operator_session(request)
        try:
            layout = await asyncio.to_thread(read_workspace_layout)
        except (OSError, ValueError, StorageUnavailable):
            return web.json_response(
                {"error": "Workspace preferences unavailable"},
                status=503,
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response(
            layout,
            headers={"Cache-Control": "no-store", "ETag": workspace_layout_etag(layout)},
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
                current = read_workspace_layout()
                current_etag = workspace_layout_etag(current)
                if not hmac.compare_digest(expected_etag, current_etag):
                    return current, current_etag, False
                atomic_write_json(path, layout)
                return layout, workspace_layout_etag(layout), True

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
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        try:
            body = await request.json()
            if (
                not isinstance(body, dict)
                or set(body) != {"password"}
                or not isinstance(body["password"], str)
                or not 1 <= len(body["password"].encode("utf-8")) <= 1024
            ):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text="Invalid operator sign-in") from None
        if not await state.verify(body["password"]):
            logger.info("operator sign-in rejected")
            raise web.HTTPUnauthorized(text="Operator sign-in rejected")
        state.revoke(request.cookies.get(cookie_name))
        token = state.issue()
        response = web.json_response(
            {"authenticated": True, "expires_in_seconds": config.session_seconds}
        )
        response.set_cookie(
            cookie_name,
            token,
            path="/",
            httponly=True,
            secure=secure,
            samesite="Strict",
            max_age=config.session_seconds,
        )
        logger.info("operator sign-in accepted")
        return response

    async def logout(request):
        state.revoke(request.cookies.get(cookie_name))
        logger.info("operator session revoked")
        return clear_cookie(web.Response(status=204))

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
    args = parser.parse_args()
    try:
        config = GatewayConfig(
            args.origin, args.upstream, args.dist, os.environ.get("AITOS_CONSOLE_TOKEN")
        )
        app = create_gateway(
            config, password=os.environ.get("AITOS_OPERATOR_PASSWORD", "")
        )
    except ValueError:
        parser.exit(
            2,
            "Gateway configuration rejected. Check origins, built bundle and operator credential settings.\n",
        )
    web.run_app(app, host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
