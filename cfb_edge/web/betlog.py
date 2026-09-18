"""The shared bet log: what the group actually put money on, and how it went.

Anyone with the dashboard password can add a bet and settle it later, so this
is a shared notebook rather than a set of accounts. It lives in the same SQLite
file as the scan history, which is what lets it answer the only question that
matters before the results come in: did you beat the closing number.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from cfb_edge.edges import format_pick
from cfb_edge.models import market_label
from cfb_edge.oddsmath import OddsError, american_to_decimal, american_to_prob, format_american
from cfb_edge.report import kickoff_et
from cfb_edge.store import init_db

OPEN = "open"
WON = "won"
LOST = "lost"
PUSH = "push"
RESULTS = (WON, LOST, PUSH, OPEN)

MAX_NAME = 40
MAX_STAKE = 1_000_000.0


class BetLogError(ValueError):
    """Something in the submitted bet does not make sense."""


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise BetLogError("who placed it?")
    return name[:MAX_NAME]


def _number(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise BetLogError(f"{field} must be a number") from None


def game_details(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT home_team, away_team, MAX(commence_time_utc) AS commence_time_utc
        FROM observations WHERE event_id = ? GROUP BY event_id
        """,
        (event_id,),
    ).fetchone()


def add_bet(conn: sqlite3.Connection, payload: dict[str, Any]) -> int:
    """Record a placed bet. Returns its id."""
    init_db(conn)
    person = _clean_name(payload.get("person"))
    event_id = str(payload.get("event_id") or "").strip()
    market = str(payload.get("market") or "").strip()
    side = str(payload.get("side") or "").strip()
    if not (event_id and market and side):
        raise BetLogError("pick a game and a side")

    price = _number(payload.get("price"), "price")
    try:
        american_to_prob(price)
    except OddsError as exc:
        raise BetLogError(str(exc)) from None
    stake = _number(payload.get("stake"), "stake")
    if not 0 < stake <= MAX_STAKE:
        raise BetLogError("stake must be greater than zero")

    point = payload.get("point")
    point = None if point in (None, "") else _number(point, "point")

    placed = str(payload.get("placed_on") or "").strip()
    if placed:
        try:
            placed_at = _iso(datetime.fromisoformat(placed).replace(tzinfo=timezone.utc))
        except ValueError:
            raise BetLogError("date should look like 2026-09-20") from None
    else:
        placed_at = _iso(datetime.now(timezone.utc))

    details = game_details(conn, event_id)
    home = details["home_team"] if details else str(payload.get("home_team") or "")
    away = details["away_team"] if details else str(payload.get("away_team") or "")
    commence = details["commence_time_utc"] if details else payload.get("commence_time")

    cursor = conn.execute(
        """
        INSERT INTO bets (
            placed_at_utc, event_id, commence_time_utc, home_team, away_team, market,
            side, point, price, stake, book, fair_prob, edge_pct, note, person, result
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            placed_at, event_id, commence, home, away, market, side, point, price, stake,
            "draftkings", payload.get("fair_prob"), payload.get("edge_pct"),
            str(payload.get("note") or "")[:200], person, OPEN,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def settle(conn: sqlite3.Connection, bet_id: int, result: str) -> None:
    """Mark a bet won, lost, pushed, or back to open."""
    init_db(conn)
    if result not in RESULTS:
        raise BetLogError(f"result must be one of {', '.join(RESULTS)}")
    found = conn.execute("SELECT 1 FROM bets WHERE bet_id = ?", (bet_id,)).fetchone()
    if not found:
        raise BetLogError(f"no bet #{bet_id}")
    conn.execute(
        "UPDATE bets SET result = ?, settled_at_utc = ? WHERE bet_id = ?",
        (result, None if result == OPEN else _iso(datetime.now(timezone.utc)), bet_id),
    )
    conn.commit()


def closing_sharp_price(
    conn: sqlite3.Connection, event_id: str, market: str, side: str
) -> sqlite3.Row | None:
    """The sharp book's last price on this side before kickoff."""
    return conn.execute(
        """
        SELECT sharp_price, sharp_source, fair_prob, observed_at_utc, dk_point, sharp_point
        FROM observations
        WHERE event_id = ? AND market = ? AND side = ?
          AND observed_at_utc <= commence_time_utc
          AND sharp_price IS NOT NULL
        ORDER BY observed_at_utc DESC LIMIT 1
        """,
        (event_id, market, side),
    ).fetchone()


def profit(price: float, stake: float, result: str) -> float | None:
    """What the bet returned, excluding the stake itself."""
    if result == WON:
        return stake * (american_to_decimal(price) - 1.0)
    if result == LOST:
        return -stake
    if result == PUSH:
        return 0.0
    return None


def clv_points(bet_price: float, closing_price: float) -> float | None:
    """Points of implied probability your price beat the close by."""
    try:
        yours = american_to_prob(bet_price)
        close = american_to_prob(closing_price)
    except OddsError:
        return None
    return (close - yours) * 100.0


def serialize_bet(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    result = row["result"] or OPEN
    close = closing_sharp_price(conn, row["event_id"], row["market"], row["side"])
    clv = clv_points(row["price"], close["sharp_price"]) if close else None
    kickoff = row["commence_time_utc"]
    return {
        "bet_id": row["bet_id"],
        "person": row["person"] or "?",
        "matchup": f"{row['away_team']} @ {row['home_team']}".strip(" @"),
        "event_id": row["event_id"],
        "market": row["market"],
        "market_label": market_label(row["market"]),
        "pick": format_pick(row["market"], row["side"], row["point"]),
        "price": format_american(row["price"]),
        "price_value": row["price"],
        "stake": row["stake"],
        "result": result,
        "profit": profit(row["price"], row["stake"], result),
        "placed_at_utc": row["placed_at_utc"],
        "placed_on": (row["placed_at_utc"] or "")[:10],
        "kickoff_utc": kickoff,
        "kickoff_et": kickoff_et(datetime.fromisoformat(kickoff.replace("Z", "+00:00")))
        if kickoff else None,
        "closing_price": format_american(close["sharp_price"]) if close else None,
        "closing_source": close["sharp_source"] if close else None,
        "clv_pct": None if clv is None else round(clv, 2),
    }


def list_bets(conn: sqlite3.Connection, limit: int = 500) -> list[dict[str, Any]]:
    init_db(conn)
    rows = conn.execute(
        "SELECT * FROM bets ORDER BY placed_at_utc DESC, bet_id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [serialize_bet(conn, row) for row in rows]


def _tally(bets: Sequence[dict[str, Any]]) -> dict[str, Any]:
    settled = [b for b in bets if b["result"] in (WON, LOST, PUSH)]
    staked = sum(b["stake"] for b in settled if b["result"] != PUSH)
    earned = sum(b["profit"] or 0.0 for b in settled)
    scored = [b["clv_pct"] for b in bets if b["clv_pct"] is not None]
    return {
        "bets": len(bets),
        "open": sum(1 for b in bets if b["result"] == OPEN),
        "won": sum(1 for b in settled if b["result"] == WON),
        "lost": sum(1 for b in settled if b["result"] == LOST),
        "push": sum(1 for b in settled if b["result"] == PUSH),
        "staked": round(staked, 2),
        "profit": round(earned, 2),
        "roi_pct": round(earned / staked * 100.0, 2) if staked else None,
        "avg_clv_pct": round(sum(scored) / len(scored), 2) if scored else None,
        "beat_close": sum(1 for c in scored if c > 0),
        "scored": len(scored),
    }


def summarize(bets: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The group's record, and each person's."""
    people: dict[str, list[dict[str, Any]]] = {}
    for bet in bets:
        people.setdefault(bet["person"], []).append(bet)
    breakdown = [{"person": name, **_tally(theirs)} for name, theirs in people.items()]
    # Most profitable first; anyone with nothing settled sorts to the bottom.
    breakdown.sort(key=lambda p: (-(p["profit"] or 0.0), p["person"].lower()))
    return {"overall": _tally(bets), "people": breakdown}


def log_payload(conn: sqlite3.Connection, limit: int = 500) -> dict[str, Any]:
    bets = list_bets(conn, limit=limit)
    return {"bets": bets, **summarize(bets)}


def known_people(bets: Iterable[dict[str, Any]]) -> list[str]:
    return sorted({b["person"] for b in bets if b["person"]})
