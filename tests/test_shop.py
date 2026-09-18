"""Tests for the best-price-across-books column."""

from __future__ import annotations

import pytest

from cfb_edge.config import Config
from cfb_edge.models import Outcome
from cfb_edge.shop import (
    best_offers,
    book_label,
    book_name,
    serialize_offer,
    shop_board,
)

from conftest import make_game


def shopping_game(**books):
    """A game where pinnacle is the reference and the rest are shoppable."""
    return make_game(books)


class TestBestOffer:
    def test_the_cheapest_juice_on_the_same_number_wins(self, cfg):
        game = shopping_game(
            pinnacle={"spreads": [Outcome("Home Team", -110, -3.5),
                                  Outcome("Away Team", -110, 3.5)]},
            draftkings={"spreads": [Outcome("Home Team", -115, -3.5),
                                    Outcome("Away Team", -105, 3.5)]},
            betmgm={"spreads": [Outcome("Home Team", -102, -3.5),
                                Outcome("Away Team", -118, 3.5)]},
        )
        best = best_offers(game, "spreads", cfg)
        assert best["Home Team"].book == "betmgm"
        assert best["Away Team"].book == "draftkings"

    def test_draftkings_wins_its_own_column_when_it_is_best(self, cfg):
        game = shopping_game(
            pinnacle={"h2h": [Outcome("Home Team", -140), Outcome("Away Team", 120)]},
            draftkings={"h2h": [Outcome("Home Team", -125), Outcome("Away Team", 135)]},
            fanduel={"h2h": [Outcome("Home Team", -145), Outcome("Away Team", 115)]},
        )
        best = best_offers(game, "h2h", cfg)
        assert best["Home Team"].book == "draftkings"
        assert best["Away Team"].book == "draftkings"

    def test_a_better_number_can_beat_cheaper_juice(self, cfg, halfpoint_table):
        """Best is the edge, so the two halves are weighed, not ranked apart."""
        game = shopping_game(
            pinnacle={"spreads": [Outcome("Home Team", -110, -3.0),
                                  Outcome("Away Team", -110, 3.0)]},
            draftkings={"spreads": [Outcome("Home Team", -105, -3.0),
                                    Outcome("Away Team", -115, 3.0)]},
            caesars={"spreads": [Outcome("Home Team", -118, -2.5),
                                 Outcome("Away Team", -102, 2.5)]},
        )
        best = best_offers(game, "spreads", cfg, halfpoint_table)
        # -2.5 buys past the 3, which is worth more than the 13 cents it costs.
        assert best["Home Team"].book == "caesars"
        assert best["Home Team"].point == -2.5

    def test_a_side_with_no_sharp_price_is_not_ranked(self, cfg):
        """Without a fair price there is nothing to call 'best'."""
        game = shopping_game(
            draftkings={"h2h": [Outcome("Home Team", -125), Outcome("Away Team", 135)]},
        )
        assert best_offers(game, "h2h", cfg) == {}

    def test_books_that_did_not_post_the_market_are_simply_absent(self, cfg):
        game = shopping_game(
            pinnacle={"h2h": [Outcome("Home Team", -140), Outcome("Away Team", 120)]},
            draftkings={"h2h": [Outcome("Home Team", -125), Outcome("Away Team", 135)]},
        )
        best = best_offers(game, "h2h", cfg)
        assert {o.book for o in best.values()} == {"draftkings"}

    def test_the_sharp_book_is_not_shoppable(self, cfg):
        """Pinnacle is the yardstick, not somewhere the group has an account."""
        game = shopping_game(
            pinnacle={"h2h": [Outcome("Home Team", -105), Outcome("Away Team", -105)]},
            draftkings={"h2h": [Outcome("Home Team", -130), Outcome("Away Team", 110)]},
        )
        best = best_offers(game, "h2h", cfg)
        assert all(offer.book != "pinnacle" for offer in best.values())

    def test_espn_bet_is_shopped(self, cfg):
        """It is in the response, so it belongs in the comparison."""
        game = shopping_game(
            pinnacle={"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
            draftkings={"totals": [Outcome("Over", -115, 51.5), Outcome("Under", -105, 51.5)]},
            espnbet={"totals": [Outcome("Over", 100, 51.5), Outcome("Under", -120, 51.5)]},
        )
        assert best_offers(game, "totals", cfg)["Over"].book == "espnbet"

    def test_a_tie_is_broken_the_same_way_every_scan(self, cfg):
        """Two books on an identical line must not swap places between scans."""
        game = shopping_game(
            pinnacle={"h2h": [Outcome("Home Team", -140), Outcome("Away Team", 120)]},
            draftkings={"h2h": [Outcome("Home Team", -130), Outcome("Away Team", 110)]},
            fanduel={"h2h": [Outcome("Home Team", -130), Outcome("Away Team", 110)]},
        )
        winners = {best_offers(game, "h2h", cfg)["Home Team"].book for _ in range(5)}
        assert len(winners) == 1

    def test_an_empty_shop_list_shops_nothing(self, cfg):
        game = shopping_game(
            pinnacle={"h2h": [Outcome("Home Team", -140), Outcome("Away Team", 120)]},
            draftkings={"h2h": [Outcome("Home Team", -125), Outcome("Away Team", 135)]},
        )
        assert best_offers(game, "h2h", cfg, books=[]) == {}


class TestBoard:
    def test_every_game_and_market_is_keyed(self, sample_games, cfg):
        best = shop_board(sample_games, ("h2h", "spreads", "totals"), cfg)
        assert best
        for (event_id, market, side), offer in best.items():
            assert market in ("h2h", "spreads", "totals")
            assert offer.book in cfg.shop_books
            assert offer.row.event_id == event_id
            assert offer.row.side == side

    def test_the_fixture_has_a_book_beating_draftkings(self, sample_games, cfg):
        """If DK always won, the column would be decoration."""
        best = shop_board(sample_games, ("h2h", "spreads", "totals"), cfg)
        assert any(offer.book != "draftkings" for offer in best.values())


class TestSerialization:
    def test_a_spread_keeps_its_sign_and_a_total_its_letter(self, cfg):
        game = shopping_game(
            pinnacle={"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
            draftkings={"totals": [Outcome("Over", -108, 51.5), Outcome("Under", -112, 51.5)]},
        )
        offer = best_offers(game, "totals", cfg)["Over"]
        payload = serialize_offer(offer, "totals", "O ")
        assert payload["number"] == "O 51.5"
        assert payload["book_label"] == "DK"

    def test_nothing_serializes_to_nothing(self):
        assert serialize_offer(None, "spreads") is None

    @pytest.mark.parametrize(
        "book,short,long",
        [("draftkings", "DK", "DraftKings"), ("espnbet", "ESPN", "ESPN Bet"),
         ("betmgm", "MGM", "BetMGM")],
    )
    def test_book_labels(self, book, short, long):
        assert book_label(book) == short
        assert book_name(book) == long

    def test_an_unknown_book_still_gets_a_readable_name(self):
        assert book_label("bet_rivers") == "Bet Rivers"


class TestConfig:
    def test_the_shop_books_are_requested_from_the_api(self):
        """A book we never ask for cannot win the column."""
        keys = Config().api_book_keys()
        assert "espnbet" in keys

    def test_shopping_does_not_change_the_consensus_fallback(self):
        """Adding ESPN Bet must not move anyone's edge."""
        cfg = Config()
        assert "espnbet" not in cfg.consensus_books
