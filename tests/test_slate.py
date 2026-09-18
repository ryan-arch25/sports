"""Tests for the Slate view: every game, DraftKings beside the sharp book."""

from __future__ import annotations

from cfb_edge.edges import evaluate_games
from cfb_edge.models import Outcome, parse_commence_time
from cfb_edge.web.slate import (
    BETTER,
    SAME,
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
    def test_a_better_number_is_better(self, cfg, halfpoint_table):
        from cfb_edge.edges import evaluate_market

        game = make_game({
            "draftkings": {"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
            "pinnacle": {"totals": [Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)]},
        })
        rows = {r.side: r for r in evaluate_market(game, "totals", cfg, halfpoint_table)}
        assert compare_side(rows["Over"]) == BETTER
        assert compare_side(rows["Under"]) == WORSE

    def test_on_the_same_number_the_price_decides(self, cfg):
        from cfb_edge.edges import evaluate_market

        game = make_game({
            "draftkings": {"spreads": [
                Outcome("Home Team", -125, -3.5), Outcome("Away Team", 105, 3.5),
            ]},
            "pinnacle": {"spreads": [
                Outcome("Home Team", -105, -3.5), Outcome("Away Team", -105, 3.5),
            ]},
        })
        rows = {r.side: r for r in evaluate_market(game, "spreads", cfg)}
        assert compare_side(rows["Away Team"]) == BETTER   # +105 beats -105
        assert compare_side(rows["Home Team"]) == WORSE    # -125 is worse than -105

    def test_identical_prices_are_the_same(self, cfg):
        from cfb_edge.edges import evaluate_market

        game = make_game({
            "draftkings": {"h2h": [Outcome("Home Team", -140), Outcome("Away Team", 120)]},
            "pinnacle": {"h2h": [Outcome("Home Team", -140), Outcome("Away Team", 120)]},
        })
        assert {compare_side(r) for r in evaluate_market(game, "h2h", cfg)} == {SAME}

    def test_a_rounding_hair_does_not_colour_a_cell(self, cfg):
        """A consensus price is an average and can miss DK's by a fraction."""
        from cfb_edge.edges import evaluate_market

        game = make_game({
            "draftkings": {"h2h": [Outcome("Home Team", -170), Outcome("Away Team", 155)]},
            "fanduel": {"h2h": [Outcome("Home Team", -170), Outcome("Away Team", 155)]},
            "betmgm": {"h2h": [Outcome("Home Team", -175), Outcome("Away Team", 150)]},
            "williamhill_us": {"h2h": [Outcome("Home Team", -165), Outcome("Away Team", 160)]},
        })
        rows = {r.side: r for r in evaluate_market(game, "h2h", cfg)}
        assert rows["Away Team"].sharp_price != 155  # the average is not exactly DK's
        assert round(rows["Away Team"].sharp_price) == 155
        assert compare_side(rows["Away Team"]) == SAME  # but both read +155 on screen

    def test_no_sharp_price_means_no_verdict(self, cfg):
        from cfb_edge.edges import evaluate_market

        game = make_game({
            "draftkings": {"h2h": [Outcome("Home Team", -320), Outcome("Away Team", 260)]},
            "fanduel": {"h2h": [Outcome("Home Team", -310), Outcome("Away Team", 250)]},
        })
        assert {compare_side(r) for r in evaluate_market(game, "h2h", cfg)} == {None}

    def test_a_missing_row_has_no_verdict(self):
        assert compare_side(None) is None


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
