"""Compare DraftKings prices against the sharp fair line."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Sequence

from cfb_edge.config import Config
from cfb_edge.fair import FairLine, fair_line, resolve_book
from cfb_edge.models import MARKET_LABELS, Game
from cfb_edge.oddsmath import (
    OddsError,
    american_to_prob,
    ev_per_100,
    kelly_stake,
    prob_to_american,
)

# Row statuses
PRICED = "priced"
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

    @property
    def matchup(self) -> str:
        return f"{self.away_team} @ {self.home_team}"

    @property
    def market_label(self) -> str:
        return MARKET_LABELS.get(self.market, self.market)

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


def evaluate_market(game: Game, market: str, cfg: Config) -> list[EdgeRow]:
    """One row per DraftKings side in this market."""
    dk_key = resolve_book(game, cfg.target_book, cfg.aliases)
    dk_market = game.market(dk_key, market) if dk_key else None
    if dk_market is None:
        return []

    sharp = fair_line(game, market, cfg)
    rows: list[EdgeRow] = []
    for outcome in dk_market.outcomes:
        try:
            dk_prob = american_to_prob(outcome.price)
        except OddsError:
            continue  # a price no book could post; nothing to compare
        if sharp is None:
            rows.append(
                _empty_row(
                    game, market, outcome.name, outcome.point, outcome.price,
                    NO_SHARP_LINE, "no sharp or consensus line available",
                )
            )
            continue
        rows.append(_row_against(game, market, outcome.name, outcome.point, outcome.price, dk_prob, sharp, cfg))
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

    if not _same_number(dk_point, sharp_side.point):
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

    fair_prob = sharp_side.fair_prob
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
        status=PRICED,
    )


def _same_number(a: float | None, b: float | None) -> bool:
    """Exact match on the line's number (moneylines have no number at all)."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < 1e-9


def evaluate_games(
    games: Sequence[Game], cfg: Config, markets: Sequence[str]
) -> list[EdgeRow]:
    rows: list[EdgeRow] = []
    for game in games:
        for market in markets:
            rows.extend(evaluate_market(game, market, cfg))
    return rows


def rank(rows: Sequence[EdgeRow], min_edge_pct: float) -> list[EdgeRow]:
    """Priced rows at or above the edge threshold, best edge first."""
    keep = [
        r for r in rows
        if r.status == PRICED and r.edge is not None and r.edge * 100.0 >= min_edge_pct
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
