"""FastAPI process for the agent service.

/health is the release puller's contract ({"ok": true, "sha": ...}, like the website).
The FastMCP app (stateless streamable HTTP, JSON responses) is mounted LAST because
a mount at "/" shadows every route declared after it. There is no module-level app:
uvicorn calls create_app() (factory mode, see __main__.py), so importing this module
never touches the database.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import config, db, hosted, oauth


class _ApiKeyShim:
    """X-API-Key → Authorization: Bearer, so FastMCP's auth middleware sees one header (Claude Desktop parity)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if b"authorization" not in headers and headers.get(b"x-api-key"):
                scope["headers"] = [*scope["headers"], (b"authorization", b"Bearer " + headers[b"x-api-key"])]
        await self.app(scope, receive, send)


class _HeadAsGet:
    """HEAD answers like GET, without the body (RFC 9110 §9.1), as Starlette's own routes do. FastAPI's GET routes do
    not take HEAD, so the "/" mount would answer it 404: `curl -I https://admiry.ai/` and the edge's HEAD redirects to
    the browser pages (spec 2026-09-29 §4) must land on the page. /mcp keeps its own answer."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "HEAD" or scope["path"].startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        async def headers_only(message):
            if message["type"] == "http.response.body":
                message = {**message, "body": b""}
            await send(message)

        await self.app({**scope, "method": "GET"}, receive, headers_only)


class _TenantHost:
    """H17: a request whose `Host` is a pages-domain hostname (`<slug>.<AGENT_PAGES_DOMAIN>`) is answered by
    `hosted.serve` alone -- the agent's own routers (`/mcp`, `/connect`, `/t/*`, the homepage, ...) never see
    it. A pure ASGI middleware, not BaseHTTPMiddleware, because the answer is `hosted.serve`'s own Starlette
    Response, sent directly rather than built from a FastAPI route or path-matched at all; the blocking work
    (opening a connection, the hostname lookup, rendering a template) runs in a thread (run_in_threadpool),
    the same discipline as track.py, since nothing here keeps it off the event loop for free the way a
    FastAPI path-operation function would. Inert -- always calls through -- when `settings.pages_domain` is
    unset (H1). Installed between `_OAuthRateLimit` and `_HeadAsGet` (see create_app) so a HEAD request has
    already become a GET, with its body-stripping `send` already in place, by the time this runs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = scope["app"].state.settings
        host = _host_header(scope)
        if not hosted.is_pages_host(host, settings.pages_domain):
            await self.app(scope, receive, send)
            return
        etags = _header(scope, b"if-none-match") or None  # a site bundle's file answers 304 to its own ETag (A3p-3)
        response = await run_in_threadpool(_serve_tenant, settings, host, scope["path"], scope["method"], etags)
        await response(scope, receive, send)


def _header(scope, name: bytes) -> str:
    return next((v.decode("latin-1") for k, v in scope["headers"] if k == name), "")


def _host_header(scope) -> str:
    return _header(scope, b"host")


def _serve_tenant(settings, host: str, path: str, method: str, if_none_match: str | None = None) -> Response:
    with db.connect(settings.database_url) as conn:
        return hosted.serve(conn, settings, host, path, method, if_none_match=if_none_match)


class _Static(StaticFiles):
    """The homepage's script and stylesheet (agent/static): revalidated on every load (ETag, so usually a 304),
    because the page links them without a version and a deploy must never pair a new page with an old script."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class _OAuthRateLimit(BaseHTTPMiddleware):
    """P8: 20 requests a minute per address on the OAuth endpoints. A BaseHTTPMiddleware on the FastAPI
    app (not a wrapper around mcp_app) so it covers both the SDK's mounted routes and /oauth/consent
    (a FastAPI route, Task 3) in one place."""

    PATHS = ("/authorize", "/token", "/register", "/revoke", oauth.CONSENT_PATH)

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.PATHS:
            ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or "local"
            with db.connect(request.app.state.settings.database_url) as conn:
                ok = oauth.hit(conn, ip, request.url.path)
                if not ok:
                    return Response(status_code=429, headers={"retry-after": "60"})
                if request.url.path == "/token" and request.method == "POST":
                    mismatch = await self._resource_mismatch(conn, request)
                    if mismatch:
                        return JSONResponse(
                            {"error": "invalid_grant", "error_description": mismatch},
                            status_code=400,
                            headers={"Cache-Control": "no-store"},
                        )
        return await call_next(request)

    @staticmethod
    async def _resource_mismatch(conn, request: Request) -> str | None:
        """RFC 8707: the SDK's TokenHandler never checks a code's stored `resource` (set at /authorize)
        against the one presented at /token (see oauth.peek_code_resource), so this middleware does —
        a mismatch answered here as invalid_grant before the SDK's own handler ever sees the request.

        Reads the body with `request.body()` (not `.form()`, which reads the raw ASGI stream directly and
        would leave nothing for the SDK's handler to read downstream) so the cached bytes replay for it."""
        body = await request.body()
        if "application/x-www-form-urlencoded" not in request.headers.get("content-type", ""):
            return None
        form = dict(parse_qsl(body.decode()))
        if form.get("grant_type") != "authorization_code":
            return None
        code, resource = form.get("code"), form.get("resource")
        if not code or not resource:
            return None
        stored = oauth.peek_code_resource(conn, code)
        if stored is not None and stored != resource:
            return "resource does not match the one used at authorization"
        return None


def create_app() -> FastAPI:
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        db.apply_schema(conn)  # idempotent; fail fast if the database is unreachable
        from . import benchmark

        benchmark.seed(conn)

    from . import channels, surface  # noqa: F401 - importing channels registers the channel adapters
    from .mcp_app import mcp  # importing registers the tools

    surface.load()  # the serving process has the public tools whichever module it imported first
    mcp_app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    app = FastAPI(title="Admiry agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=mcp_app.lifespan)
    app.state.settings = settings
    app.add_middleware(_OAuthRateLimit)
    app.add_middleware(_TenantHost)  # a tenant Host is fully answered here, before any router (H17)
    app.add_middleware(_HeadAsGet)  # outermost: every route below sees a HEAD as its GET

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "sha": settings.build_sha}

    @app.exception_handler(RequestValidationError)
    async def _bad_request_body(request: Request, exc: RequestValidationError):
        # A malformed JSON body (or one FastAPI cannot bind) answers 422 by default; the homepage's
        # /api/turn (A1b-4c) reads it as a plain dict and wants 400 for anything it cannot parse.
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"ok": False, "error": "bad request"}, status_code=400, headers={"Cache-Control": "no-store"}
            )
        return await request_validation_exception_handler(request, exc)

    from .connect import router as connect_router

    app.include_router(connect_router)

    from .pages import router as pages_router

    app.include_router(pages_router)

    from .web import router as web_router

    app.include_router(web_router)

    from .your_ads import router as your_ads_router

    app.include_router(your_ads_router)

    from .oauth_web import router as oauth_router

    app.include_router(oauth_router)

    from .pay import router as pay_router

    app.include_router(pay_router)

    from .home_api import router as home_router

    app.include_router(home_router)

    from .setup_api import router as setup_router
    from .track import router as track_router

    app.include_router(setup_router)
    app.include_router(track_router)

    from .support_api import router as support_router

    app.include_router(support_router)

    from .mail_in import router as mail_router

    app.include_router(mail_router)

    from .mail_web import router as mail_web_router

    app.include_router(mail_web_router)

    from .digest_web import router as digest_web_router

    app.include_router(digest_web_router)  # the daily digest's links, Reports card and /api/report (K2 Task 12)

    from .eval_web import router as eval_web_router

    app.include_router(eval_web_router)  # the founder's grading page, dev only (K5)

    from .ops_web import router as ops_web_router

    app.include_router(ops_web_router)  # the admins' launch review queue (L1)

    app.include_router(hosted.router)  # /p/{token}, its photos, /pages/ask (H17) -- the agent host only

    from .uploads import router as uploads_router

    app.include_router(uploads_router)  # /u/{token}, its photos -- the owner's own upload link (A3p-2 Task 6)

    from .site_upload import router as site_upload_router

    app.include_router(site_upload_router)  # PUT /s/{token}, GET /s/zip/{token} -- site bundles (A3p-3 batch 3)

    # python:3.12-slim has no /etc/mime.types and Python's own table has no .woff2: without this the vendored fonts
    # go out as application/octet-stream (a Mac's /etc/apache2/mime.types hides it locally)
    mimetypes.add_type("font/woff2", ".woff2")
    app.mount("/static", _Static(directory=Path(__file__).parent / "static"), name="static")
    app.mount("/", _ApiKeyShim(mcp_app))  # keep last
    return app
