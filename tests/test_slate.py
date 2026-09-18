"""Tests for the Slate view: every game, DraftKings beside the sharp book."""

from __future__ import annotations

from cfb_edge.edges import evaluate_games
import pytest

from cfb_edge.models import Outcome, parse_commence_time
from cfb_edge.web.slate import (
    BETTER,
    NEUTRAL,
    WORSE,
    build_slate,
    compare_side,
    day_label,
    serialize_cell,
)

from conftest import make_game


def rows_for(games, cfg, markets=("h2h", "spreads", "totals")):
    return evaluate_games(games, cfg, markets)


def one_game_slate(game, cfg, markets=("h2h", "spreads", "totals")):
    return build_slate([game], rows_for([game], cfg, markets))[0]["games"][0]


def side_of(game_payload, label):
    return next(s for s in game_payload["sides"] if s["label"] == label)


class TestCompareSide:
    """The verdict is the edge, so number and juice are weighed together."""

    def market(self, cfg, dk, sharp, market="totals", table=None):
        from cfb_edge.edges import evaluate_market

        game = make_game({"draftkings": {market: dk}, "pinnacle": {market: sharp}})
        return {r.side: r for r in evaluate_market(game, market, cfg, table)}

    def test_a_better_price_on_the_same_number_is_better(self, cfg):
        rows = self.market(
            cfg,
            dk=[Outcome("Over", 105, 44.5), Outcome("Under", -125, 44.5)],
            sharp=[Outcome("Over", -110, 44.5), Outcome("Under", -110, 44.5)],
        )
        assert compare_side(rows["Over"]) == BETTER
        assert compare_side(rows["Under"]) == WORSE

    def test_a_better_number_bought_with_bad_juice_is_not_better(self, cfg, halfpoint_table):
        """The whole reason for weighing them together."""
        rows = self.market(
            cfg,
            dk=[Outcome("Over", -200, 51.5), Outcome("Under", 160, 51.5)],
            sharp=[Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)],
            table=halfpoint_table,
        )
        over = rows["Over"]
        assert over.line_diff > 0          # DK's number is the better one
        assert compare_side(over) == WORSE  # and the price more than swallows it

    def test_a_near_coin_flip_stays_neutral(self, cfg):
        rows = self.market(
            cfg,
            dk=[Outcome("Over", -102, 44.5), Outcome("Under", -102, 44.5)],
            sharp=[Outcome("Over", -102, 44.5), Outcome("Under", -102, 44.5)],
        )
        assert {compare_side(r) for r in rows.values()} == {NEUTRAL}

    @pytest.mark.parametrize(
        "edge,expected",
        [(2.0, BETTER), (0.5, BETTER), (0.49, NEUTRAL), (0.0, NEUTRAL),
         (-0.49, NEUTRAL), (-0.5, WORSE), (-3.0, WORSE)],
    )
    def test_the_bands(self, edge, expected, cfg):
        rows = self.market(
            cfg,
            dk=[Outcome("Over", -110, 44.5), Outcome("Under", -110, 44.5)],
            sharp=[Outcome("Over", -110, 44.5), Outcome("Under", -110, 44.5)],
        )
        row = rows["Over"]
        row.edge = edge / 100.0
        assert compare_side(row) == expected

    def test_no_sharp_line_means_no_verdict(self, cfg):
        from cfb_edge.edges import evaluate_market

        game = make_game({
            "draftkings": {"h2h": [Outcome("Home Team", -320), Outcome("Away Team", 260)]},
            "fanduel": {"h2h": [Outcome("Home Team", -310), Outcome("Away Team", 250)]},
        })
        assert {compare_side(r) for r in evaluate_market(game, "h2h", cfg)} == {None}

    def test_a_missing_row_has_no_verdict(self):
        assert compare_side(None) is None

    def test_the_cell_only_offers_its_number_when_it_is_worth_reading(self, cfg):
        rows = self.market(
            cfg,
            dk=[Outcome("Over", 105, 44.5), Outcome("Under", -125, 44.5)],
            sharp=[Outcome("Over", -110, 44.5), Outcome("Under", -110, 44.5)],
        )
        assert serialize_cell("totals", rows["Over"])["show_edge"] is True
        assert serialize_cell("totals", rows["Under"])["show_edge"] is False


class TestCells:
    def test_a_spread_keeps_its_sign(self, cfg):
        game = make_game({
            "draftkings": {"spreads": [
                Outcome("Home Team", -110, -3.5), Outcome("Away Team", -110, 3.5),
            ]},
            "pinnacle": {"spreads": [
                Outcome("Home Team", -110, -3.5), Outcome("Away Team", -110, 3.5),
            ]},
        })
        payload = one_game_slate(game, cfg, ("spreads",))
        assert side_of(payload, "Home Team")["spread"]["number"] == "-3.5"
        assert side_of(payload, "Away Team")["spread"]["number"] == "+3.5"

    def test_a_total_carries_its_over_under_prefix(self, cfg):
        game = make_game({
            "draftkings": {"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
            "pinnacle": {"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
        })
        payload = one_game_slate(game, cfg, ("totals",))
        assert side_of(payload, "Away Team")["total"]["number"] == "O 51.5"
        assert side_of(payload, "Home Team")["total"]["number"] == "U 51.5"

    def test_a_moneyline_has_a_price_but_no_number(self, cfg):
        game = make_game({
            "draftkings": {"h2h": [Outcome("Home Team", -175), Outcome("Away Team", 145)]},
            "pinnacle": {"h2h": [Outcome("Home Team", -145), Outcome("Away Team", 130)]},
        })
        cell = side_of(one_game_slate(game, cfg, ("h2h",)), "Away Team")["h2h"]
        assert cell["number"] == ""
        assert cell["price"] == "+145"
        assert cell["sharp_price"] == "+130"

    def test_the_sharp_source_travels_with_the_cell(self, cfg):
        game = make_game({
            "draftkings": {"h2h": [Outcome("Home Team", -175), Outcome("Away Team", 145)]},
            "circa": {"h2h": [Outcome("Home Team", -145), Outcome("Away Team", 130)]},
        })
        cell = side_of(one_game_slate(game, cfg, ("h2h",)), "Away Team")["h2h"]
        assert cell["sharp_source"] == "circa"

    def test_a_positive_edge_rides_along(self, cfg):
        game = make_game({
            "draftkings": {"h2h": [Outcome("Home Team", -175), Outcome("Away Team", 145)]},
            "pinnacle": {"h2h": [Outcome("Home Team", -145), Outcome("Away Team", 130)]},
        })
        cell = side_of(one_game_slate(game, cfg, ("h2h",)), "Away Team")["h2h"]
        assert cell["edge_pct"] > 0
        assert cell["verdict"] == BETTER

    def test_a_market_nobody_posted_is_empty(self, cfg):
        game = make_game({
            "draftkings": {"h2h": [Outcome("Home Team", -175), Outcome("Away Team", 145)]},
            "pinnacle": {"h2h": [Outcome("Home Team", -145), Outcome("Away Team", 130)]},
        })
        payload = one_game_slate(game, cfg)
        assert side_of(payload, "Away Team")["spread"] is None
        assert side_of(payload, "Away Team")["total"] is None

    def test_serialize_cell_passes_none_through(self):
        assert serialize_cell("spreads", None) is None


class TestSlateShape:
    def test_away_team_and_over_come_first(self, sample_games, cfg):
        slate = build_slate(sample_games, rows_for(sample_games, cfg))
        game = next(
            g for day in slate for g in day["games"] if g["event_id"] == "g2michigan"
        )
        assert [s["label"] for s in game["sides"]] == [
            "Ohio State Buckeyes", "Michigan Wolverines"
        ]
        assert [s["total_label"] for s in game["sides"]] == ["Over", "Under"]

    def test_every_game_appears_even_without_draftkings(self, sample_games, cfg):
        slate = build_slate(sample_games, rows_for(sample_games, cfg))
        assert sum(len(day["games"]) for day in slate) == len(sample_games)

    def test_games_are_grouped_by_kickoff_day_in_eastern_time(self, cfg):
        early = make_game(
            {"draftkings": {"h2h": [Outcome("Home Team", -110), Outcome("Away Team", -110)]}},
            event_id="a", commence_time="2026-09-19T23:00:00Z",  # 7pm ET Friday
        )
        late = make_game(
            {"draftkings": {"h2h": [Outcome("Home Team", -110), Outcome("Away Team", -110)]}},
            event_id="b", commence_time="2026-09-20T16:00:00Z",  # noon ET Saturday
        )
        slate = build_slate([late, early], rows_for([late, early], cfg, ("h2h",)))
        assert [day["label"] for day in slate] == [
            "Saturday, September 19", "Sunday, September 20"
        ]
        assert [day["games"][0]["event_id"] for day in slate] == ["a", "b"]

    def test_games_inside_a_day_are_in_kickoff_order(self, cfg):
        games = [
            make_game({"draftkings": {"h2h": [Outcome("Home Team", -110), Outcome("Away Team", -110)]}},
                      event_id=name, commence_time=stamp)
            for name, stamp in [
                ("late", "2026-09-20T23:30:00Z"),
                ("early", "2026-09-20T16:00:00Z"),
                ("middle", "2026-09-20T19:30:00Z"),
            ]
        ]
        slate = build_slate(games, rows_for(games, cfg, ("h2h",)))
        assert [g["event_id"] for g in slate[0]["games"]] == ["early", "middle", "late"]

    def test_kickoff_times_are_eastern(self, sample_games, cfg):
        slate = build_slate(sample_games, rows_for(sample_games, cfg))
        game = next(g for day in slate for g in day["games"] if g["event_id"] == "g2michigan")
        assert game["kickoff_time_et"] == "12:00 PM"

    def test_the_search_field_is_ready_to_match(self, sample_games, cfg):
        slate = build_slate(sample_games, rows_for(sample_games, cfg))
        game = next(g for day in slate for g in day["games"] if g["event_id"] == "g2michigan")
        assert game["search"] == "ohio state buckeyes michigan wolverines"
        assert "michigan" in game["search"]

    def test_an_empty_board(self, cfg):
        assert build_slate([], []) == []

    def test_day_label_is_written_out(self):
        assert day_label(parse_commence_time("2026-09-20T16:00:00Z")) == "Sunday, September 20"

    def test_a_late_kickoff_lands_on_the_eastern_day_not_the_utc_one(self):
        # 01:00 UTC Sunday is still Saturday evening in the east.
        assert day_label(parse_commence_time("2026-09-20T01:00:00Z")) == "Saturday, September 19"


class TestEstimatedTagPerRow:
    def test_a_row_is_tagged_when_any_of_its_markets_was_estimated(self, cfg):
        from cfb_edge.halfpoint import estimated_table

        game = make_game({
            "draftkings": {
                "totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)],
                "spreads": [Outcome("Home Team", -110, -3.5), Outcome("Away Team", -110, 3.5)],
            },
            "pinnacle": {
                "totals": [Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)],
                "spreads": [Outcome("Home Team", -110, -3.5), Outcome("Away Team", -110, 3.5)],
            },
        })
        rows = evaluate_games([game], cfg, ("spreads", "totals"), estimated_table(cfg))
        payload = build_slate([game], rows)[0]["games"][0]
        assert all(side["estimated"] is True for side in payload["sides"])
        # The spread was on the same number, so only the total was estimated.
        assert payload["sides"][0]["spread"]["estimated"] is False
        assert payload["sides"][0]["total"]["estimated"] is True

    def test_a_row_with_nothing_estimated_is_not_tagged(self, sample_games, cfg):
        slate = build_slate(sample_games, rows_for(sample_games, cfg))
        game = next(g for day in slate for g in day["games"] if g["event_id"] == "g2michigan")
        assert all(side["estimated"] is False for side in game["sides"])
