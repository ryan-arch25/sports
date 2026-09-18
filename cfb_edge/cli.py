"""cfb-edge command line interface."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cfb_edge import __version__
from cfb_edge.api import OddsApiError, get_odds
from cfb_edge.config import Config, ConfigError, load_config
from cfb_edge.edges import (
    DIFFERENT_NUMBER,
    NO_SHARP_LINE,
    NO_SHARP_SIDE,
    PRICED,
    different_number_rows,
    evaluate_games,
    rank,
    summarize,
)
from cfb_edge.models import MARKETS, parse_games
from cfb_edge.report import (
    different_number_table,
    edge_table,
    kickoff_et,
    use_color,
    write_csv,
    write_json,
)
from cfb_edge.store import connect, log_run

STATUS_NOTES = {
    NO_SHARP_LINE: "no sharp/consensus line",
    NO_SHARP_SIDE: "sharp line had no matching side",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cfb-edge",
        description="Find +EV DraftKings college football bets against a sharp book.",
    )
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="path to .env (default: .env)")
    parser.add_argument(
        "--min-edge", type=float, default=None, metavar="PCT",
        help="minimum edge in percentage points (default: 1.0)",
    )
    parser.add_argument(
        "--market", action="append", default=None, metavar="MARKET",
        help=f"limit to a market ({', '.join(MARKETS)}); repeatable or comma-separated",
    )
    parser.add_argument("--bankroll", type=float, default=None, help="override bankroll from config")
    parser.add_argument("--kelly", type=float, default=None, metavar="FRACTION",
                        help="Kelly fraction (default: 0.25)")
    parser.add_argument("--max-bet-pct", type=float, default=None, metavar="PCT",
                        help="cap any stake at this percent of bankroll")
    parser.add_argument("--refresh", action="store_true", help="ignore cache and call the API")
    parser.add_argument("--cache-only", action="store_true",
                        help="never call the API; use the newest cached pull")
    parser.add_argument("--cache-file", type=Path, default=None,
                        help="score a specific cached snapshot")
    parser.add_argument("--max-cache-age", type=float, default=None, metavar="MINUTES",
                        help="reuse a cached pull younger than this (default: 15)")
    parser.add_argument("--out-dir", type=Path, default=None, help="where CSV/JSON are written")
    parser.add_argument("--db", type=Path, default=None, help="SQLite database path")
    parser.add_argument("--no-db", action="store_true", help="do not log this run to SQLite")
    parser.add_argument("--no-files", action="store_true", help="do not write CSV/JSON")
    parser.add_argument("--limit", type=int, default=None, help="show only the top N bets")
    parser.add_argument("--hide-different-number", action="store_true",
                        help="omit the DK-number-mismatch section")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI color")
    parser.add_argument("--version", action="version", version=f"cfb-edge {__version__}")
    return parser


def resolve_markets(requested: list[str] | None, cfg: Config) -> tuple[str, ...]:
    if not requested:
        return tuple(cfg.markets)
    chosen: list[str] = []
    for item in requested:
        for part in str(item).split(","):
            name = part.strip().lower()
            if not name:
                continue
            if name not in MARKETS:
                raise ConfigError(f"unknown market {name!r}; choose from {', '.join(MARKETS)}")
            if name not in chosen:
                chosen.append(name)
    return tuple(chosen)


def apply_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    if args.bankroll is not None:
        cfg.bankroll = args.bankroll
    if args.kelly is not None:
        cfg.kelly_fraction = args.kelly
    if args.max_bet_pct is not None:
        cfg.max_bet_pct = args.max_bet_pct
    if args.out_dir is not None:
        cfg.out_dir = args.out_dir
    if args.db is not None:
        cfg.db_path = args.db
    if cfg.kelly_fraction <= 0:
        raise ConfigError("kelly fraction must be > 0")
    return cfg


def make_run_id(now: datetime) -> str:
    return f"{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except BrokenPipeError:
        # Something downstream closed the pipe first, e.g. `cfb-edge | head`.
        # Redirect stdout to devnull so the interpreter's final flush is quiet.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started_at = datetime.now(timezone.utc)

    try:
        cfg = load_config(args.config, env_path=args.env_file)
        cfg = apply_overrides(cfg, args)
        markets = resolve_markets(args.market, cfg)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if not markets:
        print("config error: no markets selected", file=sys.stderr)
        return 2

    min_edge = cfg.min_edge if args.min_edge is None else args.min_edge

    try:
        snapshot, source = get_odds(
            cfg,
            markets,
            refresh=args.refresh,
            cache_only=args.cache_only,
            cache_file=args.cache_file,
            max_age_minutes=args.max_cache_age,
        )
    except OddsApiError as exc:
        print(f"odds api error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"could not read odds snapshot: {exc}", file=sys.stderr)
        return 1

    games = parse_games(snapshot.data)
    rows = evaluate_games(games, cfg, markets)
    bets = rank(rows, min_edge)
    if args.limit is not None:
        bets = bets[: args.limit]

    run_id = make_run_id(started_at)
    color = use_color() and not args.no_color
    fetched_at_iso = snapshot.fetched_at.isoformat().replace("+00:00", "Z")

    csv_path = json_path = None
    if not args.no_files:
        csv_path = cfg.out_dir / f"cfb-edge_{run_id}.csv"
        json_path = cfg.out_dir / f"cfb-edge_{run_id}.json"
        meta = {
            "source": source,
            "cache_file": str(snapshot.path),
            "sport": cfg.sport,
            "markets": list(markets),
            "min_edge_pct": min_edge,
            "bankroll": cfg.bankroll,
            "kelly_fraction": cfg.kelly_fraction,
            "max_bet_pct": cfg.max_bet_pct,
            "target_book": cfg.target_book,
            "sharp_priority": list(cfg.sharp_priority),
            "consensus_books": list(cfg.consensus_books),
            "games": len(games),
            "status_counts": summarize(rows),
            "quota": snapshot.quota,
        }
        try:
            write_csv(csv_path, rows, run_id, fetched_at_iso, min_edge)
            write_json(json_path, rows, run_id, fetched_at_iso, min_edge, meta)
        except OSError as exc:
            print(f"could not write output files: {exc}", file=sys.stderr)
            return 1

    if not args.no_db:
        try:
            conn = connect(cfg.db_path)
            try:
                log_run(
                    conn,
                    run_id=run_id,
                    started_at=started_at,
                    fetched_at=snapshot.fetched_at,
                    source=source,
                    cache_path=str(snapshot.path),
                    sport=cfg.sport,
                    markets=markets,
                    min_edge_pct=min_edge,
                    bankroll=cfg.bankroll,
                    kelly_fraction=cfg.kelly_fraction,
                    n_games=len(games),
                    rows=rows,
                    quota_remaining=snapshot.quota.get("remaining"),
                    csv_path=str(csv_path) if csv_path else None,
                    json_path=str(json_path) if json_path else None,
                )
            finally:
                conn.close()
        except Exception as exc:  # sqlite3.Error and friends
            print(f"warning: could not log run to {cfg.db_path}: {exc}", file=sys.stderr)

    _print_report(
        cfg, args, snapshot, source, games, rows, bets, markets, min_edge, color,
        csv_path, json_path, run_id,
    )
    return 0


def _print_report(
    cfg, args, snapshot, source, games, rows, bets, markets, min_edge, color,
    csv_path, json_path, run_id,
):
    counts = summarize(rows)
    age = snapshot.age_minutes
    print()
    print(f"cfb-edge  run {run_id}")
    print(
        f"odds: {source} ({snapshot.path.name}), pulled "
        f"{kickoff_et(snapshot.fetched_at)} ET, {age:.0f} min old"
    )
    quota = snapshot.quota.get("remaining")
    if quota:
        print(f"api quota remaining: {quota}")
    print(
        f"games: {len(games)} | markets: {', '.join(markets)} | "
        f"min edge: {min_edge:g}% | bankroll: ${cfg.bankroll:,.0f} | "
        f"kelly: {cfg.kelly_fraction:g}x"
    )
    print()

    if bets:
        print(edge_table(bets, color=color))
        total_stake = sum(b.stake or 0.0 for b in bets)
        total_ev = sum((b.ev_per_100 or 0.0) * (b.stake or 0.0) / 100.0 for b in bets)
        print()
        print(
            f"{len(bets)} bet(s) at >= {min_edge:g}% edge | "
            f"total stake ${total_stake:,.2f} | expected profit ${total_ev:,.2f}"
        )
    else:
        print(f"No DraftKings lines at or above a {min_edge:g}% edge.")

    flagged = different_number_rows(rows)
    if flagged and not args.hide_different_number:
        print()
        print(f"Different number — DK is off the sharp line, edge not computed ({len(flagged)}):")
        print(different_number_table(flagged, color=color))

    skipped = [
        f"{count} {STATUS_NOTES[status]}"
        for status, count in counts.items()
        if status in STATUS_NOTES
    ]
    print()
    print(
        "evaluated "
        f"{counts.get(PRICED, 0)} priced line(s), "
        f"{counts.get(DIFFERENT_NUMBER, 0)} on a different number"
        + (", " + ", ".join(skipped) if skipped else "")
    )
    if csv_path and json_path:
        print(f"wrote {csv_path}")
        print(f"wrote {json_path}")
    if not args.no_db:
        print(f"logged to {cfg.db_path}")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
