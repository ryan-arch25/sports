"""Tests for sharp-book selection and fair-line construction."""

from __future__ import annotations

import pytest

from cfb_edge.fair import fair_from_book, fair_from_consensus, fair_line, resolve_book
from cfb_edge.models import BookMarket, Outcome
from cfb_edge.oddsmath import american_to_prob

from conftest import make_game


def totals(book: str, point: float, over: float, under: float) -> BookMarket:
    return BookMarket(
        book=book,
        market="totals",
        outcomes=(Outcome("Over", over, point), Outcome("Under", under, point)),
    )


class TestFairFromBook:
    def test_devigs_a_two_sided_market(self):
        line = fair_from_book(totals("pinnacle", 53.0, -105, -105), "pinnacle")
        assert [s.fair_prob for s in line.sides] == pytest.approx([0.5, 0.5])
        assert line.hold == pytest.approx(0.0244, abs=1e-4)
        assert line.label == "pinnacle"

    def test_keeps_the_sharp_number_and_price(self):
        line = fair_from_book(totals("pinnacle", 53.0, -110, -103), "pinnacle")
        over = line.side("Over")
        assert over.point == 53.0
        assert over.price == -110

    def test_rejects_a_one_sided_market(self):
        bm = BookMarket("pinnacle", "totals", (Outcome("Over", -110, 53.0),))
        assert fair_from_book(bm, "pinnacle") is None

    def test_rejects_an_unusable_price(self):
        assert fair_from_book(totals("pinnacle", 53.0, -110, 0), "pinnacle") is None


class TestConsensus:
    def test_averages_devigged_probabilities(self):
        books = [totals("fanduel", 44.5, -110, -110), totals("betmgm", 44.5, -108, -112)]
        line = fair_from_consensus(books)
        assert line.source == "consensus"
        assert line.label == "consensus(2)"
        assert sum(s.fair_prob for s in line.sides) == pytest.approx(1.0)
        assert line.side("Over").fair_prob == pytest.approx(0.49783, abs=1e-4)

    def test_ignores_books_on_a_different_number(self):
        books = [
            totals("fanduel", 44.5, -110, -110),
            totals("betmgm", 44.5, -108, -112),
            totals("williamhill_us", 45.5, -110, -110),
        ]
        line = fair_from_consensus(books)
        assert line.books == ("fanduel", "betmgm")
        assert line.side("Over").point == 44.5

    def test_requires_a_minimum_number_of_books(self):
        assert fair_from_consensus([totals("fanduel", 44.5, -110, -110)], min_books=2) is None
        assert fair_from_consensus([totals("fanduel", 44.5, -110, -110)], min_books=1) is not None

    def test_ties_go_to_the_lower_hold_group(self):
        books = [
            totals("fanduel", 44.5, -120, -120),
            totals("betmgm", 44.5, -120, -120),
            totals("williamhill_us", 45.5, -102, -102),
            totals("draftkings", 45.5, -102, -102),
        ]
        line = fair_from_consensus(books)
        assert line.side("Over").point == 45.5

    def test_moneyline_books_group_together_without_a_number(self):
        books = [
            BookMarket(b, "h2h", (Outcome("Texas", price_a), Outcome("Oklahoma", price_b)))
            for b, price_a, price_b in [
                ("fanduel", -180, 155), ("betmgm", -175, 150), ("williamhill_us", -185, 160)
            ]
        ]
        line = fair_from_consensus(books)
        assert line.label == "consensus(3)"
        assert line.side("Texas").fair_prob > 0.6

    def test_representative_price_is_the_average_offered_price(self):
        books = [totals("fanduel", 44.5, -110, -110), totals("betmgm", 44.5, -110, -110)]
        line = fair_from_consensus(books)
        assert american_to_prob(line.side("Over").price) == pytest.approx(american_to_prob(-110))


class TestBookPriority:
    def test_pinnacle_wins_when_present(self, cfg):
        game = make_game({
            "pinnacle": {"totals": [Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)]},
            "circasports": {"totals": [Outcome("Over", -110, 52.0), Outcome("Under", -110, 52.0)]},
            "fanduel": {"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
        })
        line = fair_line(game, "totals", cfg)
        assert line.source == "pinnacle"
        assert line.side("Over").point == 53.0

    def test_circa_is_the_fallback(self, cfg):
        game = make_game({
            "circasports": {"totals": [Outcome("Over", -110, 52.0), Outcome("Under", -110, 52.0)]},
            "fanduel": {"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
            "betmgm": {"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
        })
        line = fair_line(game, "totals", cfg)
        assert line.source == "circa"

    def test_falls_through_to_consensus(self, cfg):
        game = make_game({
            "fanduel": {"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
            "betmgm": {"totals": [Outcome("Over", -112, 51.5), Outcome("Under", -108, 51.5)]},
        })
        line = fair_line(game, "totals", cfg)
        assert line.source == "consensus"

    def test_falls_through_when_the_sharp_book_skipped_this_market(self, cfg):
        game = make_game({
            "pinnacle": {"h2h": [Outcome("Home Team", -140), Outcome("Away Team", 125)]},
            "fanduel": {"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
            "betmgm": {"totals": [Outcome("Over", -112, 51.5), Outcome("Under", -108, 51.5)]},
        })
        assert fair_line(game, "totals", cfg).source == "consensus"
        assert fair_line(game, "h2h", cfg).source == "pinnacle"

    def test_no_line_when_nothing_qualifies(self, cfg):
        game = make_game({
            "fanduel": {"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
        })
        assert fair_line(game, "totals", cfg) is None

    def test_draftkings_is_never_part_of_its_own_benchmark(self, cfg):
        cfg.consensus_books = ("draftkings", "fanduel", "betmgm")
        game = make_game({
            "draftkings": {"totals": [Outcome("Over", 200, 51.5), Outcome("Under", -300, 51.5)]},
            "fanduel": {"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
            "betmgm": {"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
        })
        line = fair_line(game, "totals", cfg)
        assert "draftkings" not in line.books
        assert line.side("Over").fair_prob == pytest.approx(0.5)

    def test_book_aliases_are_resolved(self, cfg):
        game = make_game({"circa": {"h2h": [Outcome("A", -110), Outcome("B", -110)]}})
        assert resolve_book(game, "circa", cfg.aliases) == "circa"
        assert resolve_book(game, "pinnacle", cfg.aliases) is None
        assert fair_line(game, "h2h", cfg).source == "circa"


class TestAgainstTheSampleBoard:
    def test_pinnacle_game(self, games_by_id, cfg):
        line = fair_line(games_by_id["g1alabama"], "h2h", cfg)
        assert line.source == "pinnacle"
        assert line.side("Georgia Bulldogs").fair_prob == pytest.approx(0.4235, abs=1e-3)

    def test_circa_game(self, games_by_id, cfg):
        line = fair_line(games_by_id["g2michigan"], "spreads", cfg)
        assert line.label == "circa"
        assert line.side("Michigan Wolverines").fair_prob == pytest.approx(0.5)

    def test_consensus_game_drops_the_odd_number_out(self, games_by_id, cfg):
        line = fair_line(games_by_id["g3texas"], "totals", cfg)
        assert line.label == "consensus(2)"
        assert line.side("Over").point == 44.5

    def test_one_soft_book_is_not_a_consensus(self, games_by_id, cfg):
        assert fair_line(games_by_id["g4boise"], "h2h", cfg) is None
