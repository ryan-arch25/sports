"""Normalized view of an Odds API payload."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

MARKETS = ("h2h", "spreads", "totals")

MARKET_LABELS = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}


def parse_commence_time(value: str) -> datetime:
    """The API returns ISO-8601 UTC like '2026-09-20T23:30:00Z'."""
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class Outcome:
    name: str
    price: float
    point: float | None = None

    @property
    def key(self) -> str:
        """Side identity: team name for h2h/spreads, Over/Under for totals."""
        return self.name


@dataclass(frozen=True)
class BookMarket:
    """One bookmaker's prices for one market of one game."""

    book: str
    market: str
    outcomes: tuple[Outcome, ...]
    last_update: str | None = None

    def outcome(self, name: str) -> Outcome | None:
        for o in self.outcomes:
            if o.name == name:
                return o
        return None

    @property
    def is_two_sided(self) -> bool:
        return len(self.outcomes) == 2 and len({o.name for o in self.outcomes}) == 2

    @property
    def signature(self) -> tuple[tuple[str, float | None], ...]:
        """Identifies 'the same line' across books: names plus their numbers."""
        return tuple(sorted((o.name, o.point) for o in self.outcomes))


@dataclass
class Game:
    event_id: str
    commence_time: datetime
    home_team: str
    away_team: str
    # book key -> market key -> BookMarket
    books: dict[str, dict[str, BookMarket]] = field(default_factory=dict)

    @property
    def matchup(self) -> str:
        return f"{self.away_team} @ {self.home_team}"

    def market(self, book: str, market: str) -> BookMarket | None:
        return self.books.get(book, {}).get(market)

    def books_with(self, market: str, candidates: Iterable[str]) -> list[str]:
        return [b for b in candidates if self.market(b, market) is not None]


def parse_games(payload: Iterable[dict[str, Any]]) -> list[Game]:
    """Turn raw Odds API JSON into `Game` objects, skipping malformed entries."""
    games: list[Game] = []
    for event in payload:
        try:
            game = Game(
                event_id=event["id"],
                commence_time=parse_commence_time(event["commence_time"]),
                home_team=event["home_team"],
                away_team=event["away_team"],
            )
        except (KeyError, TypeError, ValueError):
            continue
        for bookmaker in event.get("bookmakers") or []:
            book_key = bookmaker.get("key")
            if not book_key:
                continue
            markets: dict[str, BookMarket] = {}
            for market in bookmaker.get("markets") or []:
                market_key = market.get("key")
                if not market_key:
                    continue
                outcomes = []
                for raw in market.get("outcomes") or []:
                    try:
                        outcomes.append(
                            Outcome(
                                name=raw["name"],
                                price=float(raw["price"]),
                                point=None if raw.get("point") is None else float(raw["point"]),
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                if outcomes:
                    markets[market_key] = BookMarket(
                        book=book_key,
                        market=market_key,
                        outcomes=tuple(outcomes),
                        last_update=market.get("last_update") or bookmaker.get("last_update"),
                    )
            if markets:
                game.books[book_key] = markets
        games.append(game)
    games.sort(key=lambda g: (g.commence_time, g.matchup))
    return games
