"""Fair-price estimation from the sharp book.

Pinnacle is the reference. If Pinnacle has not posted the market we fall back
to Circa, and if neither is there we build a consensus out of the remaining
books. In every case both sides' American prices are converted to implied
probabilities and divided by their sum, which strips the vig and leaves a fair
win probability.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Iterable, Sequence

from cfb_edge.config import Config
from cfb_edge.models import BookMarket, Game
from cfb_edge.oddsmath import (
    OddsError,
    american_to_prob,
    devig,
    hold,
    prob_to_american,
)


@dataclass(frozen=True)
class FairSide:
    name: str
    point: float | None
    fair_prob: float
    price: float  # representative sharp American price for this side


@dataclass(frozen=True)
class FairLine:
    market: str
    source: str  # logical book name, or "consensus"
    books: tuple[str, ...]  # Odds API book keys actually used
    sides: tuple[FairSide, ...]
    hold: float

    @property
    def label(self) -> str:
        if self.source == "consensus":
            return f"consensus({len(self.books)})"
        return self.source

    def side(self, name: str) -> FairSide | None:
        for s in self.sides:
            if s.name == name:
                return s
        return None


def resolve_book(game: Game, logical: str, aliases: dict[str, list[str]]) -> str | None:
    """Map a logical book name to the API key actually present in this game."""
    for alias in aliases.get(logical, [logical]):
        if alias in game.books:
            return alias
    return None


def _usable(book_market: BookMarket | None) -> bool:
    if book_market is None or not book_market.is_two_sided:
        return False
    try:
        for outcome in book_market.outcomes:
            american_to_prob(outcome.price)
    except OddsError:
        return False
    return True


def _devig_market(book_market: BookMarket) -> tuple[list[float], float]:
    raw = [american_to_prob(o.price) for o in book_market.outcomes]
    return devig(raw), hold(raw)


def fair_from_book(book_market: BookMarket, source: str) -> FairLine | None:
    """De-vig one book's two-sided market into fair probabilities."""
    if not _usable(book_market):
        return None
    fair_probs, book_hold = _devig_market(book_market)
    sides = tuple(
        FairSide(name=o.name, point=o.point, fair_prob=p, price=o.price)
        for o, p in zip(book_market.outcomes, fair_probs)
    )
    return FairLine(
        market=book_market.market,
        source=source,
        books=(book_market.book,),
        sides=sides,
        hold=book_hold,
    )


def _consensus_group(markets: Sequence[BookMarket]) -> list[BookMarket]:
    """Pick the set of books quoting the same number (the modal line).

    Ties go to the group with the lowest average hold, which is the closest
    thing to 'sharpest' among soft books.
    """
    groups: dict[tuple, list[BookMarket]] = {}
    for bm in markets:
        groups.setdefault(bm.signature, []).append(bm)

    def score(item: tuple[tuple, list[BookMarket]]) -> tuple:
        _, group = item
        avg_hold = fmean(_devig_market(bm)[1] for bm in group)
        return (-len(group), avg_hold)

    return min(groups.items(), key=score)[1] if groups else []


def fair_from_consensus(
    markets: Sequence[BookMarket], min_books: int = 2
) -> FairLine | None:
    """Average the de-vigged probabilities of every book on the same number."""
    usable = [bm for bm in markets if _usable(bm)]
    group = _consensus_group(usable)
    if len(group) < min_books:
        return None

    names = [o.name for o in group[0].outcomes]
    fair_by_name: dict[str, list[float]] = {name: [] for name in names}
    raw_by_name: dict[str, list[float]] = {name: [] for name in names}
    for bm in group:
        fair_probs, _ = _devig_market(bm)
        for outcome, fair_prob in zip(bm.outcomes, fair_probs):
            fair_by_name[outcome.name].append(fair_prob)
            raw_by_name[outcome.name].append(american_to_prob(outcome.price))

    # Averaging de-vigged probabilities can drift off 1.0; renormalize.
    averaged = [fmean(fair_by_name[name]) for name in names]
    normalized = devig(averaged)
    raw_avg = [fmean(raw_by_name[name]) for name in names]

    points = {o.name: o.point for o in group[0].outcomes}
    sides = tuple(
        FairSide(
            name=name,
            point=points[name],
            fair_prob=prob,
            price=prob_to_american(raw),
        )
        for name, prob, raw in zip(names, normalized, raw_avg)
    )
    return FairLine(
        market=group[0].market,
        source="consensus",
        books=tuple(bm.book for bm in group),
        sides=sides,
        hold=hold(raw_avg),
    )


def fair_line(game: Game, market: str, cfg: Config) -> FairLine | None:
    """Pinnacle, then Circa, then a consensus of the remaining books."""
    for logical in cfg.sharp_priority:
        book_key = resolve_book(game, logical, cfg.aliases)
        if book_key is None:
            continue
        line = fair_from_book(game.market(book_key, market), logical)
        if line is not None:
            return line

    candidates: list[BookMarket] = []
    for logical in _consensus_candidates(cfg):
        book_key = resolve_book(game, logical, cfg.aliases)
        if book_key is None:
            continue
        bm = game.market(book_key, market)
        if bm is not None:
            candidates.append(bm)
    return fair_from_consensus(candidates, min_books=cfg.min_consensus_books)


def _consensus_candidates(cfg: Config) -> Iterable[str]:
    """Consensus never includes the book we are shopping against."""
    return [b for b in cfg.consensus_books if b != cfg.target_book]
