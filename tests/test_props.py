"""Tests for player props and alternate lines."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import cfb_edge.api as api
from cfb_edge.cache import save_response
from cfb_edge.edges import NO_SHARP_LINE, PRICED, evaluate_market
from cfb_edge.models import Outcome, merge_event_payload
from cfb_edge.props import (
    eligible_games,
    estimate_cost,
    fetch_extra_markets,
    selected_markets,
)
from cfb_edge.scan import ScanOptions

from conftest import make_game
from test_api import FakeResponse

NOW = datetime(2026, 9, 18, 18, 0, tzinfo=timezone.utc)


def prop_payload(event_id: str, books=("draftkings", "pinnacle")) -> dict:
    """A per-event response: two books quoting one passing-yards prop."""
    prices = {"draftkings": (105, -125), "pinnacle": (-108, -108)}
    return {
        "id": event_id,
        "bookmakers": [
            {
                "key": book,
                "markets": [{
                    "key": "player_pass_yds",
                    "outcomes": [
                        {"name": "Over", "description": "Jalen Milroe", "price": prices[book][0],
                         "point": 249.5},
                        {"name": "Under", "description": "Jalen Milroe", "price": prices[book][1],
                         "point": 249.5},
                    ],
                }],
            }
            for book in books
        ],
    }


def game_at(hours_from_now: float, event_id: str = "evt"):
    return make_game(
        {"draftkings": {"h2h": [Outcome("Home Team", -110), Outcome("Away Team", -110)]}},
        event_id=event_id,
        commence_time=(NOW + timedelta(hours=hours_from_now)).isoformat().replace("+00:00", "Z"),
    )


class TestMarketSelection:
    def test_props_only(self, cfg):
        markets = selected_markets(cfg, ScanOptions(props=True))
        assert "player_pass_yds" in markets
        assert "alternate_spreads" not in markets

    def test_alts_only(self, cfg):
        assert selected_markets(cfg, ScanOptions(alts=True)) == cfg.alt_markets

    def test_both_without_duplicates(self, cfg):
        markets = selected_markets(cfg, ScanOptions(props=True, alts=True))
        assert len(markets) == len(set(markets))
        assert markets[0] in cfg.alt_markets

    def test_an_explicit_prop_list_overrides_the_config(self, cfg):
        markets = selected_markets(cfg, ScanOptions(props=True, prop_markets=("player_rush_yds",)))
        assert markets == ("player_rush_yds",)

    def test_nothing_requested(self, cfg):
        assert selected_markets(cfg, ScanOptions()) == ()


class TestEligibility:
    def test_only_games_inside_the_window(self, cfg):
        games = [game_at(2, "soon"), game_at(50, "later")]
        picked = eligible_games(games, cfg, ScanOptions(props=True, props_window_hours=24), now=NOW)
        assert [g.event_id for g in picked] == ["soon"]

    def test_games_already_underway_are_skipped(self, cfg):
        games = [game_at(-1, "started"), game_at(3, "soon")]
        picked = eligible_games(games, cfg, ScanOptions(props=True), now=NOW)
        assert [g.event_id for g in picked] == ["soon"]

    def test_soonest_first_and_capped(self, cfg):
        games = [game_at(h, f"g{h}") for h in (10, 2, 6)]
        picked = eligible_games(
            games, cfg, ScanOptions(props=True, props_max_events=2), now=NOW
        )
        assert [g.event_id for g in picked] == ["g2", "g6"]

    def test_a_wide_window_keeps_the_whole_slate(self, cfg):
        games = [game_at(h, f"g{h}") for h in (10, 100)]
        picked = eligible_games(
            games, cfg, ScanOptions(props=True, props_window_hours=200), now=NOW
        )
        assert len(picked) == 2

    def test_cost_is_per_game_per_market(self):
        assert estimate_cost(6, 4) == 24
        assert estimate_cost(3, 0) == 3


class TestFetching:
    def test_merges_props_into_the_board(self, cfg, tmp_path, monkeypatch):
        cfg.cache_dir = tmp_path / "cache"
        cfg.api_key = "k"
        game = game_at(2, "evt")
        monkeypatch.setattr(
            api.requests, "get", lambda *a, **k: FakeResponse(200, prop_payload("evt"))
        )
        markets, requests_made, warnings = fetch_extra_markets(
            cfg, [game], ScanOptions(props=True, prop_markets=("player_pass_yds",))
        )
        assert markets == ("player_pass_yds",)
        assert requests_made == 1
        assert game.market("draftkings", "player_pass_yds") is not None

    def test_a_prop_prices_against_the_sharp_book(self, cfg, tmp_path, monkeypatch):
        cfg.cache_dir = tmp_path / "cache"
        cfg.api_key = "k"
        game = game_at(2, "evt")
        monkeypatch.setattr(
            api.requests, "get", lambda *a, **k: FakeResponse(200, prop_payload("evt"))
        )
        fetch_extra_markets(
            cfg, [game], ScanOptions(props=True, prop_markets=("player_pass_yds",))
        )
        rows = evaluate_market(game, "player_pass_yds", cfg)
        assert {r.status for r in rows} == {PRICED}
        assert {r.side for r in rows} == {"Jalen Milroe Over", "Jalen Milroe Under"}
        over = next(r for r in rows if r.side.endswith("Over"))
        assert over.fair_prob == pytest.approx(0.5)
        assert over.edge == pytest.approx(0.5 - 100 / 205)  # +105 beats a fair coin flip

    def test_each_player_is_priced_on_their_own_line(self, cfg):
        game = make_game({
            "draftkings": {"player_rush_yds": [
                Outcome("Over", -110, 80.5, "Back One"),
                Outcome("Under", -110, 80.5, "Back One"),
                Outcome("Over", -110, 45.5, "Back Two"),
                Outcome("Under", -110, 45.5, "Back Two"),
            ]},
            "pinnacle": {"player_rush_yds": [
                Outcome("Over", -105, 80.5, "Back One"),
                Outcome("Under", -105, 80.5, "Back One"),
            ]},
        })
        rows = evaluate_market(game, "player_rush_yds", cfg)
        by_side = {r.side: r.status for r in rows}
        assert by_side["Back One Over"] == PRICED
        # Pinnacle never posted Back Two, and one soft book is not a consensus.
        assert by_side["Back Two Over"] == NO_SHARP_LINE

    def test_declining_the_quota_prompt_spends_nothing(self, cfg, tmp_path, monkeypatch):
        cfg.cache_dir = tmp_path / "cache"
        cfg.api_key = "k"
        cfg.props_confirm_threshold = 0
        monkeypatch.setattr(
            api.requests, "get", lambda *a, **k: pytest.fail("should not call the API")
        )
        markets, requests_made, warnings = fetch_extra_markets(
            cfg, [game_at(2)], ScanOptions(props=True, confirm_quota=lambda n, t: False)
        )
        assert (markets, requests_made) == ((), 0)
        assert "declined" in warnings[0]

    def test_the_prompt_is_skipped_below_the_threshold(self, cfg, tmp_path, monkeypatch):
        cfg.cache_dir = tmp_path / "cache"
        cfg.api_key = "k"
        cfg.props_confirm_threshold = 100
        monkeypatch.setattr(
            api.requests, "get", lambda *a, **k: FakeResponse(200, prop_payload("evt"))
        )
        _, requests_made, _ = fetch_extra_markets(
            cfg, [game_at(2, "evt")],
            ScanOptions(props=True, prop_markets=("player_pass_yds",),
                        confirm_quota=lambda n, t: pytest.fail("should not prompt")),
        )
        assert requests_made == 1

    def test_a_cached_event_costs_no_request(self, cfg, tmp_path, monkeypatch):
        cfg.cache_dir = tmp_path / "cache"
        cfg.api_key = "k"
        options = ScanOptions(props=True, prop_markets=("player_pass_yds",))
        params = api.event_request_params(cfg, ("player_pass_yds",))
        save_response(cfg.cache_dir, f"{cfg.sport}_event_evt", params, [prop_payload("evt")])
        monkeypatch.setattr(
            api.requests, "get", lambda *a, **k: pytest.fail("should not call the API")
        )
        game = game_at(2, "evt")
        markets, requests_made, _ = fetch_extra_markets(cfg, [game], options)
        assert requests_made == 0
        assert game.market("draftkings", "player_pass_yds") is not None

    def test_nothing_in_the_window_is_reported(self, cfg):
        markets, requests_made, warnings = fetch_extra_markets(
            cfg, [game_at(200)], ScanOptions(props=True)
        )
        assert (markets, requests_made) == ((), 0)
        assert "window" in warnings[0]

    def test_a_market_no_book_offers_is_reported(self, cfg, tmp_path, monkeypatch):
        cfg.cache_dir = tmp_path / "cache"
        cfg.api_key = "k"
        monkeypatch.setattr(api.requests, "get", lambda *a, **k: FakeResponse(404))
        markets, _, warnings = fetch_extra_markets(
            cfg, [game_at(2, "evt")],
            ScanOptions(props=True, prop_markets=("player_pass_yds",)),
        )
        assert markets == ()
        assert any("no book offered" in w for w in warnings)


class TestMerge:
    def test_a_mismatched_event_is_ignored(self):
        game = game_at(2, "evt")
        merge_event_payload(game, prop_payload("someone-else"))
        assert game.market("draftkings", "player_pass_yds") is None

    def test_existing_markets_survive_the_merge(self):
        game = game_at(2, "evt")
        merge_event_payload(game, prop_payload("evt"))
        assert game.market("draftkings", "h2h") is not None
        assert game.market("draftkings", "player_pass_yds") is not None

    def test_junk_is_ignored(self):
        game = game_at(2, "evt")
        merge_event_payload(game, [])
        assert set(game.books) == {"draftkings"}
