"""Compare DraftKings prices against the sharp fair line."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Sequence

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
    translation_source: str | None = None  # "table" or "estimated"

    @property
    def matchup(self) -> str:
        return f"{self.away_team} @ {self.home_team}"

    @property
    def market_label(self) -> str:
        return market_label(self.market)

    @property
    def is_estimated(self) -> bool:
        """Priced off the published chart rather than a built table."""
        return self.translation_source == "estimated"

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

    @property
    def line_diff(self) -> float | None:
        """How much better DK's number is than the sharp book's, for this side.

        Positive means DK's number helps the pick, negative means it hurts,
        None when there is no number to compare. Units are the market's own, so
        a passing-yards prop reads in yards rather than points.
        """
        from cfb_edge.halfpoint import TOTAL, line_diff, market_kind, side_role

        if self.market == "h2h":
            return None
        # Markets the half-point table does not price -- props, team totals --
        # are still Over/Under markets whose number is worth comparing.
        role = side_role(market_kind(self.market) or TOTAL, self.side, self.sharp_point)
        if role is None:
            return None
        return line_diff(role, self.sharp_point, self.dk_point)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["commence_time"] = self.commence_time.isoformat().replace("+00:00", "Z")
        data["matchup"] = self.matchup
        data["pick"] = self.pick
        data["market_label"] = self.market_label
        data["edge_pct"] = self.edge_pct
        data["line_diff"] = self.line_diff
        data["is_estimated"] = self.is_estimated
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


def evaluate_book(
    game: Game,
    market: str,
    book: str,
    cfg: Config,
    table: "HalfPointTable | None" = None,
    sharp_by_group: dict[str | None, FairLine] | None = None,
) -> list[EdgeRow]:
    """One row per side this book posts in this market, priced off the sharp line.

    `sharp_by_group` lets a caller price several books against one fair line
    without paying to de-vig it again for each of them.
    """
    book_key = resolve_book(game, book, cfg.aliases)
    book_market = game.market(book_key, market) if book_key else None
    if book_market is None:
        return []

    if sharp_by_group is None:
        sharp_by_group = fair_lines(game, market, cfg)
    rows: list[EdgeRow] = []
    for outcome in book_market.outcomes:
        try:
            offered_prob = american_to_prob(outcome.price)
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
                game, market, outcome.key, outcome.point, outcome.price, offered_prob,
                sharp, cfg, table,
            )
        )
    return rows


def evaluate_market(
    game: Game,
    market: str,
    cfg: Config,
    table: "HalfPointTable | None" = None,
) -> list[EdgeRow]:
    """One row per DraftKings side in this market."""
    return evaluate_book(game, market, cfg.target_book, cfg, table)


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
    translation_source = None

    if not _same_number(dk_point, sharp_side.point):
        priced = translate_prob(table, market, side, sharp_side.point, dk_point, fair_prob)
        if priced is None:
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
        moved, translation_source = priced
        status = TRANSLATED
        translated_from = sharp_side.point
        origin = (
            "published half-point estimate"
            if translation_source == "estimated"
            else "half-point table"
        )
        note = (
            f"{origin} moved {sharp.label} "
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
        translation_source=translation_source,
    )


def as_tables(table: Any) -> list[Any]:
    """Accept one table or several, so callers can stay simple."""
    if table is None:
        return []
    if isinstance(table, (list, tuple)):
        return [t for t in table if t is not None]
    return [table]


def translate_prob(
    table: Any,
    market: str,
    side: str,
    sharp_point: float | None,
    dk_point: float | None,
    fair_prob: float,
) -> tuple[float, str] | None:
    """Move a fair probability from one number to another on the same side.

    Returns (probability, which table did it), or None when the move cannot be
    priced: a market the half-point table does not model, a gap wider than the
    table's `max_move`, or a sample too thin to mean anything. Callers are
    expected to show nothing rather than guess.
    """
    tables = as_tables(table)
    if not tables or sharp_point is None or dk_point is None:
        return None
    from cfb_edge.halfpoint import market_kind, side_role

    kind = market_kind(market)
    if kind is None:
        return None
    role = side_role(kind, side, sharp_point)
    if role is None:
        return None
    for candidate in tables:
        moved = candidate.translate(
            kind, role, fair_prob, float(sharp_point), float(dk_point)
        )
        if moved is not None:
            return moved, getattr(candidate, "source_name", "table")
    return None


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
    "evaluate_book",
    "evaluate_games",
    "evaluate_market",
    "different_number_rows",
    "format_pick",
    "format_point",
    "rank",
    "summarize",
    "translate_prob",
]
