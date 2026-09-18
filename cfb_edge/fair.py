"""Fair-price estimation from the sharp book.

Pinnacle is the reference. If Pinnacle has not posted the market we fall back
to Circa, and if neither is there we build a consensus out of the remaining
books. In every case both sides' American prices are converted to implied
probabilities and divided by their sum, which strips the vig and leaves a fair
win probability.

Markets are handled one *group* at a time. A game market is a single group; a
player prop market is one group per player, each with its own two sides.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Iterable, Sequence

from cfb_edge.config import Config
from cfb_edge.models import BookMarket, Game, Outcome, outcomes_signature
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
    group: str | None = None  # player name for props, None for game markets

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


def _usable(outcomes: Sequence[Outcome]) -> bool:
    if len(outcomes) != 2 or len({o.key for o in outcomes}) != 2:
        return False
    try:
        for outcome in outcomes:
            american_to_prob(outcome.price)
    except OddsError:
        return False
    return True


def _devig_outcomes(outcomes: Sequence[Outcome]) -> tuple[list[float], float]:
    raw = [american_to_prob(o.price) for o in outcomes]
    return devig(raw), hold(raw)


def _fair_from_outcomes(
    book: str, market: str, outcomes: Sequence[Outcome], source: str, group: str | None = None
) -> FairLine | None:
    if not _usable(outcomes):
        return None
    fair_probs, book_hold = _devig_outcomes(outcomes)
    sides = tuple(
        FairSide(name=o.key, point=o.point, fair_prob=p, price=o.price)
        for o, p in zip(outcomes, fair_probs)
    )
    return FairLine(
        market=market, source=source, books=(book,), sides=sides, hold=book_hold, group=group
    )


def fair_from_book(book_market: BookMarket, source: str) -> FairLine | None:
    """De-vig one book's two-sided market into fair probabilities."""
    if book_market is None:
        return None
    return _fair_from_outcomes(
        book_market.book, book_market.market, book_market.outcomes, source
    )


def _consensus_group(
    quotes: Sequence[tuple[str, tuple[Outcome, ...]]]
) -> list[tuple[str, tuple[Outcome, ...]]]:
    """Pick the set of books quoting the same number (the modal line).

    Ties go to the group with the lowest average hold, which is the closest
    thing to 'sharpest' among soft books.
    """
    groups: dict[tuple, list[tuple[str, tuple[Outcome, ...]]]] = {}
    for book, outcomes in quotes:
        groups.setdefault(outcomes_signature(outcomes), []).append((book, outcomes))

    def score(item: tuple[tuple, list]) -> tuple:
        _, group = item
        avg_hold = fmean(_devig_outcomes(outcomes)[1] for _, outcomes in group)
        return (-len(group), avg_hold)

    return min(groups.items(), key=score)[1] if groups else []


def _fair_from_consensus_quotes(
    market: str,
    quotes: Sequence[tuple[str, tuple[Outcome, ...]]],
    min_books: int = 2,
    group: str | None = None,
) -> FairLine | None:
    """Average the de-vigged probabilities of every book on the same number."""
    usable = [(book, outcomes) for book, outcomes in quotes if _usable(outcomes)]
    chosen = _consensus_group(usable)
    if len(chosen) < min_books:
        return None

    reference = chosen[0][1]
    names = [o.key for o in reference]
    fair_by_name: dict[str, list[float]] = {name: [] for name in names}
    raw_by_name: dict[str, list[float]] = {name: [] for name in names}
    for _, outcomes in chosen:
        fair_probs, _ = _devig_outcomes(outcomes)
        for outcome, fair_prob in zip(outcomes, fair_probs):
            fair_by_name[outcome.key].append(fair_prob)
            raw_by_name[outcome.key].append(american_to_prob(outcome.price))

    # Averaging de-vigged probabilities can drift off 1.0; renormalize.
    averaged = [fmean(fair_by_name[name]) for name in names]
    normalized = devig(averaged)
    raw_avg = [fmean(raw_by_name[name]) for name in names]

    points = {o.key: o.point for o in reference}
    sides = tuple(
        FairSide(name=name, point=points[name], fair_prob=prob, price=prob_to_american(raw))
        for name, prob, raw in zip(names, normalized, raw_avg)
    )
    return FairLine(
        market=market,
        source="consensus",
        books=tuple(book for book, _ in chosen),
        sides=sides,
        hold=hold(raw_avg),
        group=group,
    )


def fair_from_consensus(
    markets: Sequence[BookMarket], min_books: int = 2
) -> FairLine | None:
    """Consensus across whole two-sided markets (game markets)."""
    if not markets:
        return None
    quotes = [(bm.book, bm.outcomes) for bm in markets]
    return _fair_from_consensus_quotes(markets[0].market, quotes, min_books=min_books)


def _consensus_candidates(cfg: Config) -> Iterable[str]:
    """Consensus never includes the book we are shopping against."""
    return [b for b in cfg.consensus_books if b != cfg.target_book]


def fair_lines(game: Game, market: str, cfg: Config) -> dict[str | None, FairLine]:
    """Fair lines for every group in a market: one per player, or one per game."""
    sharp_groups: dict[str | None, FairLine] = {}

    for logical in cfg.sharp_priority:
        book_key = resolve_book(game, logical, cfg.aliases)
        if book_key is None:
            continue
        book_market = game.market(book_key, market)
        if book_market is None:
            continue
        for group, outcomes in book_market.groups().items():
            if group in sharp_groups:
                continue  # an earlier, sharper book already priced this group
            line = _fair_from_outcomes(book_key, market, outcomes, logical, group=group)
            if line is not None:
                sharp_groups[group] = line

    # Anything the sharp books did not cover falls through to consensus.
    consensus_quotes: dict[str | None, list[tuple[str, tuple[Outcome, ...]]]] = {}
    for logical in _consensus_candidates(cfg):
        book_key = resolve_book(game, logical, cfg.aliases)
        if book_key is None:
            continue
        book_market = game.market(book_key, market)
        if book_market is None:
            continue
        for group, outcomes in book_market.groups().items():
            consensus_quotes.setdefault(group, []).append((book_key, outcomes))

    for group, quotes in consensus_quotes.items():
        if group in sharp_groups:
            continue
        line = _fair_from_consensus_quotes(
            market, quotes, min_books=cfg.min_consensus_books, group=group
        )
        if line is not None:
            sharp_groups[group] = line
    return sharp_groups


def fair_line(game: Game, market: str, cfg: Config) -> FairLine | None:
    """The game-level fair line: Pinnacle, then Circa, then a consensus."""
    return fair_lines(game, market, cfg).get(None)
