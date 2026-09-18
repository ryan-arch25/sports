"""Synthetic historical results for tests.

This is scaffolding, not data: it generates games whose margins carry the
key-number structure real college football has (piles on 3, 7, 10, 14) so the
half-point machinery can be exercised deterministically. A real table must be
built from `cfb-edge scores fetch`.
"""

from __future__ import annotations

import random

from cfb_edge.scores import HistoricalGame

# Roughly how often a final margin lands on each key number, before the
# smooth part of the distribution is added.
KEY_MARGINS = {3: 0.095, 7: 0.065, 10: 0.055, 14: 0.045, 4: 0.040, 1: 0.035}
KEY_TOTAL_HOOKS = (0, 3, 7, 10)


def synthetic_games(
    count: int = 6000, seed: int = 20260918, seasons: tuple[int, ...] = (2019, 2024)
) -> list[HistoricalGame]:
    rng = random.Random(seed)
    games: list[HistoricalGame] = []
    first, last = seasons
    for i in range(count):
        spread_magnitude = round(abs(rng.gauss(0, 9)) * 2) / 2  # half-point grid
        spread_magnitude = min(spread_magnitude, 34.0)
        closing_total = round(rng.gauss(53, 7) * 2) / 2
        closing_total = min(max(closing_total, 30.0), 85.0)

        margin = _draw_margin(rng, spread_magnitude)
        total_points = max(int(round(rng.gauss(closing_total, 10.5))), 0)
        # Keep the score self-consistent with the margin and the total.
        if (total_points + margin) % 2:
            total_points += 1
        favorite_score = (total_points + margin) // 2
        underdog_score = total_points - favorite_score
        if favorite_score < 0 or underdog_score < 0:
            favorite_score, underdog_score = max(favorite_score, 0), max(underdog_score, 0)

        home_is_favorite = rng.random() < 0.65
        home_score = favorite_score if home_is_favorite else underdog_score
        away_score = underdog_score if home_is_favorite else favorite_score
        closing_spread = -spread_magnitude if home_is_favorite else spread_magnitude

        games.append(HistoricalGame(
            game_id=1_000_000 + i,
            season=rng.randint(first, last),
            week=rng.randint(1, 15),
            season_type="regular",
            start_date=None,
            home_team=f"Home {i % 130}",
            away_team=f"Away {i % 127}",
            home_score=home_score,
            away_score=away_score,
            closing_spread=closing_spread,
            closing_total=closing_total,
            line_provider="synthetic",
        ))
    return games


def _draw_margin(rng: random.Random, spread: float) -> int:
    """Favorite's margin: a wide bell, with extra weight on the key numbers."""
    roll = rng.random()
    cumulative = 0.0
    for key, weight in KEY_MARGINS.items():
        cumulative += weight
        if roll < cumulative:
            # Key numbers cluster near the line, not uniformly across the board.
            sign = 1 if rng.random() < 0.5 + min(spread, 20) / 50 else -1
            return sign * key if sign > 0 else -key
    return int(round(rng.gauss(spread, 13.5)))
