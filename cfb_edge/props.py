"""Player props and alternate lines.

These do not come back from the main odds endpoint: each game needs its own
request, so a full Saturday slate costs far more quota than the rest of the
scan put together. Everything here is opt-in, windowed to games that kick off
soon, capped, and announced before a single request goes out.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Sequence

from cfb_edge.api import event_request_params, fetch_event_odds
from cfb_edge.cache import latest_response, save_response
from cfb_edge.config import Config
from cfb_edge.models import Game, merge_event_payload

if TYPE_CHECKING:  # pragma: no cover
    from cfb_edge.scan import ScanOptions


def selected_markets(cfg: Config, options: "ScanOptions") -> tuple[str, ...]:
    markets: list[str] = []
    if options.alts:
        markets.extend(cfg.alt_markets)
    if options.props:
        markets.extend(options.prop_markets or cfg.prop_markets)
    seen: list[str] = []
    for market in markets:
        if market not in seen:
            seen.append(market)
    return tuple(seen)


def eligible_games(
    games: Sequence[Game],
    cfg: Config,
    options: "ScanOptions",
    now: datetime | None = None,
) -> list[Game]:
    """Games close enough to kickoff to be worth a request, soonest first."""
    now = now or datetime.now(timezone.utc)
    window = cfg.props_window_hours if options.props_window_hours is None else options.props_window_hours
    cap = cfg.props_max_events if options.props_max_events is None else options.props_max_events

    upcoming = []
    for game in games:
        hours = game.hours_to_kickoff(now)
        if hours < 0:
            continue  # already kicked off
        if window is not None and hours > window:
            continue
        upcoming.append(game)
    upcoming.sort(key=lambda g: g.commence_time)
    return upcoming[:cap] if cap else upcoming


def estimate_cost(n_events: int, n_markets: int) -> int:
    """The Odds API bills per-event odds as markets x regions, per request."""
    return n_events * max(n_markets, 1)


def fetch_extra_markets(
    cfg: Config, games: Sequence[Game], options: "ScanOptions"
) -> tuple[tuple[str, ...], int, list[str]]:
    """Fold props/alt lines into `games`. Returns (markets, requests, warnings)."""
    markets = selected_markets(cfg, options)
    warnings: list[str] = []
    if not markets:
        return (), 0, warnings

    targets = eligible_games(games, cfg, options)
    if not targets:
        warnings.append(
            "no games inside the props window "
            f"({options.props_window_hours or cfg.props_window_hours:g}h to kickoff); "
            "nothing extra requested"
        )
        return (), 0, warnings

    cost = estimate_cost(len(targets), len(markets))
    threshold = cfg.props_confirm_threshold
    if options.confirm_quota is not None and cost > threshold:
        if not options.confirm_quota(cost, threshold):
            warnings.append("per-game requests declined; scanned core markets only")
            return (), 0, warnings

    requests_made = 0
    failures = 0
    for game in targets:
        payload, from_cache = _event_odds(cfg, game, markets, options)
        if payload:
            merge_event_payload(game, payload)
        if not from_cache:
            requests_made += 1
        if payload is None:
            failures += 1

    if failures:
        warnings.append(f"{failures} game(s) returned no props/alt lines")

    present = tuple(
        market
        for market in markets
        if any(market in book for game in games for book in game.books.values())
    )
    missing = [m for m in markets if m not in present]
    if missing:
        warnings.append(f"no book offered: {', '.join(missing)}")
    return present, requests_made, warnings


def _event_odds(
    cfg: Config, game: Game, markets: tuple[str, ...], options: "ScanOptions"
) -> tuple[dict[str, Any] | None, bool]:
    """One event's odds, from cache when fresh enough."""
    sport_key = f"{cfg.sport}_event_{game.event_id}"
    params = event_request_params(cfg, markets)
    max_age = (
        cfg.cache_max_age_minutes if options.max_cache_age is None else options.max_cache_age
    )

    cached = latest_response(cfg.cache_dir, sport_key, params)
    if cached is not None and not options.refresh:
        if options.cache_only or cached.age_minutes <= max_age:
            data = cached.data
            return (data[0] if isinstance(data, list) and data else data or None), True
    if options.cache_only:
        return None, True

    payload, quota, params = fetch_event_odds(cfg, game.event_id, markets)
    save_response(cfg.cache_dir, sport_key, params, [payload] if payload else [], quota=quota)
    return (payload or None), False
