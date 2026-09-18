"""Terminal table plus CSV/JSON artifacts for a run."""

from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from cfb_edge.edges import DIFFERENT_NUMBER, PRICED, TRANSLATED, EdgeRow, format_pick
from cfb_edge.oddsmath import format_american

ET = ZoneInfo("America/New_York")

CSV_COLUMNS = [
    "run_id", "fetched_at", "event_id", "commence_time_utc", "kickoff_et", "matchup",
    "away_team", "home_team", "market", "market_label", "side", "pick", "dk_point",
    "dk_price", "dk_prob", "sharp_source", "sharp_books", "sharp_point", "sharp_price",
    "sharp_hold", "line_diff", "fair_prob", "fair_american", "edge_pct", "ev_per_100",
    "stake", "status", "above_min_edge", "translated_from", "translation_source", "note",
]

STATUS_ORDER = {PRICED: 0, TRANSLATED: 1, DIFFERENT_NUMBER: 2}


def kickoff_et(dt: datetime) -> str:
    local = dt.astimezone(ET)
    hour = local.hour % 12 or 12  # %-I is not portable
    return f"{local:%a %m/%d} {hour}:{local:%M %p}"


def _pct(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def _money(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:,.2f}"


def _line_diff(value: float | None, color: bool = False) -> str:
    """How much better DK's number is, in points. A dash means the same number."""
    if value is None or value == 0:
        return "-"
    return _colorize(f"{value:+g}", "green" if value > 0 else "red", color)


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def use_color(stream=sys.stdout) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _colorize(text: str, color: str, enabled: bool) -> str:
    if not enabled:
        return text
    codes = {"green": "32", "yellow": "33", "dim": "2", "bold": "1", "red": "31"}
    return f"\033[{codes[color]}m{text}\033[0m"


@dataclass
class Column:
    header: str
    align: str = "left"
    width: int = 0


def render_table(headers: Sequence[Column], rows: Sequence[Sequence[str]]) -> str:
    """Plain-text table: two-space gutters, per-column alignment."""
    widths = [max(len(h.header), *(len(r[i]) for r in rows)) if rows else len(h.header)
              for i, h in enumerate(headers)]
    lines = []
    header_cells = [
        h.header.ljust(widths[i]) if h.align == "left" else h.header.rjust(widths[i])
        for i, h in enumerate(headers)
    ]
    lines.append("  ".join(header_cells).rstrip())
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        cells = [
            row[i].ljust(widths[i]) if headers[i].align == "left" else row[i].rjust(widths[i])
            for i in range(len(headers))
        ]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def _sharp_cell(row: EdgeRow) -> str:
    """Sharp price, plus the sharp's own number when DK is not on it."""
    price = format_american(row.sharp_price)
    if row.status == TRANSLATED and row.sharp_point is not None:
        source = f"{row.sharp_source} est" if row.is_estimated else row.sharp_source
        return f"{price} @{row.sharp_point:g} ({source})"
    return f"{price} ({row.sharp_source})"


def edge_table(rows: Sequence[EdgeRow], color: bool = False, game_width: int = 32) -> str:
    headers = [
        Column("GAME"), Column("KICKOFF (ET)"), Column("MARKET"), Column("PICK"),
        Column("DK", "right"), Column("SHARP", "right"), Column("LINE DIFF", "right"),
        Column("FAIR%", "right"), Column("DK%", "right"), Column("EDGE", "right"),
        Column("EV/$100", "right"), Column("STAKE", "right"),
    ]
    body = []
    for row in rows:
        edge_text = _pct(row.edge)
        body.append([
            _truncate(row.matchup, game_width),
            kickoff_et(row.commence_time),
            row.market_label,
            _truncate(row.pick, 26),
            format_american(row.dk_price),
            _sharp_cell(row),
            _line_diff(row.line_diff, color),
            _pct(row.fair_prob),
            _pct(row.dk_prob),
            _colorize(edge_text, "green", color),
            _money(row.ev_per_100),
            f"${row.stake:,.2f}" if row.stake is not None else "-",
        ])
    return render_table(headers, body)


def different_number_table(rows: Sequence[EdgeRow], color: bool = False, game_width: int = 32) -> str:
    headers = [
        Column("GAME"), Column("KICKOFF (ET)"), Column("MARKET"), Column("DK LINE"),
        Column("DK", "right"), Column("SHARP LINE"), Column("SHARP", "right"),
        Column("LINE DIFF", "right"),
    ]
    body = []
    for row in rows:
        body.append([
            _truncate(row.matchup, game_width),
            kickoff_et(row.commence_time),
            row.market_label,
            _truncate(row.pick, 26),
            format_american(row.dk_price),
            _truncate(format_pick(row.market, row.side, row.sharp_point), 26),
            f"{format_american(row.sharp_price)} ({row.sharp_source})",
            _line_diff(row.line_diff, color),
        ])
    return render_table(headers, body)


def _sort_key(row: EdgeRow) -> tuple:
    return (
        STATUS_ORDER.get(row.status, 3),
        -(row.edge or -99.0) if row.is_bet else 0.0,
        row.commence_time,
        row.matchup,
        row.market,
        row.side,
    )


def row_record(row: EdgeRow, run_id: str, fetched_at: str, min_edge_pct: float) -> dict[str, Any]:
    data = row.as_dict()
    data.update(
        run_id=run_id,
        fetched_at=fetched_at,
        commence_time_utc=data.pop("commence_time"),
        kickoff_et=kickoff_et(row.commence_time),
        above_min_edge=bool(
            row.is_bet and row.edge is not None and row.edge * 100 >= min_edge_pct
        ),
    )
    return data


def write_csv(
    path: Path, rows: Sequence[EdgeRow], run_id: str, fetched_at: str, min_edge_pct: float
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=_sort_key)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in ordered:
            writer.writerow(row_record(row, run_id, fetched_at, min_edge_pct))
    return path


def write_json(
    path: Path,
    rows: Sequence[EdgeRow],
    run_id: str,
    fetched_at: str,
    min_edge_pct: float,
    meta: dict[str, Any],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "fetched_at": fetched_at,
        "meta": meta,
        "bets": [
            row_record(r, run_id, fetched_at, min_edge_pct)
            for r in sorted(rows, key=_sort_key)
        ],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def change_table(changes: Sequence[Any], color: bool = False, game_width: int = 28) -> str:
    """The --watch delta view: what moved since the last scan."""
    headers = [
        Column("CHANGE"), Column("GAME"), Column("KICKOFF (ET)"), Column("MARKET"),
        Column("PICK"), Column("DK", "right"), Column("SHARP", "right"),
        Column("LINE DIFF", "right"), Column("FAIR%", "right"), Column("EDGE", "right"),
        Column("EV/$100", "right"), Column("STAKE", "right"),
    ]
    tint = {"new": "green", "dropped": "dim", "number": "yellow"}
    body = []
    for change in changes:
        row = change.row
        body.append([
            _colorize(change.description, tint.get(change.kind, "bold"), color),
            _truncate(row.matchup, game_width),
            kickoff_et(row.commence_time),
            row.market_label,
            _truncate(row.pick, 24),
            format_american(row.dk_price),
            _sharp_cell(row),
            _line_diff(row.line_diff, color),
            _pct(row.fair_prob),
            _pct(row.edge),
            _money(row.ev_per_100),
            f"${row.stake:,.2f}" if row.stake is not None else "-",
        ])
    return render_table(headers, body)
