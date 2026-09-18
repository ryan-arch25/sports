"""Log the bets you placed, and score them against the closing line."""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from cfb_edge.config import Config
from cfb_edge.edges import format_pick
from cfb_edge.models import market_label, parse_commence_time
from cfb_edge.oddsmath import OddsError, american_to_prob, format_american
from cfb_edge.report import Column, kickoff_et, render_table
from cfb_edge.store import connect, init_db


class BetError(RuntimeError):
    pass


@dataclass
class ClvRow:
    bet_id: int
    placed_at: str
    matchup: str
    commence_time: str | None
    market: str
    side: str
    point: float | None
    price: float
    stake: float
    book: str
    closing_price: float | None
    closing_point: float | None
    closing_at: str | None
    clv_pct: float | None
    clv_adjusted_pct: float | None
    note: str

    @property
    def pick(self) -> str:
        return format_pick(self.market, self.side, self.point)

    @property
    def beat_close(self) -> bool | None:
        value = self.clv_adjusted_pct if self.clv_adjusted_pct is not None else self.clv_pct
        return None if value is None else value > 0


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_event(conn: sqlite3.Connection, text: str) -> sqlite3.Row:
    """Find the game a bet belongs to, by event id or by team name."""
    exact = conn.execute(
        """
        SELECT event_id, home_team, away_team, MAX(commence_time_utc) AS commence_time_utc
        FROM observations WHERE event_id = ? GROUP BY event_id
        """,
        (text,),
    ).fetchone()
    if exact:
        return exact

    like = f"%{text.lower()}%"
    matches = conn.execute(
        """
        SELECT event_id, home_team, away_team, MAX(commence_time_utc) AS commence_time_utc
        FROM observations
        WHERE LOWER(home_team) LIKE ? OR LOWER(away_team) LIKE ?
        GROUP BY event_id
        ORDER BY commence_time_utc
        """,
        (like, like),
    ).fetchall()
    if not matches:
        raise BetError(
            f"no game in the run log matches {text!r}. "
            "Run a scan first so the game is on file, or pass the event id."
        )
    if len(matches) > 1:
        options = "\n".join(
            f"  {m['event_id']}  {m['away_team']} @ {m['home_team']}" for m in matches
        )
        raise BetError(f"{text!r} matches more than one game:\n{options}")
    return matches[0]


def last_observation(
    conn: sqlite3.Connection, event_id: str, market: str, side: str, point: float | None
) -> sqlite3.Row | None:
    """The tool's most recent read on this exact line, for reference."""
    if point is None:
        return conn.execute(
            """
            SELECT * FROM observations
            WHERE event_id = ? AND market = ? AND side = ? AND dk_point IS NULL
            ORDER BY observed_at_utc DESC LIMIT 1
            """,
            (event_id, market, side),
        ).fetchone()
    return conn.execute(
        """
        SELECT * FROM observations
        WHERE event_id = ? AND market = ? AND side = ? AND ABS(dk_point - ?) < 1e-9
        ORDER BY observed_at_utc DESC LIMIT 1
        """,
        (event_id, market, side, point),
    ).fetchone()


def match_side(conn: sqlite3.Connection, event_id: str, market: str, side: str) -> str:
    """Accept 'georgia' for 'Georgia Bulldogs', and 'over' for 'Over'."""
    rows = conn.execute(
        "SELECT DISTINCT side FROM observations WHERE event_id = ? AND market = ?",
        (event_id, market),
    ).fetchall()
    known = [row["side"] for row in rows]
    if side in known:
        return side
    lowered = side.strip().lower()
    hits = [name for name in known if name.lower() == lowered]
    hits = hits or [name for name in known if lowered in name.lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise BetError(f"{side!r} matches several sides: {', '.join(hits)}")
    if known:
        raise BetError(f"{side!r} is not a side of this {market}. Known: {', '.join(known)}")
    return side


def add_bet(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    init_db(conn)
    event = resolve_event(conn, args.event)
    event_id = event["event_id"]
    side = match_side(conn, event_id, args.market, args.side)

    try:
        american_to_prob(args.price)
    except OddsError as exc:
        raise BetError(f"bad price: {exc}") from exc
    if args.stake <= 0:
        raise BetError("stake must be greater than zero")

    placed_at = _iso(
        parse_commence_time(args.placed_at) if args.placed_at else datetime.now(timezone.utc)
    )
    reference = last_observation(conn, event_id, args.market, side, args.point)
    cursor = conn.execute(
        """
        INSERT INTO bets (
            placed_at_utc, event_id, commence_time_utc, home_team, away_team, market,
            side, point, price, stake, book, fair_prob, edge_pct, note
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            placed_at, event_id, event["commence_time_utc"], event["home_team"],
            event["away_team"], args.market, side, args.point, args.price, args.stake,
            args.book or "draftkings", reference["fair_prob"] if reference else None,
            reference["edge_pct"] if reference else None, args.note,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def closing_line(
    conn: sqlite3.Connection, event_id: str, market: str, side: str
) -> sqlite3.Row | None:
    """Last observation of this side before kickoff."""
    return conn.execute(
        """
        SELECT * FROM observations
        WHERE event_id = ? AND market = ? AND side = ?
          AND observed_at_utc <= commence_time_utc
        ORDER BY observed_at_utc DESC LIMIT 1
        """,
        (event_id, market, side),
    ).fetchone()


def clv_rows(
    conn: sqlite3.Connection, cfg: Config, include_open: bool = False, limit: int = 100
) -> list[ClvRow]:
    init_db(conn)
    from cfb_edge.scan import load_halfpoint

    tables, _, _ = load_halfpoint(cfg)
    now = _iso(datetime.now(timezone.utc))
    sql = "SELECT * FROM bets"
    params: tuple = ()
    if not include_open:
        sql += " WHERE commence_time_utc IS NOT NULL AND commence_time_utc <= ?"
        params = (now,)
    sql += " ORDER BY placed_at_utc DESC LIMIT ?"
    params = params + (limit,)

    rows: list[ClvRow] = []
    for bet in conn.execute(sql, params):
        close = closing_line(conn, bet["event_id"], bet["market"], bet["side"])
        clv = adjusted = None
        note = ""
        closing_price = close["dk_price"] if close else None
        closing_point = close["dk_point"] if close else None
        if close is None:
            note = "no closing observation"
        else:
            try:
                bet_prob = american_to_prob(bet["price"])
                close_prob = american_to_prob(closing_price)
                clv = (close_prob - bet_prob) * 100.0
            except OddsError:
                note = "unreadable price"
            if clv is not None and not _same_number(bet["point"], closing_point):
                note = (
                    f"closed on {closing_point:g}, bet was {bet['point']:g}"
                    if bet["point"] is not None
                    else "the number moved"
                )
                adjusted = _adjust_for_number(tables, bet, close, close_prob)
            if bet["book"] and bet["book"] != "draftkings":
                note = (note + "; " if note else "") + f"priced against DK close, bet at {bet['book']}"
        rows.append(ClvRow(
            bet_id=bet["bet_id"], placed_at=bet["placed_at_utc"],
            matchup=f"{bet['away_team']} @ {bet['home_team']}",
            commence_time=bet["commence_time_utc"], market=bet["market"], side=bet["side"],
            point=bet["point"], price=bet["price"], stake=bet["stake"],
            book=bet["book"] or "draftkings", closing_price=closing_price,
            closing_point=closing_point, closing_at=close["observed_at_utc"] if close else None,
            clv_pct=clv, clv_adjusted_pct=adjusted, note=note,
        ))
    return rows


def _adjust_for_number(tables, bet: sqlite3.Row, close: sqlite3.Row, close_prob: float) -> float | None:
    """Move the closing price onto the number you actually bet, when possible."""
    from cfb_edge.edges import as_tables

    tables = as_tables(tables)
    if not tables or bet["point"] is None or close["dk_point"] is None:
        return None
    from cfb_edge.halfpoint import market_kind, side_role

    kind = market_kind(bet["market"])
    if kind is None:
        return None
    role = side_role(kind, bet["side"], close["dk_point"])
    if role is None:
        return None
    moved = None
    for table in tables:
        moved = table.translate(
            kind, role, close_prob, float(close["dk_point"]), float(bet["point"])
        )
        if moved is not None:
            break
    if moved is None:
        return None
    try:
        return (moved - american_to_prob(bet["price"])) * 100.0
    except OddsError:
        return None


def _same_number(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < 1e-9


def cmd_bets_add(cfg: Config, args: argparse.Namespace) -> int:
    conn = connect(cfg.db_path)
    try:
        bet_id = add_bet(conn, args)
        bet = conn.execute("SELECT * FROM bets WHERE bet_id = ?", (bet_id,)).fetchone()
    except BetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    pick = format_pick(bet["market"], bet["side"], bet["point"])
    print(f"logged bet #{bet_id}: {pick} {format_american(bet['price'])} "
          f"for ${bet['stake']:,.2f} at {bet['book']}")
    print(f"  {bet['away_team']} @ {bet['home_team']}")
    print(f"  your implied probability: {american_to_prob(bet['price']) * 100:.1f}%")
    if bet["fair_prob"] is not None:
        print(f"  fair at the time of the last scan: {bet['fair_prob'] * 100:.1f}%"
              + (f" (edge {bet['edge_pct']:.2f}%)" if bet["edge_pct"] is not None else ""))
    else:
        print("  no matching line in the run log, so no fair price was attached")
    return 0


def cmd_bets_list(cfg: Config, args: argparse.Namespace) -> int:
    conn = connect(cfg.db_path)
    try:
        init_db(conn)
        sql = "SELECT * FROM bets"
        params: tuple = ()
        if args.open:
            sql += " WHERE commence_time_utc > ?"
            params = (_iso(datetime.now(timezone.utc)),)
        sql += " ORDER BY placed_at_utc DESC LIMIT ?"
        params = params + (args.limit,)
        bets = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    if not bets:
        print("no bets logged yet. add one with: cfb-edge bets add --event ... ")
        return 0

    headers = [
        Column("#", "right"), Column("PLACED (ET)"), Column("GAME"), Column("MARKET"),
        Column("PICK"), Column("PRICE", "right"), Column("STAKE", "right"),
        Column("BOOK"), Column("EDGE", "right"),
    ]
    body = [
        [
            str(b["bet_id"]),
            kickoff_et(parse_commence_time(b["placed_at_utc"])),
            f"{b['away_team']} @ {b['home_team']}"[:30],
            market_label(b["market"]),
            format_pick(b["market"], b["side"], b["point"])[:26],
            format_american(b["price"]),
            f"${b['stake']:,.2f}",
            b["book"] or "-",
            f"{b['edge_pct']:.2f}%" if b["edge_pct"] is not None else "-",
        ]
        for b in bets
    ]
    print()
    print(render_table(headers, body))
    print()
    print(f"{len(bets)} bet(s), ${sum(b['stake'] for b in bets):,.2f} staked")
    return 0


def cmd_clv_report(cfg: Config, args: argparse.Namespace) -> int:
    conn = connect(cfg.db_path)
    try:
        rows = clv_rows(conn, cfg, include_open=args.all, limit=args.limit)
    finally:
        conn.close()

    if not rows:
        print("no settled bets to score yet (use --all to include games that have not kicked off)")
        return 0

    headers = [
        Column("#", "right"), Column("GAME"), Column("PICK"), Column("YOURS", "right"),
        Column("CLOSE", "right"), Column("CLV", "right"), Column("ADJ CLV", "right"),
        Column("STAKE", "right"), Column("NOTE"),
    ]
    body = []
    for row in rows:
        body.append([
            str(row.bet_id),
            row.matchup[:28],
            row.pick[:24],
            format_american(row.price),
            format_american(row.closing_price) + (
                f" @{row.closing_point:g}" if row.closing_point is not None
                and not _same_number(row.point, row.closing_point) else ""
            ),
            f"{row.clv_pct:+.2f}%" if row.clv_pct is not None else "-",
            f"{row.clv_adjusted_pct:+.2f}%" if row.clv_adjusted_pct is not None else "-",
            f"${row.stake:,.2f}",
            row.note[:60],
        ])
    print()
    print(render_table(headers, body))

    scored = [r for r in rows if r.clv_pct is not None]
    if scored:
        beat = sum(1 for r in scored if r.beat_close)
        average = sum(
            (r.clv_adjusted_pct if r.clv_adjusted_pct is not None else r.clv_pct) for r in scored
        ) / len(scored)
        print()
        print(
            f"{len(scored)} scored bet(s) | beat the close {beat}/{len(scored)} "
            f"({beat / len(scored):.0%}) | average CLV {average:+.2f} points of probability"
        )
        print("CLV is the closing implied probability minus yours; positive means the "
              "market moved your way.")
    if args.csv:
        _write_clv_csv(Path(args.csv), rows)
        print(f"wrote {args.csv}")
    print()
    return 0


def _write_clv_csv(path: Path, rows: Sequence[ClvRow]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "bet_id", "placed_at", "matchup", "commence_time", "market", "side", "pick",
        "point", "price", "stake", "book", "closing_price", "closing_point", "closing_at",
        "clv_pct", "clv_adjusted_pct", "beat_close", "note",
    ]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            record: dict[str, Any] = dict(row.__dict__)
            record["pick"] = row.pick
            record["beat_close"] = row.beat_close
            writer.writerow(record)
    return path
