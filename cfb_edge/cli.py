"""cfb-edge command line interface."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cfb_edge import __version__
from cfb_edge.api import OddsApiError
from cfb_edge.config import Config, ConfigError, load_config
from cfb_edge.edges import (
    DIFFERENT_NUMBER,
    NO_SHARP_LINE,
    NO_SHARP_SIDE,
    PRICED,
    TRANSLATED,
    different_number_rows,
)
from cfb_edge.models import MARKETS, PROP_MARKETS
from cfb_edge.report import (
    different_number_table,
    edge_table,
    kickoff_et,
    use_color,
)
from cfb_edge.scan import ScanOptions, ScanResult, persist, run_scan

STATUS_NOTES = {
    NO_SHARP_LINE: "no sharp/consensus line",
    NO_SHARP_SIDE: "sharp line had no matching side",
}

COMMANDS = ("scan", "watch", "scores", "halfpoint", "bets", "clv")


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument("--env-file", type=Path, default=Path(".env"),
                        help="path to .env (default: .env)")
    parser.add_argument("--db", type=Path, default=None, help="SQLite database path")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI color")


def add_scan_args(parser: argparse.ArgumentParser) -> None:
    add_common_args(parser)
    parser.add_argument("--min-edge", type=float, default=None, metavar="PCT",
                        help="minimum edge in percentage points (default: 1.0)")
    parser.add_argument("--market", action="append", default=None, metavar="MARKET",
                        help=f"limit to a market ({', '.join(MARKETS)}); "
                             "repeatable or comma-separated")
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
    parser.add_argument("--no-db", action="store_true", help="do not log this run to SQLite")
    parser.add_argument("--no-files", action="store_true", help="do not write CSV/JSON")
    parser.add_argument("--limit", type=int, default=None, help="show only the top N bets")
    parser.add_argument("--hide-different-number", action="store_true",
                        help="omit the DK-number-mismatch section")
    parser.add_argument("--props", action="store_true",
                        help="also scan player props (one API request per game)")
    parser.add_argument("--alts", action="store_true",
                        help="also scan alternate spreads and totals (one request per game)")
    parser.add_argument("--prop-market", action="append", default=None, metavar="MARKET",
                        help=f"limit props to these markets ({', '.join(PROP_MARKETS[:3])}, ...)")
    parser.add_argument("--props-window", type=float, default=None, metavar="HOURS",
                        help="only request props for games kicking off within this many hours")
    parser.add_argument("--props-max-events", type=int, default=None, metavar="N",
                        help="hard cap on per-game requests")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="skip the quota confirmation prompt")
    parser.add_argument("--notify", action="store_true",
                        help="send qualifying bets to the configured Discord webhook")
    parser.add_argument("--notify-dry-run", action="store_true",
                        help="show what would be sent to Discord without sending it")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cfb-edge",
        description="Find +EV DraftKings college football bets against a sharp book.",
    )
    parser.add_argument("--version", action="version", version=f"cfb-edge {__version__}")
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="score the current board (default)")
    add_scan_args(scan)

    watch = sub.add_parser("watch", help="rescan on an interval, printing only what changed")
    add_scan_args(watch)
    watch.add_argument("--interval", type=float, default=None, metavar="MINUTES",
                       help="minutes between scans (default: 15)")
    watch.add_argument("--iterations", type=int, default=None, metavar="N",
                       help="stop after N scans (default: run until interrupted)")
    watch.add_argument("--edge-delta", type=float, default=None, metavar="PCT",
                       help="edge move, in percentage points, that counts as a change "
                            "(default: 0.25)")
    watch.add_argument("--show-dropped", action="store_true",
                       help="also report lines that fell below the threshold")

    scores = sub.add_parser("scores", help="historical results used for half-point values")
    scores_sub = scores.add_subparsers(dest="scores_command")
    fetch = scores_sub.add_parser("fetch", help="download final scores from CollegeFootballData")
    add_common_args(fetch)
    fetch.add_argument("--seasons", default=None, metavar="RANGE",
                       help="season or range, e.g. 2015-2024 (default: the last 10 seasons)")
    fetch.add_argument("--scores-db", type=Path, default=None, help="where results are stored")
    fetch.add_argument("--division", default="fbs", help="fbs (default) or fcs")
    fetch.add_argument("--refresh", action="store_true",
                       help="re-download seasons already stored")
    info = scores_sub.add_parser("info", help="summarize what results are stored")
    add_common_args(info)
    info.add_argument("--scores-db", type=Path, default=None)

    halfpoint = sub.add_parser("halfpoint", help="half-point value table")
    hp_sub = halfpoint.add_subparsers(dest="halfpoint_command")
    hp_build = hp_sub.add_parser("build", help="build the table from stored results")
    add_common_args(hp_build)
    hp_build.add_argument("--scores-db", type=Path, default=None)
    hp_build.add_argument("--out", type=Path, default=None, help="where to write the table JSON")
    hp_build.add_argument("--min-season", type=int, default=None)
    hp_show = hp_sub.add_parser("show", help="print the stored table")
    add_common_args(hp_show)
    hp_show.add_argument("--table", type=Path, default=None, help="path to the table JSON")
    hp_show.add_argument("--market", default="spreads", choices=("spreads", "totals"))
    hp_show.add_argument("--max-number", type=float, default=21.0)

    bets = sub.add_parser("bets", help="log and review bets you placed")
    bets_sub = bets.add_subparsers(dest="bets_command")
    bet_add = bets_sub.add_parser("add", help="log a placed bet")
    add_common_args(bet_add)
    bet_add.add_argument("--event", required=True, metavar="ID_OR_TEXT",
                         help="event id, or text matching a game in the run log")
    bet_add.add_argument("--market", required=True)
    bet_add.add_argument("--side", required=True, help="team name, Over or Under")
    bet_add.add_argument("--point", type=float, default=None, help="the number you took")
    bet_add.add_argument("--price", type=float, required=True, help="American odds you got")
    bet_add.add_argument("--stake", type=float, required=True)
    bet_add.add_argument("--book", default=None, help="book you placed it at (default: draftkings)")
    bet_add.add_argument("--placed-at", default=None, metavar="ISO8601",
                         help="when it was placed (default: now)")
    bet_add.add_argument("--note", default="")
    bet_list = bets_sub.add_parser("list", help="show logged bets")
    add_common_args(bet_list)
    bet_list.add_argument("--open", action="store_true", help="only games that have not kicked off")
    bet_list.add_argument("--limit", type=int, default=50)

    clv = sub.add_parser("clv", help="compare your prices to the closing line")
    add_common_args(clv)
    clv.add_argument("--limit", type=int, default=100)
    clv.add_argument("--csv", type=Path, default=None, help="also write the report to a CSV")
    clv.add_argument("--all", action="store_true",
                     help="include bets whose game has not kicked off yet")
    return parser


def normalize_argv(argv: list[str] | None) -> list[str]:
    """`cfb-edge --min-edge 2` keeps working as a shorthand for `scan`."""
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return ["scan"]
    first = argv[0]
    if first in COMMANDS or first in ("-h", "--help", "--version"):
        return list(argv)
    return ["scan", *argv]


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
    for attr, field_name in (
        ("bankroll", "bankroll"),
        ("kelly", "kelly_fraction"),
        ("max_bet_pct", "max_bet_pct"),
        ("out_dir", "out_dir"),
        ("db", "db_path"),
        ("scores_db", "scores_db_path"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(cfg, field_name, value)
    if cfg.kelly_fraction <= 0:
        raise ConfigError("kelly fraction must be > 0")
    return cfg


def scan_options(args: argparse.Namespace, cfg: Config, markets: tuple[str, ...]) -> ScanOptions:
    return ScanOptions(
        markets=markets,
        min_edge=cfg.min_edge if args.min_edge is None else args.min_edge,
        refresh=args.refresh,
        cache_only=args.cache_only,
        cache_file=args.cache_file,
        max_cache_age=args.max_cache_age,
        limit=args.limit,
        write_files=not args.no_files,
        write_db=not args.no_db,
        props=args.props,
        alts=args.alts,
        prop_markets=tuple(args.prop_market) if args.prop_market else None,
        props_window_hours=args.props_window,
        props_max_events=args.props_max_events,
        confirm_quota=None if args.yes else _confirm_quota,
    )


def _confirm_quota(n_events: int, threshold: int) -> bool:
    if not sys.stdin.isatty():
        print(
            f"refusing to spend {n_events} API requests without confirmation "
            f"(threshold {threshold}); rerun with --yes",
            file=sys.stderr,
        )
        return False
    answer = input(f"This will use {n_events} API requests. Continue? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def load_cfg(args: argparse.Namespace) -> Config:
    cfg = load_config(getattr(args, "config", None), env_path=getattr(args, "env_file", ".env"))
    return apply_overrides(cfg, args)


def main(argv: list[str] | None = None) -> int:
    try:
        return _dispatch(normalize_argv(argv))
    except BrokenPipeError:
        # Something downstream closed the pipe first, e.g. `cfb-edge | head`.
        # Redirect stdout to devnull so the interpreter's final flush is quiet.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _dispatch(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "scan"

    handlers = {
        "scan": cmd_scan,
        "watch": cmd_watch,
        "scores": cmd_scores,
        "halfpoint": cmd_halfpoint,
        "bets": cmd_bets,
        "clv": cmd_clv,
    }
    try:
        return handlers[command](args, parser)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except OddsApiError as exc:
        print(f"odds api error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_scan(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    cfg = load_cfg(args)
    markets = resolve_markets(args.market, cfg)
    if not markets:
        raise ConfigError("no markets selected")
    options = scan_options(args, cfg, markets)

    result = run_scan(cfg, options)
    persist(cfg, result, options)

    color = use_color() and not args.no_color
    _print_report(cfg, args, result, color)
    _maybe_notify(cfg, args, result)
    return 0


def cmd_watch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from cfb_edge.watch import watch

    cfg = load_cfg(args)
    markets = resolve_markets(args.market, cfg)
    if not markets:
        raise ConfigError("no markets selected")
    return watch(cfg, args, scan_options(args, cfg, markets))


def cmd_scores(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from cfb_edge.scores import cmd_scores_fetch, cmd_scores_info

    if args.scores_command == "fetch":
        return cmd_scores_fetch(load_cfg(args), args)
    if args.scores_command == "info":
        return cmd_scores_info(load_cfg(args), args)
    parser.parse_args(["scores", "--help"])
    return 2


def cmd_halfpoint(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from cfb_edge.halfpoint import cmd_halfpoint_build, cmd_halfpoint_show

    if args.halfpoint_command == "build":
        return cmd_halfpoint_build(load_cfg(args), args)
    if args.halfpoint_command == "show":
        return cmd_halfpoint_show(load_cfg(args), args)
    parser.parse_args(["halfpoint", "--help"])
    return 2


def cmd_bets(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from cfb_edge.bets import cmd_bets_add, cmd_bets_list

    if args.bets_command == "add":
        return cmd_bets_add(load_cfg(args), args)
    if args.bets_command == "list":
        return cmd_bets_list(load_cfg(args), args)
    parser.parse_args(["bets", "--help"])
    return 2


def cmd_clv(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from cfb_edge.bets import cmd_clv_report

    return cmd_clv_report(load_cfg(args), args)


def _maybe_notify(cfg: Config, args: argparse.Namespace, result: ScanResult) -> None:
    if not (getattr(args, "notify", False) or getattr(args, "notify_dry_run", False)):
        return
    from cfb_edge.notify import NotifyError, notify_bets

    try:
        outcome = notify_bets(cfg, result, dry_run=args.notify_dry_run)
    except NotifyError as exc:
        # A failed alert must not take the scan down with it.
        print(f"discord: {exc}", file=sys.stderr)
        return
    if outcome.reason:
        print(f"discord: {outcome.reason}")
    if outcome.skipped_duplicates:
        print(f"discord: {outcome.skipped_duplicates} line(s) already announced")


def _print_report(cfg: Config, args: argparse.Namespace, result: ScanResult, color: bool) -> None:
    counts = result.status_counts
    snapshot = result.snapshot
    print()
    print(f"cfb-edge  run {result.run_id}")
    print(
        f"odds: {result.source} ({snapshot.path.name}), pulled "
        f"{kickoff_et(snapshot.fetched_at)} ET, {snapshot.age_minutes:.0f} min old"
    )
    quota = snapshot.quota.get("remaining")
    if quota:
        print(f"api quota remaining: {quota}")
    if result.event_requests:
        print(f"per-game requests spent this scan: {result.event_requests}")
    for warning in result.warnings:
        print(f"note: {warning}")
    print(
        f"games: {len(result.games)} | markets: {', '.join(result.markets)} | "
        f"min edge: {result.min_edge:g}% | bankroll: ${cfg.bankroll:,.0f} | "
        f"kelly: {cfg.kelly_fraction:g}x"
    )
    print()

    bets = result.bets
    if bets:
        print(edge_table(bets, color=color))
        total_stake = sum(b.stake or 0.0 for b in bets)
        total_ev = sum((b.ev_per_100 or 0.0) * (b.stake or 0.0) / 100.0 for b in bets)
        print()
        print(
            f"{len(bets)} bet(s) at >= {result.min_edge:g}% edge | "
            f"total stake ${total_stake:,.2f} | expected profit ${total_ev:,.2f}"
        )
    else:
        print(f"No DraftKings lines at or above a {result.min_edge:g}% edge.")

    flagged = different_number_rows(result.rows)
    if flagged and not args.hide_different_number:
        print()
        print(
            "Different number — DK is off the sharp line, edge not computed "
            f"({len(flagged)}):"
        )
        print(different_number_table(flagged, color=color))

    skipped = [
        f"{count} {STATUS_NOTES[status]}"
        for status, count in counts.items()
        if status in STATUS_NOTES
    ]
    translated = counts.get(TRANSLATED, 0)
    print()
    print(
        "evaluated "
        f"{counts.get(PRICED, 0)} priced line(s)"
        + (f", {translated} priced through the half-point table" if translated else "")
        + f", {counts.get(DIFFERENT_NUMBER, 0)} on a different number"
        + (", " + ", ".join(skipped) if skipped else "")
    )
    if result.csv_path and result.json_path:
        print(f"wrote {result.csv_path}")
        print(f"wrote {result.json_path}")
    if not args.no_db:
        print(f"logged to {cfg.db_path}")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
