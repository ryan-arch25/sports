"""The Odds API client."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from cfb_edge.cache import CachedResponse, latest_response, read_response, save_response
from cfb_edge.config import Config

BASE_URL = "https://api.the-odds-api.com/v4"
QUOTA_HEADERS = {
    "x-requests-remaining": "remaining",
    "x-requests-used": "used",
    "x-requests-last": "last_cost",
}


class OddsApiError(RuntimeError):
    pass


def request_params(cfg: Config, markets: tuple[str, ...]) -> dict[str, Any]:
    """Query params, minus the API key (which must never reach the cache)."""
    return {
        "regions": cfg.regions,
        "markets": ",".join(markets),
        "oddsFormat": "american",
        "dateFormat": "iso",
        # `bookmakers` takes precedence over `regions` and is what lets us pull
        # Pinnacle (eu) and Circa (us2) alongside the US books.
        "bookmakers": ",".join(cfg.api_book_keys()),
    }


def event_request_params(cfg: Config, markets: tuple[str, ...]) -> dict[str, Any]:
    params = request_params(cfg, markets)
    params["markets"] = ",".join(markets)
    return params


def fetch_event_odds(
    cfg: Config, event_id: str, markets: tuple[str, ...], timeout: float = 30.0
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    """Odds for one event, which is the only way to get props and alt lines."""
    if not cfg.api_key:
        raise OddsApiError("ODDS_API_KEY is not set; cannot request per-event odds.")
    params = event_request_params(cfg, markets)
    url = f"{BASE_URL}/sports/{cfg.sport}/events/{event_id}/odds"
    try:
        response = requests.get(url, params={**params, "apiKey": cfg.api_key}, timeout=timeout)
    except requests.RequestException as exc:
        raise OddsApiError(f"request to The Odds API failed: {exc}") from exc

    if response.status_code == 404:
        # The event has no book offering these markets yet.
        return {}, {}, params
    if response.status_code == 422:
        raise OddsApiError(
            f"422 from The Odds API for event {event_id} "
            f"(unsupported market?): {response.text[:200]}"
        )
    if response.status_code == 429:
        raise OddsApiError("429 from The Odds API: request quota exhausted.")
    if not response.ok:
        raise OddsApiError(f"{response.status_code} from The Odds API: {response.text[:200]}")

    try:
        data = response.json()
    except ValueError as exc:
        raise OddsApiError(f"The Odds API returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise OddsApiError(f"expected an event object, got {type(data).__name__}")

    quota = {
        label: response.headers[header]
        for header, label in QUOTA_HEADERS.items()
        if header in response.headers
    }
    return data, quota, params


def fetch_odds(
    cfg: Config, markets: tuple[str, ...], timeout: float = 30.0
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    """Call the odds endpoint. Returns (events, quota headers, params used)."""
    if not cfg.api_key:
        raise OddsApiError(
            "ODDS_API_KEY is not set. Put it in a .env file as ODDS_API_KEY=... "
            "or export it, or rerun with --cache-only to use a saved pull."
        )
    params = request_params(cfg, markets)
    url = f"{BASE_URL}/sports/{cfg.sport}/odds"
    try:
        response = requests.get(url, params={**params, "apiKey": cfg.api_key}, timeout=timeout)
    except requests.RequestException as exc:
        raise OddsApiError(f"request to The Odds API failed: {exc}") from exc

    if response.status_code == 401:
        raise OddsApiError("401 from The Odds API: the API key was rejected.")
    if response.status_code == 429:
        raise OddsApiError("429 from The Odds API: request quota exhausted.")
    if response.status_code == 422:
        raise OddsApiError(f"422 from The Odds API (bad parameters): {response.text[:300]}")
    if not response.ok:
        raise OddsApiError(f"{response.status_code} from The Odds API: {response.text[:300]}")

    try:
        data = response.json()
    except ValueError as exc:
        raise OddsApiError(f"The Odds API returned invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise OddsApiError(f"expected a list of events, got {type(data).__name__}")

    quota = {
        label: response.headers[header]
        for header, label in QUOTA_HEADERS.items()
        if header in response.headers
    }
    return data, quota, params


def get_odds(
    cfg: Config,
    markets: tuple[str, ...],
    *,
    refresh: bool = False,
    cache_only: bool = False,
    cache_file: Path | None = None,
    max_age_minutes: float | None = None,
) -> tuple[CachedResponse, str]:
    """Return odds from cache when fresh enough, otherwise from the API.

    The second element of the tuple is 'api', 'cache' or 'cache-file'.
    """
    if cache_file is not None:
        return read_response(Path(cache_file)), "cache-file"

    params = request_params(cfg, markets)
    max_age = cfg.cache_max_age_minutes if max_age_minutes is None else max_age_minutes

    cached = latest_response(cfg.cache_dir, cfg.sport, params)
    if cached is not None and not refresh and (cache_only or cached.age_minutes <= max_age):
        return cached, "cache"
    if cache_only:
        raise OddsApiError(
            f"--cache-only was set but no cached pull exists in {cfg.cache_dir} "
            "for these markets and books."
        )

    data, quota, params = fetch_odds(cfg, markets)
    saved = save_response(
        cfg.cache_dir,
        cfg.sport,
        params,
        data,
        quota=quota,
        fetched_at=datetime.now(timezone.utc),
    )
    return saved, "api"
