"""The FastAPI app: one HTML page, a JSON endpoint behind a shared password."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from cfb_edge import __version__
from cfb_edge.config import Config, ConfigError, load_config
from cfb_edge.scan import ScanOptions
from cfb_edge.web.auth import COOKIE_NAME, Auth, AuthNotConfigured
from cfb_edge.web.service import Dashboard

log = logging.getLogger("cfb_edge.web")

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
LOGIN_HTML = STATIC_DIR / "login.html"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def build_config(env: dict[str, str] | None = None) -> Config:
    """Config file if there is one, then environment overrides.

    A deployment usually has no config.toml (it is gitignored, so it is not in
    the image either), which is why every setting the dashboard needs can also
    come from an environment variable.
    """
    try:
        cfg = load_config()
    except ConfigError as exc:
        log.warning("could not read config (%s); falling back to defaults", exc)
        cfg = Config()
    return apply_env_overrides(cfg, env)


def apply_env_overrides(cfg: Config, env: dict[str, str] | None = None) -> Config:
    env = os.environ if env is None else env

    def number(name: str, current: float) -> float:
        raw = env.get(name)
        if not raw:
            return current
        try:
            return float(raw)
        except ValueError:
            log.warning("%s=%r is not a number; keeping %s", name, raw, current)
            return current

    cfg.bankroll = number("BANKROLL", cfg.bankroll)
    cfg.kelly_fraction = number("KELLY_FRACTION", cfg.kelly_fraction)
    cfg.min_edge = number("MIN_EDGE", cfg.min_edge)
    cfg.web_refresh_minutes = number("DASHBOARD_REFRESH_MINUTES", cfg.web_refresh_minutes)
    if env.get("MAX_BET_PCT"):
        cfg.max_bet_pct = number("MAX_BET_PCT", cfg.max_bet_pct or 0.0)
    if env.get("DASHBOARD_TITLE"):
        cfg.web_title = env["DASHBOARD_TITLE"]
    if env.get("MARKETS"):
        markets = tuple(m.strip() for m in env["MARKETS"].split(",") if m.strip())
        if markets:
            cfg.markets = markets
    # Railway mounts a volume here when you add one; without it the paths stay
    # relative and everything written is lost on redeploy.
    data_dir = env.get("DATA_DIR")
    if data_dir:
        base = Path(data_dir)
        cfg.cache_dir = base / "cache"
        cfg.out_dir = base / "runs"
        cfg.db_path = base / "cfb_edge.sqlite"
        cfg.scores_db_path = base / "scores.sqlite"
        cfg.halfpoint_table_path = base / "halfpoint.json"
    if cfg.kelly_fraction <= 0:
        log.warning("KELLY_FRACTION must be > 0; using 0.25")
        cfg.kelly_fraction = 0.25
    return cfg


def create_app(
    cfg: Config | None = None,
    auth: Auth | None = None,
    dashboard: Dashboard | None = None,
    start_scheduler: bool = True,
) -> FastAPI:
    cfg = cfg or build_config()
    auth = auth or Auth.from_env()
    if dashboard is None:
        refresh = cfg.web_refresh_minutes
        dashboard = Dashboard(
            cfg,
            options=ScanOptions(
                markets=tuple(cfg.markets),
                min_edge=cfg.web_floor_edge,
                write_files=_env_bool("DASHBOARD_WRITE_FILES", False),
                write_db=_env_bool("DASHBOARD_WRITE_DB", True),
            ),
            refresh_minutes=refresh,
            display_min_edge=cfg.min_edge,
            floor_edge=cfg.web_floor_edge,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler and auth.configured:
            await dashboard.start()
        elif not auth.configured:
            log.error(
                "DASHBOARD_PASSWORD is not set; refusing to scan or serve. "
                "Set it and restart."
            )
        try:
            yield
        finally:
            await dashboard.stop()

    app = FastAPI(
        title=cfg.web_title,
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.cfg = cfg
    app.state.auth = auth
    app.state.dashboard = dashboard

    def client_key(request: Request) -> str:
        """Identify the caller for the login throttle, behind Railway's proxy."""
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def is_secure(request: Request) -> bool:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        return proto.split(",")[0].strip() == "https"

    def signed_in(request: Request) -> bool:
        return auth.valid_token(request.cookies.get(COOKIE_NAME))

    def require_auth(request: Request) -> None:
        """Dependency for the JSON API: 401 rather than a redirect."""
        try:
            auth.require_configured()
        except AuthNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not signed_in(request):
            raise HTTPException(status_code=401, detail="sign in required")

    def render(path: Path, **substitutions: str) -> str:
        html = path.read_text(encoding="utf-8")
        for key, value in substitutions.items():
            html = html.replace("{{" + key + "}}", value)
        return html

    def login_page(request: Request, error: str = "", status_code: int = 200) -> HTMLResponse:
        return HTMLResponse(
            render(
                LOGIN_HTML,
                title=_escape(cfg.web_title),
                error=_escape(error),
                error_style="block" if error else "none",
            ),
            status_code=status_code,
        )

    # -- routes -----------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        """Unauthenticated, for Railway's health check."""
        state = dashboard.state
        return JSONResponse({
            "ok": True,
            "version": __version__,
            "password_configured": auth.configured,
            "scan_ready": state.ready,
            "last_updated": state.updated_at_utc,
            "last_error": state.error,
        })

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        if not auth.configured:
            return HTMLResponse(
                render(LOGIN_HTML, title=_escape(cfg.web_title),
                       error=_escape(
                           "DASHBOARD_PASSWORD is not set on the server, so the "
                           "dashboard cannot let anyone in. Set it and redeploy."
                       ),
                       error_style="block"),
                status_code=503,
            )
        if not signed_in(request):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(render(INDEX_HTML, title=_escape(cfg.web_title)))

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        if signed_in(request):
            return RedirectResponse("/", status_code=303)
        return login_page(request)

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Response:
        password = read_form_field(await request.body(), "password")
        if not auth.configured:
            return login_page(
                request, "DASHBOARD_PASSWORD is not set on the server.", status_code=503
            )
        client = client_key(request)
        if auth.throttled(client):
            wait = auth.seconds_until_unthrottled(client)
            return login_page(
                request, f"Too many attempts. Try again in {wait} seconds.", status_code=429
            )
        if not auth.check_password(password):
            auth.record_failure(client)
            return login_page(request, "Wrong password.", status_code=401)

        auth.clear_failures(client)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            auth.issue_token(),
            max_age=auth.max_age_seconds,
            httponly=True,
            samesite="lax",
            secure=is_secure(request),
            path="/",
        )
        return response

    @app.post("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/api/state")
    async def api_state(_: None = Depends(require_auth)) -> JSONResponse:
        return JSONResponse(dashboard.state.as_dict())

    @app.post("/api/refresh")
    async def api_refresh(_: None = Depends(require_auth)) -> JSONResponse:
        refreshed, message = await dashboard.manual_refresh()
        payload: dict[str, Any] = dashboard.state.as_dict()
        payload["refreshed"] = refreshed
        payload["message"] = message
        return JSONResponse(payload)

    return app


# The login form is one urlencoded field, so it is parsed here rather than
# pulling in python-multipart just to read it.
MAX_FORM_BYTES = 64 * 1024


def read_form_field(body: bytes, field: str) -> str:
    """One field out of an `application/x-www-form-urlencoded` body."""
    if not body or len(body) > MAX_FORM_BYTES:
        return ""
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    values = parse_qs(decoded, keep_blank_values=True).get(field) or []
    return values[0] if values else ""


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


app = create_app()
