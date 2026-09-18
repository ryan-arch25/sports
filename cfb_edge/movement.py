"""Line movement: what the board was showing before, and what changed.

Everything here reads and writes `line_history`, which holds one row per
*change* rather than one per scan. A line nobody touched all week is a single
row; a line that moves three times is three. That is what makes it cheap enough
to consult on every page load.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from cfb_edge.config import Config
from cfb_edge.edges import EdgeRow
from cfb_edge.oddsmath import OddsError, american_to_prob, format_american
from cfb_edge.report import ET
from cfb_edge.store import HistoryKey, LineSnapshot, baseline_lines

UP = "up"
DOWN = "down"


def sharp_book_name(row: EdgeRow) -> str:
    """The sharp side's book, without the consensus count.

    `sharp_source` reads "consensus(3)", and the 3 changes whenever a book
    stops posting. Keeping the count in the key would make an unchanged number
    look like a brand new line every time that happened.
    """
    return (row.sharp_source or "").split("(")[0].strip() or "sharp"


def snapshots(
    rows: Iterable[EdgeRow], cfg: Config, markets: Sequence[str] | None = None
) -> list[LineSnapshot]:
    """Both books' current offers on every side, ready to be recorded."""
    wanted = set(markets if markets is not None else cfg.history_markets)
    out: list[LineSnapshot] = []
    for row in rows:
        if row.market not in wanted:
            continue
        out.append(LineSnapshot(
            event_id=row.event_id, market=row.market, side=row.side,
            book=cfg.target_book, point=row.dk_point, price=row.dk_price,
        ))
        if row.sharp_price is not None:
            out.append(LineSnapshot(
                event_id=row.event_id, market=row.market, side=row.side,
                book=sharp_book_name(row), point=row.sharp_point, price=row.sharp_price,
            ))
    return out


@dataclass(frozen=True)
class Move:
    """How one line differs from what it was showing at the cutoff."""

    previous_point: float | None
    previous_price: float | None
    previous_at_utc: str
    direction: str

    def as_dict(self, market: str) -> dict[str, Any]:
        from cfb_edge.web.slate import number_text

        return {
            "direction": self.direction,
            "previous_number": number_text(market, self.previous_point),
            "previous_price": (
                "" if self.previous_price is None else format_american(self.previous_price)
            ),
            "previous_at_et": stamp_et(self.previous_at_utc),
        }


def stamp_et(value: str | None) -> str:
    """A recorded timestamp as the group reads it: 'Sat 2:15 PM'."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(ET)
    return f"{local:%a} {local.hour % 12 or 12}:{local:%M %p}"


def _price_direction(previous: float | None, current: float | None) -> str:
    """Up means the price got longer, which is the side of it a bettor wants."""
    try:
        if previous is None or current is None:
            return UP
        return UP if american_to_prob(current) < american_to_prob(previous) else DOWN
    except OddsError:
        return UP


def _same(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < 1e-6


def move_for(
    baseline: tuple[float | None, float | None, str] | None,
    point: float | None,
    price: float | None,
) -> Move | None:
    """Compare a live line against its baseline. None means it has not moved.

    The live value comes from the scan rather than from the history table, so a
    scan whose history write failed shows no arrow instead of a wrong one.
    """
    if baseline is None:
        return None
    previous_point, previous_price, previous_at = baseline
    if not _same(previous_point, point):
        direction = UP if (point or 0.0) > (previous_point or 0.0) else DOWN
    elif not _same(previous_price, price):
        direction = _price_direction(previous_price, price)
    else:
        return None
    return Move(previous_point, previous_price, previous_at, direction)


def moves(
    conn,
    rows: Iterable[EdgeRow],
    cfg: Config,
    now: datetime | None = None,
) -> dict[HistoryKey, Move]:
    """Every line that has moved since the arrow cutoff, keyed as history is."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg.history_arrow_hours)
    baseline = baseline_lines(conn, cutoff)
    if not baseline:
        return {}
    out: dict[HistoryKey, Move] = {}
    for snapshot in snapshots(rows, cfg):
        move = move_for(baseline.get(snapshot.key), snapshot.point, snapshot.price)
        if move is not None:
            out[snapshot.key] = move
    return out


def serialize_moves(
    found: dict[HistoryKey, Move], cfg: Config
) -> dict[tuple[str, str, str], dict[str, dict[str, Any]]]:
    """Reshape for the slate: (event, market, side) -> {"dk": ..., "sharp": ...}."""
    out: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    for (event_id, market, side, book), move in found.items():
        role = "dk" if book == cfg.target_book else "sharp"
        out.setdefault((event_id, market, side), {})[role] = move.as_dict(market)
    return out


# -- the expandable panel -------------------------------------------------


def history_payload(
    conn,
    cfg: Config,
    event_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Every recorded change for one game inside the display window.

    One entry per change, newest first, which is exactly what is in the table:
    there is no re-sampling or gap filling, because a row here means a book
    actually moved and a gap means it did not.
    """
    from cfb_edge.models import market_label
    from cfb_edge.shop import book_name
    from cfb_edge.store import first_seen_ids, line_history
    from cfb_edge.web.slate import number_text

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=cfg.history_window_hours)
    # The first row for a line is where the board opened, not somewhere it
    # moved to. Saying otherwise would make the first scan after a deploy look
    # like the whole slate moved at once.
    opened = first_seen_ids(conn, event_id)
    changes = []
    for row in line_history(conn, event_id, since, cfg.history_markets):
        market = row["market"]
        changes.append({
            "opened": row["id"] in opened,
            "at_et": stamp_et(row["recorded_at_utc"]),
            "at_utc": row["recorded_at_utc"],
            "market": market,
            "market_label": market_label(market),
            "side": row["side"],
            "book": row["book"],
            "book_label": book_name(row["book"]),
            "number": number_text(market, row["point"]),
            "price": "" if row["price"] is None else format_american(row["price"]),
        })
    return {
        "event_id": event_id,
        "window_hours": cfg.history_window_hours,
        "changes": changes,
        "count": len(changes),
    }
