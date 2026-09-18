"""Best available price across the US books in the response.

The rest of the tool asks one question: is DraftKings beatable? This asks a
second one the same data already answers -- of the books somebody in the group
could actually bet at, which one is offering the best version of this side right
now. The answer is the same edge the Slate colours by, so a better number bought
with worse juice does not win here either.

No extra API calls: every book here came back in the response we already paid
for, because the `bookmakers` filter asks for all of them at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from cfb_edge.config import Config
from cfb_edge.edges import EdgeRow, evaluate_book
from cfb_edge.fair import fair_lines
from cfb_edge.models import Game

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for typing
    from cfb_edge.halfpoint import HalfPointTable


@dataclass(frozen=True)
class Offer:
    """One book's version of one side, scored against the sharp fair line."""

    book: str  # logical name, e.g. "betmgm"
    row: EdgeRow

    @property
    def edge_pct(self) -> float | None:
        return self.row.edge_pct

    @property
    def point(self) -> float | None:
        return self.row.dk_point

    @property
    def price(self) -> float:
        return self.row.dk_price


def _rank(offer: Offer) -> tuple:
    """Best edge first; ties broken so the winner is stable across scans."""
    return (
        -(offer.edge_pct if offer.edge_pct is not None else -1e9),
        offer.book,
    )


def best_offers(
    game: Game,
    market: str,
    cfg: Config,
    table: "HalfPointTable | None" = None,
    books: Sequence[str] | None = None,
) -> dict[str, Offer]:
    """The winning book for each side of this market, keyed by side.

    A side only appears when at least one book posted it *and* the sharp line
    covers it, because without a fair price there is nothing to rank by. Books
    that did not post the market are simply absent rather than an error.
    """
    shop = list(books if books is not None else cfg.shop_books)
    if not shop:
        return {}
    sharp_by_group = fair_lines(game, market, cfg)
    winners: dict[str, Offer] = {}
    for book in shop:
        for row in evaluate_book(game, market, book, cfg, table, sharp_by_group):
            if row.edge_pct is None:
                continue  # no fair price for this side, so nothing to compare
            offer = Offer(book=book, row=row)
            current = winners.get(row.side)
            if current is None or _rank(offer) < _rank(current):
                winners[row.side] = offer
    return winners


def best_by_market(
    game: Game,
    markets: Sequence[str],
    cfg: Config,
    table: "HalfPointTable | None" = None,
) -> dict[tuple[str, str], Offer]:
    """Every market's winners in one dict, keyed (market, side)."""
    out: dict[tuple[str, str], Offer] = {}
    for market in markets:
        for side, offer in best_offers(game, market, cfg, table).items():
            out[(market, side)] = offer
    return out


def shop_board(
    games: Sequence[Game],
    markets: Sequence[str],
    cfg: Config,
    table: "HalfPointTable | None" = None,
) -> dict[tuple[str, str, str], Offer]:
    """Winners for the whole board, keyed (event_id, market, side)."""
    out: dict[tuple[str, str, str], Offer] = {}
    for game in games:
        for (market, side), offer in best_by_market(game, markets, cfg, table).items():
            out[(game.event_id, market, side)] = offer
    return out


BOOK_LABELS = {
    "draftkings": "DK",
    "fanduel": "FD",
    "betmgm": "MGM",
    "caesars": "CZR",
    "espnbet": "ESPN",
    "pinnacle": "Pin",
    "circa": "Circa",
}


# The same books written out, for places with room for the real name.
BOOK_NAMES = {
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "caesars": "Caesars",
    "espnbet": "ESPN Bet",
    "pinnacle": "Pinnacle",
    "circa": "Circa",
    "consensus": "Consensus",
}


def book_label(book: str) -> str:
    """Short enough for a table cell, recognisable enough to act on."""
    return BOOK_LABELS.get(book, book.replace("_", " ").title())


def book_name(book: str) -> str:
    return BOOK_NAMES.get(book, book.replace("_", " ").title())


def serialize_offer(offer: Offer | None, market: str, prefix: str = "") -> dict[str, Any] | None:
    from cfb_edge.oddsmath import format_american
    from cfb_edge.web.slate import BETTER_BAND_PCT, number_text, verdict_for

    if offer is None:
        return None
    # The same bands as the DK column, from the same function. Two cells side by
    # side showing the same edge in different colours would be worse than no
    # colour at all.
    verdict = verdict_for(market, offer.price, offer.edge_pct)
    return {
        "book": offer.book,
        "book_label": book_label(offer.book),
        "number": number_text(market, offer.point, prefix),
        "price": format_american(offer.price),
        "edge_pct": None if offer.edge_pct is None else round(offer.edge_pct, 2),
        "verdict": verdict,
        "show_edge": (
            verdict is not None
            and offer.edge_pct is not None
            and offer.edge_pct >= BETTER_BAND_PCT
        ),
    }


__all__ = [
    "Offer",
    "best_by_market",
    "best_offers",
    "book_label",
    "book_name",
    "serialize_offer",
    "shop_board",
]
