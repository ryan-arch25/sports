"""The scan pipeline: fetch a board, price it, persist the results.

Kept separate from the CLI so that one-shot runs, --watch and tests all drive
the same code path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cfb_edge.api import get_odds
from cfb_edge.cache import CachedResponse
from cfb_edge.config import Config
from cfb_edge.edges import EdgeRow, evaluate_games, rank, summarize
from cfb_edge.models import Game, parse_games
from cfb_edge.report import write_csv, write_json
from cfb_edge.store import connect, log_run


def make_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"


@dataclass
class ScanOptions:
    """Everything that changes how a scan is fetched, priced and stored."""

    markets: tuple[str, ...] = ("h2h", "spreads", "totals")
    min_edge: float = 1.0
    refresh: bool = False
    cache_only: bool = False
    cache_file: Path | None = None
    max_cache_age: float | None = None
    limit: int | None = None
    write_files: bool = True
    write_db: bool = True
    props: bool = False
    alts: bool = False
    prop_markets: tuple[str, ...] | None = None
    props_window_hours: float | None = None
    props_max_events: int | None = None
    confirm_quota: Any = None  # callable(n_events) -> bool


@dataclass
class ScanResult:
    run_id: str
    started_at: datetime
    snapshot: CachedResponse
    source: str
    games: list[Game]
    rows: list[EdgeRow]
    bets: list[EdgeRow]
    markets: tuple[str, ...]
    min_edge: float
    csv_path: Path | None = None
    json_path: Path | None = None
    event_requests: int = 0
    warnings: list[str] = field(default_factory=list)
    halfpoint_source: str | None = None

    @property
    def fetched_at_iso(self) -> str:
        return self.snapshot.fetched_at.isoformat().replace("+00:00", "Z")

    @property
    def status_counts(self) -> dict[str, int]:
        return summarize(self.rows)

    def meta(self, cfg: Config) -> dict[str, Any]:
        return {
            "source": self.source,
            "cache_file": str(self.snapshot.path),
            "sport": cfg.sport,
            "markets": list(self.markets),
            "min_edge_pct": self.min_edge,
            "bankroll": cfg.bankroll,
            "kelly_fraction": cfg.kelly_fraction,
            "max_bet_pct": cfg.max_bet_pct,
            "target_book": cfg.target_book,
            "sharp_priority": list(cfg.sharp_priority),
            "consensus_books": list(cfg.consensus_books),
            "games": len(self.games),
            "status_counts": self.status_counts,
            "quota": self.snapshot.quota,
            "event_requests": self.event_requests,
            "halfpoint_table": self.halfpoint_source,
        }


def load_halfpoint(cfg: Config):
    """The half-point table, if it has been built and is switched on."""
    if not cfg.halfpoint_enabled:
        return None, None, None
    from cfb_edge.halfpoint import HalfPointError, load_table

    try:
        table = load_table(cfg.halfpoint_table_path)
    except HalfPointError as exc:
        return None, None, f"half-point table ignored: {exc}"
    if table is None or table.is_empty():
        return None, None, None
    return table, str(cfg.halfpoint_table_path), None


def run_scan(cfg: Config, options: ScanOptions) -> ScanResult:
    """Fetch the board and price every DraftKings line on it."""
    started_at = datetime.now(timezone.utc)
    snapshot, source = get_odds(
        cfg,
        options.markets,
        refresh=options.refresh,
        cache_only=options.cache_only,
        cache_file=options.cache_file,
        max_age_minutes=options.max_cache_age,
    )
    games = parse_games(snapshot.data)
    markets = tuple(options.markets)
    warnings: list[str] = []
    event_requests = 0
    table, halfpoint_source, table_warning = load_halfpoint(cfg)
    if table_warning:
        warnings.append(table_warning)

    if options.props or options.alts:
        from cfb_edge.props import fetch_extra_markets

        extra_markets, event_requests, extra_warnings = fetch_extra_markets(
            cfg, games, options
        )
        markets = markets + extra_markets
        warnings.extend(extra_warnings)

    rows = evaluate_games(games, cfg, markets, table)
    bets = rank(rows, options.min_edge)
    if options.limit is not None:
        bets = bets[: options.limit]

    return ScanResult(
        run_id=make_run_id(started_at),
        started_at=started_at,
        snapshot=snapshot,
        source=source,
        games=games,
        rows=rows,
        bets=bets,
        markets=markets,
        min_edge=options.min_edge,
        event_requests=event_requests,
        warnings=warnings,
        halfpoint_source=halfpoint_source,
    )


def persist(cfg: Config, result: ScanResult, options: ScanOptions) -> ScanResult:
    """Write the CSV/JSON artifacts and append the run to SQLite."""
    if options.write_files:
        result.csv_path = cfg.out_dir / f"cfb-edge_{result.run_id}.csv"
        result.json_path = cfg.out_dir / f"cfb-edge_{result.run_id}.json"
        write_csv(result.csv_path, result.rows, result.run_id, result.fetched_at_iso, result.min_edge)
        write_json(
            result.json_path, result.rows, result.run_id, result.fetched_at_iso,
            result.min_edge, result.meta(cfg),
        )
    if options.write_db:
        conn = connect(cfg.db_path)
        try:
            log_run(
                conn,
                run_id=result.run_id,
                started_at=result.started_at,
                fetched_at=result.snapshot.fetched_at,
                source=result.source,
                cache_path=str(result.snapshot.path),
                sport=cfg.sport,
                markets=result.markets,
                min_edge_pct=result.min_edge,
                bankroll=cfg.bankroll,
                kelly_fraction=cfg.kelly_fraction,
                n_games=len(result.games),
                rows=result.rows,
                quota_remaining=result.snapshot.quota.get("remaining"),
                csv_path=str(result.csv_path) if result.csv_path else None,
                json_path=str(result.json_path) if result.json_path else None,
            )
        finally:
            conn.close()
    return result


def scan_key(row: EdgeRow) -> tuple[str, str, str]:
    """Identity of a line across scans, independent of the number DK is on."""
    return (row.event_id, row.market, row.side)
