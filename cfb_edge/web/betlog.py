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

from cfb_edge.edges import format_pick, translate_prob
from cfb_edge.models import market_label
from cfb_edge.oddsmath import (
    OddsError,
    american_to_decimal,
    american_to_prob,
    format_american,
    prob_to_american,
)
from cfb_edge.report import kickoff_et
from cfb_edge.store import init_db

OPEN = "open"
WON = "won"
LOST = "lost"
PUSH = "push"
RESULTS = (WON, LOST, PUSH, OPEN)

SINGLE = "single"
PARLAY = "parlay"
BET_TYPES = (SINGLE, PARLAY)
# What a parlay's own row carries in the market column, so it is obvious in the
# database that the selections live in bet_legs rather than on the row.
PARLAY_MARKET = "parlay"
MIN_LEGS = 2
MAX_LEGS = 10

# Markets a leg may name. Anything else is a typo or a market this tool has no
# closing line for, and silently accepting it would produce a bet nobody can
# score later.
LOGGABLE_MARKETS = ("spreads", "totals", "team_totals", "h2h")

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


def _placed_at(payload: dict[str, Any]) -> str:
    placed = str(payload.get("placed_on") or "").strip()
    if not placed:
        return _iso(datetime.now(timezone.utc))
    try:
        return _iso(datetime.fromisoformat(placed).replace(tzinfo=timezone.utc))
    except ValueError:
        raise BetLogError("date should look like 2026-09-20") from None


def _price(value: Any, field: str = "price") -> float:
    price = _number(value, field)
    try:
        american_to_prob(price)
    except OddsError as exc:
        raise BetLogError(str(exc)) from None
    return price


def _stake(value: Any) -> float:
    stake = _number(value, "stake")
    if not 0 < stake <= MAX_STAKE:
        raise BetLogError("stake must be greater than zero")
    return stake


def _point(value: Any) -> float | None:
    return None if value in (None, "") else _number(value, "line")


def clean_selection(conn: sqlite3.Connection, raw: dict[str, Any]) -> dict[str, Any]:
    """Validate one selection -- a single bet's pick, or one leg of a parlay.

    The same check for both, because a custom alt spread should be as hard to
    mistype in a parlay as on its own.
    """
    event_id = str(raw.get("event_id") or "").strip()
    market = str(raw.get("market") or "").strip()
    side = str(raw.get("side") or "").strip()
    if not (event_id and market and side):
        raise BetLogError("each pick needs a game, a market and a side")
    if market not in LOGGABLE_MARKETS:
        raise BetLogError(f"market must be one of {', '.join(LOGGABLE_MARKETS)}")
    point = _point(raw.get("point"))
    if market == "h2h":
        point = None  # a moneyline has no number; one here would break the lookup
    elif point is None:
        raise BetLogError(f"a {market_label(market).lower()} needs a line number")

    details = game_details(conn, event_id)
    return {
        "event_id": event_id,
        "market": market,
        "side": side,
        "point": point,
        "home_team": details["home_team"] if details else str(raw.get("home_team") or ""),
        "away_team": details["away_team"] if details else str(raw.get("away_team") or ""),
        "commence_time_utc": (
            details["commence_time_utc"] if details else raw.get("commence_time")
        ),
    }


def add_bet(conn: sqlite3.Connection, payload: dict[str, Any]) -> int:
    """Record a placed bet, single or parlay. Returns its id."""
    init_db(conn)
    bet_type = str(payload.get("bet_type") or SINGLE).strip().lower()
    if bet_type not in BET_TYPES:
        raise BetLogError(f"type must be one of {', '.join(BET_TYPES)}")
    if bet_type == PARLAY:
        return _add_parlay(conn, payload)
    return _add_single(conn, payload)


def _insert_ticket(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    person: str,
    bet_type: str,
    selection: dict[str, Any] | None,
    price: float,
    stake: float,
    placed_at: str,
) -> int:
    """The bets row. A parlay's own row carries the ticket, not a selection."""
    blank = {"event_id": "", "market": PARLAY_MARKET, "side": "", "point": None,
             "home_team": "", "away_team": "", "commence_time_utc": None}
    pick = selection or blank
    cursor = conn.execute(
        """
        INSERT INTO bets (
            placed_at_utc, event_id, commence_time_utc, home_team, away_team, market,
            side, point, price, stake, book, fair_prob, edge_pct, note, person, result,
            bet_type
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            placed_at, pick["event_id"], pick["commence_time_utc"], pick["home_team"],
            pick["away_team"], pick["market"], pick["side"], pick["point"], price, stake,
            "draftkings", payload.get("fair_prob"), payload.get("edge_pct"),
            str(payload.get("note") or "")[:200], person, OPEN, bet_type,
        ),
    )
    return int(cursor.lastrowid)


def _add_single(conn: sqlite3.Connection, payload: dict[str, Any]) -> int:
    person = _clean_name(payload.get("person"))
    selection = clean_selection(conn, payload)
    bet_id = _insert_ticket(
        conn, payload,
        person=person, bet_type=SINGLE, selection=selection,
        price=_price(payload.get("price")), stake=_stake(payload.get("stake")),
        placed_at=_placed_at(payload),
    )
    conn.commit()
    return bet_id


def _add_parlay(conn: sqlite3.Connection, payload: dict[str, Any]) -> int:
    person = _clean_name(payload.get("person"))
    raw_legs = payload.get("legs")
    if not isinstance(raw_legs, list):
        raise BetLogError("a parlay needs a list of legs")
    if not MIN_LEGS <= len(raw_legs) <= MAX_LEGS:
        raise BetLogError(f"a parlay needs between {MIN_LEGS} and {MAX_LEGS} legs")

    legs = []
    for raw in raw_legs:
        if not isinstance(raw, dict):
            raise BetLogError("each leg must be an object")
        selection = clean_selection(conn, raw)
        # A leg's own price is what lets a pushed leg be divided back out of
        # the combined price later, so it is required rather than optional.
        selection["price"] = _price(raw.get("price"), "leg price")
        legs.append(selection)

    seen = {(leg["event_id"], leg["market"], leg["side"]) for leg in legs}
    if len(seen) != len(legs):
        raise BetLogError("the same selection appears twice in this parlay")

    bet_id = _insert_ticket(
        conn, payload,
        person=person, bet_type=PARLAY, selection=None,
        price=_price(payload.get("price"), "combined price"),
        stake=_stake(payload.get("stake")), placed_at=_placed_at(payload),
    )
    conn.executemany(
        """
        INSERT INTO bet_legs (
            bet_id, leg_no, event_id, commence_time_utc, home_team, away_team,
            market, side, point, price, result
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (bet_id, index, leg["event_id"], leg["commence_time_utc"], leg["home_team"],
             leg["away_team"], leg["market"], leg["side"], leg["point"], leg["price"], OPEN)
            for index, leg in enumerate(legs, start=1)
        ],
    )
    conn.commit()
    return bet_id


def leg_rows(conn: sqlite3.Connection, bet_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM bet_legs WHERE bet_id = ? ORDER BY leg_no", (bet_id,)
    ).fetchall()


def _bet_row(conn: sqlite3.Connection, bet_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM bets WHERE bet_id = ?", (bet_id,)).fetchone()
    if row is None:
        raise BetLogError(f"no bet #{bet_id}")
    return row


def derive_ticket_result(results: Sequence[str]) -> str:
    """A parlay's result from its legs, the way a sportsbook grades one.

    One losing leg loses the ticket immediately, whatever the rest do. An open
    leg leaves the ticket open. Pushes drop out, so a ticket whose legs all
    pushed is itself a push and anything else is a win.
    """
    if not results:
        return OPEN
    if LOST in results:
        return LOST
    if OPEN in results:
        return OPEN
    if all(result == PUSH for result in results):
        return PUSH
    return WON


def _set_ticket_result(conn: sqlite3.Connection, bet_id: int, result: str) -> None:
    conn.execute(
        "UPDATE bets SET result = ?, settled_at_utc = ? WHERE bet_id = ?",
        (result, None if result == OPEN else _iso(datetime.now(timezone.utc)), bet_id),
    )


def settle(conn: sqlite3.Connection, bet_id: int, result: str) -> None:
    """Mark a bet won, lost, pushed, or back to open.

    A parlay's result is derived from its legs, so it is not settable directly
    -- except back to open, which reopens the whole ticket.
    """
    init_db(conn)
    if result not in RESULTS:
        raise BetLogError(f"result must be one of {', '.join(RESULTS)}")
    row = _bet_row(conn, bet_id)
    if (row["bet_type"] or SINGLE) == PARLAY:
        if result != OPEN:
            raise BetLogError("settle a parlay one leg at a time")
        conn.execute(
            "UPDATE bet_legs SET result = ?, settled_at_utc = NULL WHERE bet_id = ?",
            (OPEN, bet_id),
        )
    _set_ticket_result(conn, bet_id, result)
    conn.commit()


def settle_leg(conn: sqlite3.Connection, bet_id: int, leg_no: int, result: str) -> None:
    """Grade one leg of a parlay, then re-derive the ticket."""
    init_db(conn)
    if result not in RESULTS:
        raise BetLogError(f"result must be one of {', '.join(RESULTS)}")
    row = _bet_row(conn, bet_id)
    if (row["bet_type"] or SINGLE) != PARLAY:
        raise BetLogError(f"bet #{bet_id} is not a parlay")
    found = conn.execute(
        "SELECT 1 FROM bet_legs WHERE bet_id = ? AND leg_no = ?", (bet_id, leg_no)
    ).fetchone()
    if not found:
        raise BetLogError(f"no leg {leg_no} on bet #{bet_id}")
    conn.execute(
        "UPDATE bet_legs SET result = ?, settled_at_utc = ? WHERE bet_id = ? AND leg_no = ?",
        (result, None if result == OPEN else _iso(datetime.now(timezone.utc)),
         bet_id, leg_no),
    )
    legs = leg_rows(conn, bet_id)
    _set_ticket_result(conn, bet_id, derive_ticket_result([r["result"] or OPEN for r in legs]))
    conn.commit()


def closing_sharp_price(
    conn: sqlite3.Connection, event_id: str, market: str, side: str
) -> sqlite3.Row | None:
    """The sharp book's last price on this side before kickoff."""
    return conn.execute(
        """
        SELECT sharp_price, sharp_source, fair_prob, observed_at_utc, dk_point,
               sharp_point, status
        FROM observations
        WHERE event_id = ? AND market = ? AND side = ?
          AND observed_at_utc <= commence_time_utc
          AND sharp_price IS NOT NULL
        ORDER BY observed_at_utc DESC LIMIT 1
        """,
        (event_id, market, side),
    ).fetchone()


def _same_number(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < 1e-9


def closing_at(
    closing: sqlite3.Row | None,
    market: str,
    side: str,
    point: float | None,
    tables: Any = (),
) -> tuple[float | None, bool]:
    """The closing implied probability *at the number that was bet*.

    An alt line is not the market number, so comparing it to the closing price
    of a different number would be comparing two different bets. The half-point
    table moves the close onto the logged number instead, exactly as the Edges
    tab moves a sharp line onto DraftKings'.

    Returns (probability, whether it was estimated). A probability of None means
    the move could not be priced -- a market the table does not model, or a gap
    wider than it will cross -- and the caller shows a dash rather than a number
    that would be wrong.
    """
    if closing is None or closing["sharp_price"] is None:
        return None, False
    try:
        implied = american_to_prob(closing["sharp_price"])
    except OddsError:
        return None, False

    # fair_prob belongs to dk_point: for a row priced on the number they are
    # the same, and for a translated row it is already at DraftKings' number.
    anchor = closing["dk_point"]
    if _same_number(anchor, point):
        return implied, False

    fair = closing["fair_prob"]
    if fair is None or point is None or anchor is None:
        return None, False
    moved = translate_prob(tables, market, side, anchor, point, fair)
    if moved is None:
        return None, False
    shifted = implied + (moved[0] - fair)
    if not 0.0 < shifted < 1.0:
        return None, False
    return shifted, True


def profit(price: float, stake: float, result: str) -> float | None:
    """What the bet returned, excluding the stake itself."""
    if result == WON:
        return stake * (american_to_decimal(price) - 1.0)
    if result == LOST:
        return -stake
    if result == PUSH:
        return 0.0
    return None


def effective_decimal(price: float, legs: Sequence[dict[str, Any]]) -> float | None:
    """The combined price with any pushed legs divided back out.

    This is how a sportsbook reduces a parlay: the pushed leg comes out of the
    multiplication and the ticket pays at what is left. Dividing the combined
    price keeps whatever rounding or boost went into it, which recomputing from
    the legs would quietly discard.
    """
    decimal = american_to_decimal(price)
    for leg in legs:
        if leg["result"] != PUSH:
            continue
        if leg["price_value"] is None:
            return None  # nothing to divide out; better a dash than a wrong payout
        leg_decimal = american_to_decimal(leg["price_value"])
        if leg_decimal <= 0:
            return None
        decimal /= leg_decimal
    return decimal


def parlay_profit(
    price: float, stake: float, result: str, legs: Sequence[dict[str, Any]]
) -> float | None:
    if result != WON:
        return profit(price, stake, result)
    decimal = effective_decimal(price, legs)
    if decimal is None:
        return None
    return stake * (decimal - 1.0)


def clv_points(bet_price: float, closing_price: float) -> float | None:
    """Points of implied probability your price beat the close by."""
    try:
        yours = american_to_prob(bet_price)
        close = american_to_prob(closing_price)
    except OddsError:
        return None
    return (close - yours) * 100.0


def _kickoff_et(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return kickoff_et(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _score(
    conn: sqlite3.Connection,
    event_id: str,
    market: str,
    side: str,
    point: float | None,
    price: float | None,
    tables: Any = (),
) -> dict[str, Any]:
    """Closing line and CLV for one selection, on the number that was bet."""
    closing = closing_sharp_price(conn, event_id, market, side)
    prob, estimated = closing_at(closing, market, side, point, tables)
    if prob is None or price is None:
        return {
            "closing_price": None,
            "closing_source": closing["sharp_source"] if closing else None,
            "clv_pct": None,
            "clv_estimated": False,
        }
    closing_price = prob_to_american(prob)
    clv = clv_points(price, closing_price)
    return {
        "closing_price": format_american(closing_price),
        "closing_source": closing["sharp_source"] if closing else None,
        "clv_pct": None if clv is None else round(clv, 2),
        "clv_estimated": estimated,
    }


def serialize_leg(
    conn: sqlite3.Connection, leg: sqlite3.Row, tables: Any = ()
) -> dict[str, Any]:
    return {
        "leg_no": leg["leg_no"],
        "event_id": leg["event_id"],
        "matchup": f"{leg['away_team']} @ {leg['home_team']}".strip(" @"),
        "market": leg["market"],
        "market_label": market_label(leg["market"]),
        "pick": format_pick(leg["market"], leg["side"], leg["point"]),
        "side": leg["side"],
        "point": leg["point"],
        "price": format_american(leg["price"]) if leg["price"] is not None else None,
        "price_value": leg["price"],
        "result": leg["result"] or OPEN,
        "kickoff_utc": leg["commence_time_utc"],
        "kickoff_et": _kickoff_et(leg["commence_time_utc"]),
        **_score(conn, leg["event_id"], leg["market"], leg["side"], leg["point"],
                 leg["price"], tables),
    }


def serialize_bet(
    conn: sqlite3.Connection, row: sqlite3.Row, tables: Any = ()
) -> dict[str, Any]:
    bet_type = row["bet_type"] or SINGLE
    result = row["result"] or OPEN
    kickoff = row["commence_time_utc"]
    common = {
        "bet_id": row["bet_id"],
        "bet_type": bet_type,
        "person": row["person"] or "?",
        "event_id": row["event_id"],
        "price": format_american(row["price"]),
        "price_value": row["price"],
        "stake": row["stake"],
        "result": result,
        "placed_at_utc": row["placed_at_utc"],
        "placed_on": (row["placed_at_utc"] or "")[:10],
    }

    if bet_type == PARLAY:
        legs = [serialize_leg(conn, leg, tables) for leg in leg_rows(conn, row["bet_id"])]
        earliest = min((leg["kickoff_utc"] for leg in legs if leg["kickoff_utc"]), default=None)
        return {
            **common,
            "matchup": f"{len(legs)}-leg parlay",
            "market": PARLAY_MARKET,
            "market_label": "Parlay",
            "pick": " + ".join(leg["pick"] for leg in legs),
            "legs": legs,
            "profit": parlay_profit(row["price"], row["stake"], result, legs),
            "kickoff_utc": earliest,
            "kickoff_et": _kickoff_et(earliest),
            # A ticket has no closing line of its own. Its legs each have one,
            # and multiplying them would invent a number nobody could check.
            "closing_price": None,
            "closing_source": None,
            "clv_pct": None,
            "clv_estimated": False,
        }

    return {
        **common,
        "matchup": f"{row['away_team']} @ {row['home_team']}".strip(" @"),
        "market": row["market"],
        "market_label": market_label(row["market"]),
        "pick": format_pick(row["market"], row["side"], row["point"]),
        "side": row["side"],
        "point": row["point"],
        "legs": [],
        "profit": profit(row["price"], row["stake"], result),
        "kickoff_utc": kickoff,
        "kickoff_et": _kickoff_et(kickoff),
        **_score(conn, row["event_id"], row["market"], row["side"], row["point"],
                 row["price"], tables),
    }


def list_bets(
    conn: sqlite3.Connection, limit: int = 500, tables: Any = ()
) -> list[dict[str, Any]]:
    init_db(conn)
    rows = conn.execute(
        "SELECT * FROM bets ORDER BY placed_at_utc DESC, bet_id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [serialize_bet(conn, row, tables) for row in rows]


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


def log_payload(
    conn: sqlite3.Connection, limit: int = 500, tables: Any = ()
) -> dict[str, Any]:
    bets = list_bets(conn, limit=limit, tables=tables)
    return {"bets": bets, **summarize(bets), "markets": list(LOGGABLE_MARKETS)}


def known_people(bets: Iterable[dict[str, Any]]) -> list[str]:
    return sorted({b["person"] for b in bets if b["person"]})
