from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfb_edge.config import Config
from cfb_edge.models import BookMarket, Game, Outcome, parse_games, parse_commence_time

FIXTURE = Path(__file__).parent / "fixtures" / "sample_odds.json"


@pytest.fixture
def cfg() -> Config:
    return Config(bankroll=10_000.0, kelly_fraction=0.25, max_bet_pct=None)


@pytest.fixture
def sample_envelope() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def sample_games(sample_envelope) -> list[Game]:
    return parse_games(sample_envelope["data"])


@pytest.fixture
def games_by_id(sample_games) -> dict[str, Game]:
    return {g.event_id: g for g in sample_games}


def make_game(books: dict[str, dict[str, list[Outcome]]], **kwargs) -> Game:
    """Build a Game from {book: {market: [outcomes]}}."""
    game = Game(
        event_id=kwargs.get("event_id", "evt"),
        commence_time=parse_commence_time(kwargs.get("commence_time", "2026-09-20T23:30:00Z")),
        home_team=kwargs.get("home_team", "Home Team"),
        away_team=kwargs.get("away_team", "Away Team"),
    )
    for book, markets in books.items():
        game.books[book] = {
            market: BookMarket(book=book, market=market, outcomes=tuple(outcomes))
            for market, outcomes in markets.items()
        }
    return game
