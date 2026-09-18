"""Tests for DraftKings-vs-sharp edge computation."""

from __future__ import annotations

import pytest

from cfb_edge.edges import (
    DIFFERENT_NUMBER,
    TRANSLATED,
    NO_SHARP_LINE,
    NO_SHARP_SIDE,
    PRICED,
    different_number_rows,
    evaluate_games,
    evaluate_market,
    format_pick,
    rank,
    summarize,
)
from cfb_edge.models import Outcome
from cfb_edge.oddsmath import american_to_prob

from conftest import make_game


def two_book_game(dk, sharp, market="totals"):
    return make_game({"draftkings": {market: dk}, "pinnacle": {market: sharp}})


class TestMatchingNumbers:
    def test_edge_is_fair_minus_dk_implied(self, cfg):
        game = two_book_game(
            dk=[Outcome("Over", 110, 44.5), Outcome("Under", -130, 44.5)],
            sharp=[Outcome("Over", -110, 44.5), Outcome("Under", -110, 44.5)],
        )
        over = next(r for r in evaluate_market(game, "totals", cfg) if r.side == "Over")
        assert over.status == PRICED
        assert over.fair_prob == pytest.approx(0.5)
        assert over.dk_prob == pytest.approx(american_to_prob(110))
        assert over.edge == pytest.approx(0.5 - 100 / 210)
        assert over.edge_pct == pytest.approx(2.381, abs=1e-3)

    def test_ev_and_stake_come_from_the_fair_probability(self, cfg):
        game = two_book_game(
            dk=[Outcome("Over", 110, 44.5), Outcome("Under", -130, 44.5)],
            sharp=[Outcome("Over", -110, 44.5), Outcome("Under", -110, 44.5)],
        )
        over = next(r for r in evaluate_market(game, "totals", cfg) if r.side == "Over")
        assert over.ev_per_100 == pytest.approx(5.0, abs=1e-9)
        # Quarter Kelly on a 2.38% edge at +110 with a $10k bankroll.
        assert over.stake == pytest.approx(10_000 * 0.25 * (0.5 * 2.1 - 1) / 1.1, abs=1e-6)

    def test_fair_american_is_reported(self, cfg):
        game = two_book_game(
            dk=[Outcome("Over", 110, 44.5), Outcome("Under", -130, 44.5)],
            sharp=[Outcome("Over", -110, 44.5), Outcome("Under", -110, 44.5)],
        )
        over = next(r for r in evaluate_market(game, "totals", cfg) if r.side == "Over")
        assert over.fair_american == pytest.approx(100.0)

    def test_the_juiced_side_shows_a_negative_edge(self, cfg):
        game = two_book_game(
            dk=[Outcome("Over", 110, 44.5), Outcome("Under", -130, 44.5)],
            sharp=[Outcome("Over", -110, 44.5), Outcome("Under", -110, 44.5)],
        )
        under = next(r for r in evaluate_market(game, "totals", cfg) if r.side == "Under")
        assert under.status == PRICED
        assert under.edge < 0
        assert under.stake == 0.0

    def test_moneylines_always_match_on_number(self, cfg):
        game = make_game({
            "draftkings": {"h2h": [Outcome("Away Team", 145), Outcome("Home Team", -175)]},
            "pinnacle": {"h2h": [Outcome("Away Team", 130), Outcome("Home Team", -145)]},
        })
        rows = evaluate_market(game, "h2h", cfg)
        assert {r.status for r in rows} == {PRICED}
        away = next(r for r in rows if r.side == "Away Team")
        assert away.edge_pct == pytest.approx(1.23, abs=1e-2)

    def test_spread_sides_match_on_their_own_number(self, cfg):
        game = two_book_game(
            market="spreads",
            dk=[Outcome("Home Team", -125, -3.5), Outcome("Away Team", 105, 3.5)],
            sharp=[Outcome("Home Team", -105, -3.5), Outcome("Away Team", -105, 3.5)],
        )
        rows = evaluate_market(game, "spreads", cfg)
        assert {r.status for r in rows} == {PRICED}
        assert next(r for r in rows if r.side == "Away Team").edge_pct == pytest.approx(1.22, abs=1e-2)


class TestDifferentNumber:
    def test_total_off_the_sharp_number_is_flagged_not_priced(self, cfg):
        game = two_book_game(
            dk=[Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)],
            sharp=[Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)],
        )
        rows = evaluate_market(game, "totals", cfg)
        assert {r.status for r in rows} == {DIFFERENT_NUMBER}
        under = next(r for r in rows if r.side == "Under")
        assert under.dk_point == 51.5
        assert under.sharp_point == 53.0
        assert under.edge is None and under.ev_per_100 is None and under.stake is None
        assert "51.5" in under.note and "53" in under.note

    def test_half_point_difference_on_a_spread_is_flagged(self, cfg):
        game = two_book_game(
            market="spreads",
            dk=[Outcome("Home Team", -110, -3.0), Outcome("Away Team", -110, 3.0)],
            sharp=[Outcome("Home Team", -110, -3.5), Outcome("Away Team", -110, 3.5)],
        )
        assert {r.status for r in evaluate_market(game, "spreads", cfg)} == {DIFFERENT_NUMBER}

    def test_flagged_rows_are_excluded_from_the_ranking(self, cfg):
        game = two_book_game(
            dk=[Outcome("Over", 200, 51.5), Outcome("Under", -250, 51.5)],
            sharp=[Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)],
        )
        rows = evaluate_market(game, "totals", cfg)
        assert rank(rows, 1.0) == []
        assert len(different_number_rows(rows)) == 2


class TestMissingData:
    def test_no_sharp_line_is_recorded_not_dropped(self, cfg):
        game = make_game({
            "draftkings": {"h2h": [Outcome("Home Team", -320), Outcome("Away Team", 260)]},
            "fanduel": {"h2h": [Outcome("Home Team", -310), Outcome("Away Team", 250)]},
        })
        rows = evaluate_market(game, "h2h", cfg)
        assert {r.status for r in rows} == {NO_SHARP_LINE}
        assert all(r.edge is None for r in rows)

    def test_no_draftkings_price_means_no_rows(self, cfg):
        game = make_game({
            "pinnacle": {"h2h": [Outcome("Home Team", -145), Outcome("Away Team", 130)]},
        })
        assert evaluate_market(game, "h2h", cfg) == []

    def test_team_naming_mismatch_is_reported(self, cfg):
        game = make_game({
            "draftkings": {"h2h": [Outcome("Home Team", -175), Outcome("Away Team", 145)]},
            "pinnacle": {"h2h": [Outcome("Home Squad", -145), Outcome("Away Squad", 130)]},
        })
        assert {r.status for r in evaluate_market(game, "h2h", cfg)} == {NO_SHARP_SIDE}


class TestRanking:
    def test_sorted_by_edge_descending_and_filtered(self, sample_games, cfg):
        rows = evaluate_games(sample_games, cfg, ("h2h", "spreads", "totals"))
        bets = rank(rows, 1.0)
        assert [b.edge_pct for b in bets] == sorted((b.edge_pct for b in bets), reverse=True)
        assert all(b.edge_pct >= 1.0 for b in bets)
        assert bets[0].side == "Michigan Wolverines"
        assert bets[0].edge_pct == pytest.approx(2.38, abs=1e-2)

    def test_min_edge_threshold_is_applied(self, sample_games, cfg):
        rows = evaluate_games(sample_games, cfg, ("h2h", "spreads", "totals"))
        assert len(rank(rows, 0.0)) > len(rank(rows, 1.5)) > len(rank(rows, 2.5))

    def test_market_filter_limits_the_rows(self, sample_games, cfg):
        rows = evaluate_games(sample_games, cfg, ("totals",))
        assert {r.market for r in rows} == {"totals"}

    def test_summary_counts_every_evaluated_line(self, sample_games, cfg):
        rows = evaluate_games(sample_games, cfg, ("h2h", "spreads", "totals"))
        counts = summarize(rows)
        assert counts[PRICED] == 16
        assert counts[DIFFERENT_NUMBER] == 2
        assert counts[NO_SHARP_LINE] == 2
        assert sum(counts.values()) == len(rows)


class TestPickLabels:
    @pytest.mark.parametrize(
        "market,side,point,expected",
        [
            ("h2h", "Georgia Bulldogs", None, "Georgia Bulldogs ML"),
            ("spreads", "Georgia Bulldogs", 3.5, "Georgia Bulldogs +3.5"),
            ("spreads", "Alabama Crimson Tide", -3.5, "Alabama Crimson Tide -3.5"),
            ("totals", "Under", 51.5, "Under 51.5"),
            ("totals", "Over", 44.0, "Over 44"),
        ],
    )
    def test_format_pick(self, market, side, point, expected):
        assert format_pick(market, side, point) == expected


class TestTranslatedNumbers:
    """With a half-point table, a different number is priced instead of skipped."""

    def test_a_total_off_the_sharp_number_is_priced(self, cfg, halfpoint_table):
        game = two_book_game(
            dk=[Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)],
            sharp=[Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)],
        )
        rows = evaluate_market(game, "totals", cfg, halfpoint_table)
        assert {r.status for r in rows} == {TRANSLATED}
        over = next(r for r in rows if r.side == "Over")
        assert over.fair_prob > 0.5  # a lower total is a better Over
        assert over.edge is not None and over.stake is not None
        assert over.translated_from == 53.0
        assert over.sharp_point == 53.0 and over.dk_point == 51.5

    def test_the_two_sides_move_in_opposite_directions(self, cfg, halfpoint_table):
        game = two_book_game(
            dk=[Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)],
            sharp=[Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)],
        )
        rows = {r.side: r for r in evaluate_market(game, "totals", cfg, halfpoint_table)}
        assert rows["Over"].fair_prob > 0.5 > rows["Under"].fair_prob
        assert rows["Over"].fair_prob + rows["Under"].fair_prob == pytest.approx(1.0)

    def test_the_note_records_the_move(self, cfg, halfpoint_table):
        game = two_book_game(
            dk=[Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)],
            sharp=[Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)],
        )
        over = next(r for r in evaluate_market(game, "totals", cfg, halfpoint_table)
                    if r.side == "Over")
        assert "half-point table" in over.note
        assert "53" in over.note and "51.5" in over.note

    def test_a_spread_off_the_sharp_number_is_priced(self, cfg, halfpoint_table):
        game = two_book_game(
            market="spreads",
            dk=[Outcome("Home Team", -110, -2.5), Outcome("Away Team", -110, 2.5)],
            sharp=[Outcome("Home Team", -110, -3.0), Outcome("Away Team", -110, 3.0)],
        )
        rows = {r.side: r for r in evaluate_market(game, "spreads", cfg, halfpoint_table)}
        assert rows["Home Team"].status == TRANSLATED
        assert rows["Home Team"].fair_prob > 0.5  # -2.5 is better than -3
        assert rows["Away Team"].fair_prob < 0.5

    def test_translated_rows_are_ranked_with_the_rest(self, cfg, halfpoint_table):
        game = two_book_game(
            dk=[Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)],
            sharp=[Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)],
        )
        rows = evaluate_market(game, "totals", cfg, halfpoint_table)
        assert len(rank(rows, 1.0)) == 1
        assert different_number_rows(rows) == []

    def test_a_move_beyond_the_guard_is_still_flagged(self, cfg, halfpoint_table):
        game = two_book_game(
            dk=[Outcome("Over", -110, 44.0), Outcome("Under", -110, 44.0)],
            sharp=[Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)],
        )
        assert {r.status for r in evaluate_market(game, "totals", cfg, halfpoint_table)} == {
            DIFFERENT_NUMBER
        }

    def test_moneylines_are_never_translated(self, cfg, halfpoint_table):
        game = make_game({
            "draftkings": {"h2h": [Outcome("Away Team", 145), Outcome("Home Team", -175)]},
            "pinnacle": {"h2h": [Outcome("Away Team", 130), Outcome("Home Team", -145)]},
        })
        assert {r.status for r in evaluate_market(game, "h2h", cfg, halfpoint_table)} == {PRICED}

    def test_a_prop_on_a_different_number_is_not_translated(self, cfg, halfpoint_table):
        """The table describes game margins and totals, not passing yards."""
        game = make_game({
            "draftkings": {"player_pass_yds": [
                Outcome("Over", -110, 249.5, "QB One"),
                Outcome("Under", -110, 249.5, "QB One"),
            ]},
            "pinnacle": {"player_pass_yds": [
                Outcome("Over", -105, 251.5, "QB One"),
                Outcome("Under", -105, 251.5, "QB One"),
            ]},
        })
        rows = evaluate_market(game, "player_pass_yds", cfg, halfpoint_table)
        assert {r.status for r in rows} == {DIFFERENT_NUMBER}

    def test_without_a_table_nothing_is_translated(self, cfg):
        game = two_book_game(
            dk=[Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)],
            sharp=[Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)],
        )
        assert {r.status for r in evaluate_market(game, "totals", cfg, None)} == {
            DIFFERENT_NUMBER
        }


class TestLineDiffOnRows:
    def test_a_moved_total_reports_the_difference_on_both_sides(self, cfg, halfpoint_table):
        game = two_book_game(
            dk=[Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)],
            sharp=[Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)],
        )
        rows = {r.side: r for r in evaluate_market(game, "totals", cfg, halfpoint_table)}
        assert rows["Over"].line_diff == pytest.approx(1.5)   # DK's 51.5 helps the Over
        assert rows["Under"].line_diff == pytest.approx(-1.5)  # and hurts the Under

    def test_a_moved_spread_reports_the_difference(self, cfg, halfpoint_table):
        game = two_book_game(
            market="spreads",
            dk=[Outcome("Home Team", -110, -2.5), Outcome("Away Team", -110, 2.5)],
            sharp=[Outcome("Home Team", -110, -3.0), Outcome("Away Team", -110, 3.0)],
        )
        rows = {r.side: r for r in evaluate_market(game, "spreads", cfg, halfpoint_table)}
        assert rows["Home Team"].line_diff == pytest.approx(0.5)
        assert rows["Away Team"].line_diff == pytest.approx(-0.5)

    def test_the_same_number_is_a_zero_difference(self, cfg):
        game = two_book_game(
            dk=[Outcome("Over", 110, 44.5), Outcome("Under", -130, 44.5)],
            sharp=[Outcome("Over", -110, 44.5), Outcome("Under", -110, 44.5)],
        )
        assert all(r.line_diff == 0 for r in evaluate_market(game, "totals", cfg))

    def test_a_moneyline_has_no_number_to_compare(self, cfg):
        game = make_game({
            "draftkings": {"h2h": [Outcome("Away Team", 145), Outcome("Home Team", -175)]},
            "pinnacle": {"h2h": [Outcome("Away Team", 130), Outcome("Home Team", -145)]},
        })
        assert all(r.line_diff is None for r in evaluate_market(game, "h2h", cfg))

    def test_a_flagged_row_still_reports_the_difference(self, cfg, halfpoint_table):
        """Too far to price is not too far to describe."""
        game = two_book_game(
            dk=[Outcome("Over", -110, 44.0), Outcome("Under", -110, 44.0)],
            sharp=[Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)],
        )
        rows = {r.side: r for r in evaluate_market(game, "totals", cfg, halfpoint_table)}
        assert rows["Over"].status == DIFFERENT_NUMBER
        assert rows["Over"].line_diff == pytest.approx(9.0)

    def test_no_sharp_side_means_no_difference(self, cfg):
        game = make_game({
            "draftkings": {"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
            "fanduel": {"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
        })
        rows = evaluate_market(game, "totals", cfg)
        assert {r.status for r in rows} == {NO_SHARP_LINE}
        assert all(r.line_diff is None for r in rows)

    def test_a_prop_reports_its_difference_in_its_own_units(self, cfg):
        """The table will not price a prop, but the number gap is still visible."""
        game = make_game({
            "draftkings": {"player_pass_yds": [
                Outcome("Over", -110, 249.5, "QB One"), Outcome("Under", -110, 249.5, "QB One"),
            ]},
            "pinnacle": {"player_pass_yds": [
                Outcome("Over", -105, 251.5, "QB One"), Outcome("Under", -105, 251.5, "QB One"),
            ]},
        })
        rows = {r.side: r for r in evaluate_market(game, "player_pass_yds", cfg)}
        # 249.5 yards instead of 251.5 is two yards in the Over's favour.
        assert rows["QB One Over"].line_diff == pytest.approx(2.0)
        assert rows["QB One Under"].line_diff == pytest.approx(-2.0)


class TestEstimatedPricing:
    """Lines off the sharp number get priced even with no table built."""

    def moved_total(self):
        return two_book_game(
            dk=[Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)],
            sharp=[Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)],
        )

    def test_the_estimate_prices_what_would_otherwise_be_skipped(self, cfg):
        from cfb_edge.halfpoint import estimated_table

        rows = evaluate_market(self.moved_total(), "totals", cfg, estimated_table(cfg))
        assert {r.status for r in rows} == {TRANSLATED}
        assert all(r.is_estimated for r in rows)
        assert all(r.translation_source == "estimated" for r in rows)

    def test_the_move_matches_the_published_value(self, cfg):
        from cfb_edge.halfpoint import estimated_table

        rows = {r.side: r for r in evaluate_market(
            self.moved_total(), "totals", cfg, estimated_table(cfg)
        )}
        # Pinnacle -105/-105 is a coin flip; three half points at 2.0 each.
        assert rows["Over"].fair_prob == pytest.approx(0.560)
        assert rows["Under"].fair_prob == pytest.approx(0.440)

    def test_the_note_says_where_the_price_came_from(self, cfg):
        from cfb_edge.halfpoint import estimated_table

        row = evaluate_market(self.moved_total(), "totals", cfg, estimated_table(cfg))[0]
        assert "published half-point estimate" in row.note

    def test_a_built_table_wins_when_both_are_available(self, cfg, halfpoint_table):
        from cfb_edge.halfpoint import estimated_table

        rows = evaluate_market(
            self.moved_total(), "totals", cfg, [halfpoint_table, estimated_table(cfg)]
        )
        assert all(r.translation_source == "table" for r in rows)
        assert not any(r.is_estimated for r in rows)

    def test_the_estimate_catches_what_the_built_table_refuses(self, cfg):
        """A reference line with no sample still gets priced."""
        from cfb_edge.halfpoint import HalfPointTable, estimated_table

        thin = HalfPointTable(totals={}, min_sample=200)
        rows = evaluate_market(self.moved_total(), "totals", cfg, [thin, estimated_table(cfg)])
        assert all(r.translation_source == "estimated" for r in rows)

    def test_both_guards_still_apply(self, cfg):
        from cfb_edge.halfpoint import estimated_table

        far = two_book_game(
            dk=[Outcome("Over", -110, 44.0), Outcome("Under", -110, 44.0)],
            sharp=[Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)],
        )
        rows = evaluate_market(far, "totals", cfg, estimated_table(cfg))
        assert {r.status for r in rows} == {DIFFERENT_NUMBER}
        assert all(r.translation_source is None for r in rows)

    def test_a_priced_row_is_never_marked_estimated(self, cfg):
        from cfb_edge.halfpoint import estimated_table

        game = two_book_game(
            dk=[Outcome("Over", 110, 44.5), Outcome("Under", -130, 44.5)],
            sharp=[Outcome("Over", -110, 44.5), Outcome("Under", -110, 44.5)],
        )
        rows = evaluate_market(game, "totals", cfg, estimated_table(cfg))
        assert {r.status for r in rows} == {PRICED}
        assert not any(r.is_estimated for r in rows)

    def test_the_line_diff_still_reads_the_same(self, cfg):
        from cfb_edge.halfpoint import estimated_table

        rows = {r.side: r for r in evaluate_market(
            self.moved_total(), "totals", cfg, estimated_table(cfg)
        )}
        assert rows["Over"].line_diff == pytest.approx(1.5)
        assert rows["Under"].line_diff == pytest.approx(-1.5)
