"""Tests for the half-point value table."""

from __future__ import annotations

import pytest

from cfb_edge.halfpoint import (
    FAVORITE,
    OVER,
    SPREAD,
    TOTAL,
    UNDER,
    UNDERDOG,
    Distribution,
    HalfPointError,
    HalfPointTable,
    _threshold,
    build_table,
    load_table,
    market_kind,
    save_table,
    side_role,
)


def dist(counts: dict[int, int], reference: float = 3.0) -> Distribution:
    return Distribution(reference=reference, counts=counts, sample=sum(counts.values()))


class TestDistribution:
    def test_probabilities_sum_to_one(self):
        d = dist({1: 10, 2: 30, 3: 60})
        assert sum(d.pmf.values()) == pytest.approx(1.0)

    def test_exact_above_and_below_partition_the_mass(self):
        d = dist({1: 10, 3: 30, 5: 60})
        assert d.prob_exactly(3) + d.prob_above(3) + d.prob_below(3) == pytest.approx(1.0)

    def test_half_numbers_can_never_be_landed_on(self):
        assert dist({3: 100}).prob_exactly(3.5) == 0.0

    def test_cover_prob_excludes_pushes(self):
        # 20% of games land exactly on 3; of the rest, half are above.
        d = dist({1: 40, 3: 20, 5: 40})
        assert d.cover_prob(3, above=True) == pytest.approx(0.5)

    def test_cover_prob_at_a_half_number_has_no_push(self):
        d = dist({1: 40, 3: 20, 5: 40})
        assert d.cover_prob(3.5, above=True) == pytest.approx(0.4)

    def test_below_is_the_mirror_of_above(self):
        d = dist({1: 40, 3: 20, 5: 40})
        assert d.cover_prob(3, above=False) == pytest.approx(1 - d.cover_prob(3, above=True))

    def test_empty_distribution_is_inert(self):
        empty = Distribution(3.0, {}, 0)
        assert empty.pmf == {}
        assert empty.prob_above(0) == 0.0


class TestThresholds:
    @pytest.mark.parametrize(
        "kind,role,point,expected",
        [
            (SPREAD, FAVORITE, -3.5, 3.5),   # favorite needs to win by more than 3.5
            (SPREAD, FAVORITE, -3.0, 3.0),
            (SPREAD, UNDERDOG, 3.5, 3.5),    # dog covers when the favorite wins by less
            (TOTAL, OVER, 51.5, 51.5),
            (TOTAL, UNDER, 51.5, 51.5),
        ],
    )
    def test_threshold(self, kind, role, point, expected):
        assert _threshold(kind, role, point) == expected

    @pytest.mark.parametrize(
        "market,kind",
        [
            ("spreads", SPREAD),
            ("alternate_spreads", SPREAD),
            ("totals", TOTAL),
            ("alternate_totals", TOTAL),
            ("h2h", None),
            ("player_pass_yds", None),
            ("team_totals", None),  # a team's own points, not the game's
        ],
    )
    def test_market_kind(self, market, kind):
        assert market_kind(market) == kind

    @pytest.mark.parametrize(
        "kind,side,point,role",
        [
            (SPREAD, "Alabama", -3.5, FAVORITE),
            (SPREAD, "Georgia", 3.5, UNDERDOG),
            (SPREAD, "Pick", 0.0, FAVORITE),
            (TOTAL, "Over", 51.5, OVER),
            (TOTAL, "Under", 51.5, UNDER),
            (TOTAL, "Jalen Milroe Over", 249.5, OVER),
            (TOTAL, "Neither", 51.5, None),
            (SPREAD, "Alabama", None, None),
        ],
    )
    def test_side_role(self, kind, side, point, role):
        assert side_role(kind, side, point) == role


class TestTranslation:
    def test_a_better_total_for_the_over_raises_its_probability(self, halfpoint_table):
        moved = halfpoint_table.translate(TOTAL, OVER, 0.50, 53.0, 51.5)
        assert moved is not None and moved > 0.50

    def test_the_same_move_hurts_the_under(self, halfpoint_table):
        moved = halfpoint_table.translate(TOTAL, UNDER, 0.50, 53.0, 51.5)
        assert moved is not None and moved < 0.50

    def test_over_and_under_stay_complementary(self, halfpoint_table):
        over = halfpoint_table.translate(TOTAL, OVER, 0.50, 53.0, 51.5)
        under = halfpoint_table.translate(TOTAL, UNDER, 0.50, 53.0, 51.5)
        assert over + under == pytest.approx(1.0, abs=1e-9)

    def test_buying_half_a_point_helps_the_favorite(self, halfpoint_table):
        moved = halfpoint_table.translate(SPREAD, FAVORITE, 0.50, -3.0, -2.5)
        assert moved is not None and moved > 0.50

    def test_laying_more_hurts_the_favorite(self, halfpoint_table):
        moved = halfpoint_table.translate(SPREAD, FAVORITE, 0.50, -3.0, -3.5)
        assert moved is not None and moved < 0.50

    def test_the_underdog_moves_the_other_way(self, halfpoint_table):
        favorite = halfpoint_table.translate(SPREAD, FAVORITE, 0.50, -3.0, -3.5)
        underdog = halfpoint_table.translate(SPREAD, UNDERDOG, 0.50, 3.0, 3.5)
        assert underdog > 0.50 > favorite

    def test_no_move_changes_nothing(self, halfpoint_table):
        assert halfpoint_table.translate(SPREAD, FAVORITE, 0.62, -7.0, -7.0) == pytest.approx(0.62)

    def test_the_key_number_costs_more_than_a_quiet_one(self, halfpoint_table):
        off_three = 0.5 - halfpoint_table.translate(SPREAD, FAVORITE, 0.50, -3.0, -3.5)
        off_six = 0.5 - halfpoint_table.translate(SPREAD, FAVORITE, 0.50, -6.0, -6.5)
        assert off_three > off_six

    def test_a_move_past_the_guard_is_refused(self, halfpoint_table):
        assert halfpoint_table.translate(SPREAD, FAVORITE, 0.50, -3.0, -9.0) is None

    def test_a_lopsided_sharp_price_is_refused(self, halfpoint_table):
        assert halfpoint_table.translate(SPREAD, FAVORITE, 0.98, -3.0, -3.5) is None

    def test_a_thin_sample_is_refused(self):
        table = HalfPointTable(spreads={3.0: dist({1: 5, 3: 5})}, min_sample=200)
        assert table.translate(SPREAD, FAVORITE, 0.5, -3.0, -3.5) is None

    def test_an_empty_table_is_refused(self):
        assert HalfPointTable().translate(SPREAD, FAVORITE, 0.5, -3.0, -3.5) is None

    def test_a_result_off_the_end_of_the_scale_is_refused(self):
        # 80% of games land exactly on 3, so crossing it swings the
        # probability further than the floor allows.
        table = HalfPointTable(spreads={3.0: dist({3: 800, 5: 200})}, min_sample=10)
        assert table.translate(SPREAD, FAVORITE, 0.50, -3.0, -3.5) is None

    def test_mass_sitting_on_the_number_is_what_the_half_point_buys(self):
        table = HalfPointTable(spreads={3.0: dist({3: 200, 5: 800})}, min_sample=10)
        moved = table.translate(SPREAD, FAVORITE, 0.50, -3.0, -3.5)
        # Laying 3.5 turns every push on 3 into a loss.
        assert moved == pytest.approx(0.50 + (0.8 - 1.0))

    def test_the_nearest_reference_line_is_used(self, halfpoint_table):
        assert halfpoint_table.distribution(SPREAD, 3.4).reference == 3.0
        assert halfpoint_table.distribution(SPREAD, 3.6).reference == 4.0


class TestBuild:
    def test_bins_by_the_closing_number(self, synthetic_history):
        table = build_table(synthetic_history, window=1.5, min_sample=50)
        assert table.spreads and table.totals
        assert table.meta["games"] == len(synthetic_history)
        assert table.meta["games_with_spread"] > 0

    def test_key_numbers_carry_more_mass(self, halfpoint_table):
        three = halfpoint_table.distribution(SPREAD, 3.0)
        assert three.prob_exactly(3) > three.prob_exactly(2)
        assert three.prob_exactly(3) > three.prob_exactly(5)

    def test_half_point_values_are_reported_per_line(self, halfpoint_table):
        rows = halfpoint_table.half_point_values(SPREAD, max_number=10)
        assert rows and all(r["prob_gain"] > 0 for r in rows)
        by_line = {r["reference"]: r["prob_gain"] for r in rows}
        assert by_line[3.0] > by_line[6.0]

    def test_thin_reference_lines_are_left_out_of_the_display(self):
        table = HalfPointTable(spreads={3.0: dist({1: 5})}, min_sample=200)
        assert table.half_point_values(SPREAD) == []

    def test_a_pickem_reference_does_not_render_negative_zero(self, halfpoint_table):
        rows = halfpoint_table.half_point_values(SPREAD, max_number=1)
        assert rows[0]["reference"] == 0.0


class TestPersistence:
    def test_round_trips_through_json(self, tmp_path, halfpoint_table):
        path = save_table(halfpoint_table, tmp_path / "hp.json")
        loaded = load_table(path)
        assert loaded is not None
        assert loaded.meta["games"] == halfpoint_table.meta["games"]
        assert loaded.translate(SPREAD, FAVORITE, 0.5, -3.0, -3.5) == pytest.approx(
            halfpoint_table.translate(SPREAD, FAVORITE, 0.5, -3.0, -3.5)
        )

    def test_guards_survive_the_round_trip(self, tmp_path):
        table = HalfPointTable(spreads={3.0: dist({1: 500})}, max_move=1.0, min_sample=7)
        loaded = load_table(save_table(table, tmp_path / "hp.json"))
        assert (loaded.max_move, loaded.min_sample) == (1.0, 7)

    def test_a_missing_table_is_not_an_error(self, tmp_path):
        assert load_table(tmp_path / "nope.json") is None

    def test_a_future_version_is_rejected(self, tmp_path):
        path = tmp_path / "hp.json"
        path.write_text('{"version": 99, "spreads": {}, "totals": {}}', encoding="utf-8")
        with pytest.raises(HalfPointError, match="not supported"):
            load_table(path)

    def test_unreadable_json_is_reported(self, tmp_path):
        path = tmp_path / "hp.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(HalfPointError):
            load_table(path)
