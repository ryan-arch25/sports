"""Compare DraftKings prices against the sharp fair line."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Sequence

from cfb_edge.config import Config
from cfb_edge.fair import FairLine, fair_lines, resolve_book
from cfb_edge.models import Game, market_label
from cfb_edge.oddsmath import (
    OddsError,
    american_to_prob,
    ev_per_100,
    kelly_stake,
    prob_to_american,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for typing
    from cfb_edge.halfpoint import HalfPointTable

# Row statuses
PRICED = "priced"
TRANSLATED = "translated"  # DK is on another number, priced via the half-point table
DIFFERENT_NUMBER = "different_number"
NO_SHARP_LINE = "no_sharp_line"
NO_SHARP_SIDE = "no_sharp_side"


@dataclass
class EdgeRow:
    event_id: str
    commence_time: datetime
    home_team: str
    away_team: str
    market: str
    side: str
    dk_point: float | None
    dk_price: float
    dk_prob: float | None
    sharp_source: str
    sharp_books: str
    sharp_point: float | None
    sharp_price: float | None
    sharp_hold: float | None
    fair_prob: float | None
    fair_american: float | None
    edge: float | None  # fair_prob - dk_prob, as a fraction
    ev_per_100: float | None
    stake: float | None
    status: str
    note: str = ""
    translated_from: float | None = None

    @property
    def matchup(self) -> str:
        return f"{self.away_team} @ {self.home_team}"

    @property
    def market_label(self) -> str:
        return market_label(self.market)

    @property
    def is_bet(self) -> bool:
        """A row carrying a real, comparable edge."""
        return self.status in (PRICED, TRANSLATED)

    @property
    def pick(self) -> str:
        return format_pick(self.market, self.side, self.dk_point)

    @property
    def edge_pct(self) -> float | None:
        return None if self.edge is None else self.edge * 100.0

    def as_dict(self) -> dict:
        data = asdict(self)
        data["commence_time"] = self.commence_time.isoformat().replace("+00:00", "Z")
        data["matchup"] = self.matchup
        data["pick"] = self.pick
        data["market_label"] = self.market_label
        data["edge_pct"] = self.edge_pct
        return data


def format_point(market: str, point: float | None) -> str:
    if point is None:
        return ""
    if market == "spreads":
        return f"{point:+g}"
    return f"{point:g}"


def format_pick(market: str, side: str, point: float | None) -> str:
    if market == "h2h":
        return f"{side} ML"
    suffix = format_point(market, point)
    return f"{side} {suffix}".strip()


def _empty_row(
    game: Game, market: str, side: str, point: float | None, price: float, status: str, note: str
) -> EdgeRow:
    return EdgeRow(
        event_id=game.event_id,
        commence_time=game.commence_time,
        home_team=game.home_team,
        away_team=game.away_team,
        market=market,
        side=side,
        dk_point=point,
        dk_price=price,
        dk_prob=american_to_prob(price),
        sharp_source="",
        sharp_books="",
        sharp_point=None,
        sharp_price=None,
        sharp_hold=None,
        fair_prob=None,
        fair_american=None,
        edge=None,
        ev_per_100=None,
        stake=None,
        status=status,
        note=note,
    )


def evaluate_market(
    game: Game,
    market: str,
    cfg: Config,
    table: "HalfPointTable | None" = None,
) -> list[EdgeRow]:
    """One row per DraftKings side in this market."""
    dk_key = resolve_book(game, cfg.target_book, cfg.aliases)
    dk_market = game.market(dk_key, market) if dk_key else None
    if dk_market is None:
        return []

    sharp_by_group = fair_lines(game, market, cfg)
    rows: list[EdgeRow] = []
    for outcome in dk_market.outcomes:
        try:
            dk_prob = american_to_prob(outcome.price)
        except OddsError:
            continue  # a price no book could post; nothing to compare
        sharp = sharp_by_group.get(outcome.group)
        if sharp is None:
            rows.append(
                _empty_row(
                    game, market, outcome.key, outcome.point, outcome.price,
                    NO_SHARP_LINE, "no sharp or consensus line available",
                )
            )
            continue
        rows.append(
            _row_against(
                game, market, outcome.key, outcome.point, outcome.price, dk_prob, sharp, cfg, table
            )
        )
    return rows


def _row_against(
    game: Game,
    market: str,
    side: str,
    dk_point: float | None,
    dk_price: float,
    dk_prob: float,
    sharp: FairLine,
    cfg: Config,
    table: "HalfPointTable | None" = None,
) -> EdgeRow:
    sharp_side = sharp.side(side)
    books = ",".join(sharp.books)
    if sharp_side is None:
        row = _empty_row(game, market, side, dk_point, dk_price, NO_SHARP_SIDE,
                         f"{sharp.label} has no matching side for {side!r}")
        row.sharp_source = sharp.label
        row.sharp_books = books
        row.sharp_hold = sharp.hold
        return row

    fair_prob = sharp_side.fair_prob
    status = PRICED
    note = ""
    translated_from = None

    if not _same_number(dk_point, sharp_side.point):
        moved = _translate(table, market, side, sharp_side.point, dk_point, fair_prob)
        if moved is None:
            row = _empty_row(
                game, market, side, dk_point, dk_price, DIFFERENT_NUMBER,
                f"DK {format_point(market, dk_point)} vs {sharp.label} "
                f"{format_point(market, sharp_side.point)}",
            )
            row.sharp_source = sharp.label
            row.sharp_books = books
            row.sharp_point = sharp_side.point
            row.sharp_price = sharp_side.price
            row.sharp_hold = sharp.hold
            return row
        status = TRANSLATED
        translated_from = sharp_side.point
        note = (
            f"half-point table moved {sharp.label} "
            f"{format_point(market, sharp_side.point)} ({fair_prob * 100:.1f}%) to DK "
            f"{format_point(market, dk_point)} ({moved * 100:.1f}%)"
        )
        fair_prob = moved

    return EdgeRow(
        event_id=game.event_id,
        commence_time=game.commence_time,
        home_team=game.home_team,
        away_team=game.away_team,
        market=market,
        side=side,
        dk_point=dk_point,
        dk_price=dk_price,
        dk_prob=dk_prob,
        sharp_source=sharp.label,
        sharp_books=books,
        sharp_point=sharp_side.point,
        sharp_price=sharp_side.price,
        sharp_hold=sharp.hold,
        fair_prob=fair_prob,
        fair_american=prob_to_american(fair_prob),
        edge=fair_prob - dk_prob,
        ev_per_100=ev_per_100(fair_prob, dk_price),
        stake=kelly_stake(
            fair_prob,
            dk_price,
            cfg.bankroll,
            fraction=cfg.kelly_fraction,
            max_bet_pct=cfg.max_bet_pct,
        ),
        status=status,
        note=note,
        translated_from=translated_from,
    )


def _translate(
    table: "HalfPointTable | None",
    market: str,
    side: str,
    sharp_point: float | None,
    dk_point: float | None,
    fair_prob: float,
) -> float | None:
    """Fair probability at DK's number, or None if it cannot be priced."""
    if table is None or sharp_point is None or dk_point is None:
        return None
    from cfb_edge.halfpoint import market_kind, side_role

    kind = market_kind(market)
    if kind is None:
        return None
    role = side_role(kind, side, sharp_point)
    if role is None:
        return None
    return table.translate(kind, role, fair_prob, float(sharp_point), float(dk_point))


def _same_number(a: float | None, b: float | None) -> bool:
    """Exact match on the line's number (moneylines have no number at all)."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < 1e-9


def evaluate_games(
    games: Sequence[Game],
    cfg: Config,
    markets: Sequence[str],
    table: "HalfPointTable | None" = None,
) -> list[EdgeRow]:
    rows: list[EdgeRow] = []
    for game in games:
        for market in markets:
            rows.extend(evaluate_market(game, market, cfg, table))
    return rows


def rank(rows: Sequence[EdgeRow], min_edge_pct: float) -> list[EdgeRow]:
    """Priced rows at or above the edge threshold, best edge first."""
    keep = [
        r for r in rows
        if r.is_bet and r.edge is not None and r.edge * 100.0 >= min_edge_pct
    ]
    keep.sort(key=lambda r: (-(r.edge or 0.0), r.commence_time, r.matchup))
    return keep


def different_number_rows(rows: Sequence[EdgeRow]) -> list[EdgeRow]:
    flagged = [r for r in rows if r.status == DIFFERENT_NUMBER]
    flagged.sort(key=lambda r: (r.commence_time, r.matchup, r.market, r.side))
    return flagged


def summarize(rows: Sequence[EdgeRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return counts


__all__ = [
    "EdgeRow",
    "PRICED",
    "TRANSLATED",
    "DIFFERENT_NUMBER",
    "NO_SHARP_LINE",
    "NO_SHARP_SIDE",
    "evaluate_games",
    "evaluate_market",
    "different_number_rows",
    "format_pick",
    "format_point",
    "rank",
    "summarize",
]
