"""--watch: rescan on an interval and report only what changed.

The first pass prints every qualifying line, because on the first pass they are
all new. After that the loop is quiet unless a bet appears, its price or number
moves, or its edge shifts by more than a threshold.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Sequence

from cfb_edge.config import Config
from cfb_edge.edges import EdgeRow
from cfb_edge.report import change_table, kickoff_et, use_color
from cfb_edge.scan import ScanOptions, ScanResult, persist, run_scan, scan_key

NEW = "new"
PRICE = "price"
NUMBER = "number"
EDGE = "edge"
DROPPED = "dropped"


@dataclass
class Change:
    kind: str
    row: EdgeRow
    previous: EdgeRow | None
    description: str


def _fmt_price(price: float | None) -> str:
    if price is None:
        return "-"
    value = round(float(price))
    return f"+{value}" if value > 0 else str(value)


def diff_bets(
    previous: dict[tuple[str, str, str], EdgeRow],
    current: Sequence[EdgeRow],
    edge_delta_pct: float,
    include_dropped: bool = False,
) -> list[Change]:
    """New, moved and (optionally) departed lines, best edge first."""
    changes: list[Change] = []
    seen: set[tuple[str, str, str]] = set()

    for row in current:
        key = scan_key(row)
        seen.add(key)
        before = previous.get(key)
        if before is None:
            changes.append(Change(NEW, row, None, "new"))
            continue
        if not _same_number(before.dk_point, row.dk_point):
            changes.append(Change(
                NUMBER, row, before,
                f"number {_fmt_point(before.dk_point)}→{_fmt_point(row.dk_point)}",
            ))
            continue
        if before.dk_price != row.dk_price:
            changes.append(Change(
                PRICE, row, before,
                f"price {_fmt_price(before.dk_price)}→{_fmt_price(row.dk_price)}",
            ))
            continue
        before_edge = (before.edge or 0.0) * 100
        now_edge = (row.edge or 0.0) * 100
        if abs(now_edge - before_edge) >= edge_delta_pct:
            changes.append(Change(
                EDGE, row, before, f"edge {before_edge:.1f}%→{now_edge:.1f}%",
            ))

    if include_dropped:
        for key, before in previous.items():
            if key not in seen:
                changes.append(Change(DROPPED, before, before, "dropped"))

    changes.sort(key=lambda c: (c.kind == DROPPED, -((c.row.edge or 0.0))))
    return changes


def _same_number(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < 1e-9


def _fmt_point(point: float | None) -> str:
    return "-" if point is None else f"{point:g}"


def watch(cfg: Config, args: argparse.Namespace, options: ScanOptions) -> int:
    """Run scans on a loop until interrupted or --iterations is reached."""
    interval = cfg.watch_interval_minutes if args.interval is None else args.interval
    edge_delta = cfg.watch_edge_delta if args.edge_delta is None else args.edge_delta
    color = use_color() and not args.no_color
    replay = bool(options.cache_file or options.cache_only)
    # A live watch always wants current prices; a replay leaves the cache alone.
    options = replace(options, refresh=options.refresh or not replay)

    print(
        f"cfb-edge watch: every {interval:g} min, "
        f"min edge {options.min_edge:g}%, reporting moves of {edge_delta:g}pt or more"
    )
    print("ctrl-c to stop")

    state: dict[tuple[str, str, str], EdgeRow] = {}
    iterations = args.iterations
    completed = 0

    while iterations is None or completed < iterations:
        try:
            result = run_scan(cfg, options)
        except Exception as exc:  # a scan failing should not end the watch
            print(f"\n[{_stamp()}] scan failed: {exc}", file=sys.stderr)
            if iterations is not None and completed + 1 >= iterations:
                return 1
            completed += 1
            _sleep(interval)
            continue

        persist(cfg, result, options)
        changes = diff_bets(state, result.bets, edge_delta, include_dropped=args.show_dropped)
        _report(result, changes, color)
        _notify(cfg, args, result, changes)
        state = {scan_key(row): row for row in result.bets}
        completed += 1
        if iterations is not None and completed >= iterations:
            break
        _sleep(interval)
    return 0


def _report(result: ScanResult, changes: Sequence[Change], color: bool) -> None:
    stamp = _stamp()
    if not changes:
        print(
            f"[{stamp}] {len(result.bets)} qualifying line(s), nothing new "
            f"({len(result.games)} games)"
        )
        return
    print()
    print(
        f"[{stamp}] {len(changes)} change(s) across {len(result.bets)} qualifying line(s) "
        f"— odds pulled {kickoff_et(result.snapshot.fetched_at)} ET"
    )
    print(change_table(changes, color=color))


def _notify(cfg: Config, args: argparse.Namespace, result: ScanResult, changes) -> None:
    if not (getattr(args, "notify", False) or getattr(args, "notify_dry_run", False)):
        return
    from cfb_edge.notify import NotifyError, notify_bets

    try:
        notify_bets(cfg, result, dry_run=args.notify_dry_run)
    except NotifyError as exc:
        print(f"discord: {exc}", file=sys.stderr)


def _sleep(minutes: float) -> None:
    try:
        time.sleep(max(minutes, 0.0) * 60.0)
    except KeyboardInterrupt:
        raise


def _stamp(now: datetime | None = None) -> str:
    return kickoff_et(now or datetime.now(timezone.utc))
