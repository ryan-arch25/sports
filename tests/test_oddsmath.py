"""Tests for the odds conversion and de-vig math."""

from __future__ import annotations

import math

import pytest

from cfb_edge.oddsmath import (
    OddsError,
    american_to_decimal,
    american_to_prob,
    decimal_to_american,
    devig,
    devig_american,
    devig_k,
    devig_multiplicative,
    devig_power,
    edge,
    ev_per_100,
    format_american,
    hold,
    kelly_fraction,
    kelly_stake,
    prob_to_american,
    prob_to_decimal,
)


class TestAmericanToDecimal:
    @pytest.mark.parametrize(
        "american,decimal",
        [
            (100, 2.0),
            (-100, 2.0),
            (150, 2.5),
            (-110, 1 + 100 / 110),
            (-150, 1 + 2 / 3),
            (260, 3.6),
            (-320, 1.3125),
        ],
    )
    def test_known_values(self, american, decimal):
        assert american_to_decimal(american) == pytest.approx(decimal)

    @pytest.mark.parametrize("american", [-450, -110, 100, 115, 2500])
    def test_round_trips_through_decimal(self, american):
        assert decimal_to_american(american_to_decimal(american)) == pytest.approx(american)

    def test_even_money_normalizes_to_plus_100(self):
        # -100 and +100 are the same price; the canonical form is +100.
        assert decimal_to_american(american_to_decimal(-100)) == pytest.approx(100)

    def test_decimal_must_exceed_one(self):
        with pytest.raises(OddsError):
            decimal_to_american(1.0)


class TestAmericanToProb:
    @pytest.mark.parametrize(
        "american,prob",
        [
            (100, 0.5),
            (-100, 0.5),
            (-110, 110 / 210),  # 0.5238...
            (110, 100 / 210),  # 0.4762...
            (-150, 0.6),
            (150, 0.4),
            (-200, 2 / 3),
            (200, 1 / 3),
        ],
    )
    def test_known_values(self, american, prob):
        assert american_to_prob(american) == pytest.approx(prob)

    def test_favorite_is_more_likely_than_underdog(self):
        assert american_to_prob(-250) > 0.5 > american_to_prob(250)

    @pytest.mark.parametrize("american", [-99, 0, 99, 50, -1])
    def test_rejects_impossible_prices(self, american):
        with pytest.raises(OddsError):
            american_to_prob(american)

    @pytest.mark.parametrize("bad", ["-110x", None, float("nan"), float("inf")])
    def test_rejects_non_numeric(self, bad):
        with pytest.raises(OddsError):
            american_to_prob(bad)

    def test_string_prices_are_accepted(self):
        # The API returns numbers, but JSON from other sources may be stringy.
        assert american_to_prob("-110") == pytest.approx(110 / 210)


class TestProbToAmerican:
    @pytest.mark.parametrize(
        "prob,american",
        [(0.5, 100), (0.6, -150), (0.4, 150), (2 / 3, -200), (1 / 3, 200)],
    )
    def test_known_values(self, prob, american):
        assert prob_to_american(prob) == pytest.approx(american)

    @pytest.mark.parametrize("prob", [0.05, 0.25, 0.4999, 0.5, 0.5001, 0.75, 0.97])
    def test_round_trips_through_probability(self, prob):
        assert american_to_prob(prob_to_american(prob)) == pytest.approx(prob)

    @pytest.mark.parametrize("prob", [0.0, 1.0, -0.2, 1.5])
    def test_rejects_out_of_range(self, prob):
        with pytest.raises(OddsError):
            prob_to_american(prob)

    def test_prob_to_decimal_is_the_inverse_of_implied_probability(self):
        assert prob_to_decimal(0.4) == pytest.approx(2.5)


class TestHold:
    def test_standard_juice_is_about_four_and_a_half_percent(self):
        assert hold([american_to_prob(-110), american_to_prob(-110)]) == pytest.approx(0.047619, abs=1e-6)

    def test_fair_market_has_no_hold(self):
        assert hold([0.5, 0.5]) == pytest.approx(0.0)


class TestDevig:
    def test_symmetric_juice_splits_evenly(self):
        assert devig_american([-110, -110]) == pytest.approx([0.5, 0.5])

    def test_sums_to_one(self):
        fair = devig_american([-145, 130])
        assert sum(fair) == pytest.approx(1.0)

    def test_known_two_way_example(self):
        # +150 / -170: raw 0.4000 and 0.6296, total 1.0296.
        fair = devig_multiplicative([american_to_prob(150), american_to_prob(-170)])
        assert fair[0] == pytest.approx(0.4 / (0.4 + 170 / 270), abs=1e-12)
        assert fair == pytest.approx([0.388489, 0.611511], abs=1e-6)

    def test_preserves_ratio_between_sides(self):
        raw = [american_to_prob(-145), american_to_prob(130)]
        fair = devig_multiplicative(raw)
        assert fair[0] / fair[1] == pytest.approx(raw[0] / raw[1])

    def test_favorite_stays_the_favorite(self):
        fair = devig_american([-250, 215])
        assert fair[0] > fair[1]

    def test_shorter_price_loses_more_to_the_vig(self):
        # Proportional de-vigging scales both sides by the same factor, so the
        # bigger raw probability sheds the larger absolute amount.
        raw = [american_to_prob(-250), american_to_prob(215)]
        fair = devig_multiplicative(raw)
        assert raw[0] - fair[0] > raw[1] - fair[1] > 0

    def test_already_fair_market_is_unchanged(self):
        assert devig([0.25, 0.75]) == pytest.approx([0.25, 0.75])
        assert devig_multiplicative([0.25, 0.75]) == pytest.approx([0.25, 0.75])

    def test_three_way_market_normalizes(self):
        fair = devig([0.4, 0.4, 0.3])
        assert sum(fair) == pytest.approx(1.0)
        assert fair[0] == pytest.approx(fair[1])

    def test_requires_two_sides(self):
        with pytest.raises(OddsError):
            devig([0.52])

    @pytest.mark.parametrize("probs", [[0.5, 0.0], [0.5, -0.1]])
    def test_rejects_non_positive_probabilities(self, probs):
        with pytest.raises(OddsError):
            devig(probs)

    def test_the_default_is_the_power_method(self):
        raw = [american_to_prob(-250), american_to_prob(215)]
        assert devig(raw) == pytest.approx(devig_power(raw))

    def test_the_method_can_be_chosen(self):
        raw = [american_to_prob(-250), american_to_prob(215)]
        assert devig(raw, method="multiplicative") == pytest.approx(devig_multiplicative(raw))

    def test_an_unknown_method_is_rejected(self):
        with pytest.raises(OddsError, match="unknown de-vig method"):
            devig([0.55, 0.5], method="shin")


class TestPowerDevig:
    """sum(p ** k) = 1, which takes the margin mostly off the longshot."""

    def test_sums_to_one(self):
        assert sum(devig_power([american_to_prob(-250), american_to_prob(215)])) == pytest.approx(1.0)

    def test_symmetric_juice_splits_evenly(self):
        assert devig_power([american_to_prob(-110)] * 2) == pytest.approx([0.5, 0.5])

    def test_an_already_fair_market_is_left_alone(self):
        assert devig_power([0.25, 0.75]) == pytest.approx([0.25, 0.75])
        assert devig_k([0.25, 0.75]) == pytest.approx(1.0)

    def test_the_exponent_is_above_one_for_a_vigged_market(self):
        assert devig_k([american_to_prob(-110)] * 2) > 1.0

    def test_longshots_come_out_lower_than_under_the_multiplicative_method(self):
        """The whole point: a 30% dog should not be handed back 30.8%."""
        raw = [american_to_prob(-250), american_to_prob(215)]
        power = devig_power(raw)
        proportional = devig_multiplicative(raw)
        assert power[1] < proportional[1]
        assert power[0] > proportional[0]

    def test_the_gap_widens_with_the_longshot(self):
        short = [american_to_prob(-140), american_to_prob(120)]
        long = [american_to_prob(-2000), american_to_prob(1200)]
        short_gap = devig_multiplicative(short)[1] - devig_power(short)[1]
        long_gap = devig_multiplicative(long)[1] - devig_power(long)[1]
        assert long_gap > short_gap > 0

    def test_known_value(self):
        # -250 / +215: raw 0.7143 and 0.3175, k solves to about 1.0537.
        fair = devig_power([american_to_prob(-250), american_to_prob(215)])
        assert fair == pytest.approx([0.701501, 0.298499], abs=1e-5)

    def test_the_favorite_stays_the_favorite(self):
        fair = devig_power([american_to_prob(-250), american_to_prob(215)])
        assert fair[0] > fair[1]

    def test_a_three_way_market_normalizes(self):
        fair = devig_power([0.4, 0.4, 0.3])
        assert sum(fair) == pytest.approx(1.0)
        assert fair[0] == pytest.approx(fair[1])

    def test_an_underround_market_is_pushed_up(self):
        """Arbitrage, or an averaged consensus: the sum is below one."""
        fair = devig_power([0.45, 0.50])
        assert sum(fair) == pytest.approx(1.0)
        assert fair[0] > 0.45 and fair[1] > 0.50

    def test_a_certainty_is_rejected(self):
        with pytest.raises(OddsError, match="below 1"):
            devig_power([1.0, 0.2])

    def test_needs_two_sides(self):
        with pytest.raises(OddsError):
            devig_power([0.52])

    def test_a_heavy_favorite_stays_inside_the_unit_interval(self):
        fair = devig_power([american_to_prob(-5000), american_to_prob(2500)])
        assert 0.0 < fair[1] < fair[0] < 1.0
        assert sum(fair) == pytest.approx(1.0)


class TestEdgeAndEv:
    def test_edge_is_fair_minus_offered(self):
        assert edge(0.55, -110) == pytest.approx(0.55 - 110 / 210)

    def test_edge_is_negative_when_the_price_is_short(self):
        assert edge(0.5, -120) < 0

    def test_ev_on_a_coin_flip_priced_at_even_money_is_zero(self):
        assert ev_per_100(0.5, 100) == pytest.approx(0.0)

    def test_ev_known_value(self):
        # 55% at -110 wins $90.91 or loses $100.
        assert ev_per_100(0.55, -110) == pytest.approx(5.0, abs=1e-9)

    def test_ev_matches_the_standard_juice_breakeven(self):
        assert ev_per_100(american_to_prob(-110), -110) == pytest.approx(0.0, abs=1e-9)

    def test_ev_is_negative_below_breakeven(self):
        assert ev_per_100(0.5, -110) < 0

    def test_rejects_impossible_probability(self):
        with pytest.raises(OddsError):
            ev_per_100(1.2, -110)


class TestKelly:
    def test_known_quarter_kelly_stake(self):
        # p=0.55 at -110: b=0.90909, f*=5.5%, quarter-Kelly = 1.375% of bankroll.
        assert kelly_fraction(0.55, -110) == pytest.approx(0.055, abs=1e-9)
        assert kelly_stake(0.55, -110, 10_000, fraction=0.25) == pytest.approx(137.5, abs=1e-6)

    def test_no_edge_means_no_bet(self):
        assert kelly_fraction(american_to_prob(-110), -110) == pytest.approx(0.0, abs=1e-12)
        assert kelly_stake(0.4, -110, 10_000) == 0.0

    def test_stake_scales_with_bankroll(self):
        small = kelly_stake(0.6, 100, 1_000)
        big = kelly_stake(0.6, 100, 10_000)
        assert big == pytest.approx(small * 10)

    def test_fraction_scales_the_stake(self):
        full = kelly_stake(0.6, 100, 1_000, fraction=1.0)
        quarter = kelly_stake(0.6, 100, 1_000, fraction=0.25)
        assert quarter == pytest.approx(full / 4)

    def test_max_bet_pct_caps_the_stake(self):
        capped = kelly_stake(0.9, 100, 1_000, fraction=1.0, max_bet_pct=2.0)
        assert capped == pytest.approx(20.0)

    def test_certainty_at_even_money_is_full_bankroll(self):
        assert kelly_fraction(1.0, 100) == pytest.approx(1.0)

    def test_negative_bankroll_is_rejected(self):
        with pytest.raises(OddsError):
            kelly_stake(0.6, 100, -1)


class TestFormatting:
    @pytest.mark.parametrize(
        "value,text",
        [(150, "+150"), (-110, "-110"), (-110.4, "-110"), (100, "+100"), (None, "-")],
    )
    def test_format_american(self, value, text):
        assert format_american(value) == text


def test_fair_price_of_a_devigged_market_is_worse_than_the_offered_price():
    """Sanity check tying the pieces together: -110/-110 de-vigs to +100 fair."""
    fair = devig_american([-110, -110])
    assert prob_to_american(fair[0]) == pytest.approx(100.0)
    assert math.isclose(ev_per_100(fair[0], -110), -4.545454, abs_tol=1e-5)
