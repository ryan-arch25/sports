"""Tests for line movement: recording changes, and reading them back."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cfb_edge.edges import evaluate_games
from cfb_edge.models import Outcome
from cfb_edge.movement import (
    DOWN,
    UP,
    history_payload,
    move_for,
    moves,
    serialize_moves,
    sharp_book_name,
    snapshots,
    stamp_et,
)
from cfb_edge.store import (
    LineSnapshot,
    baseline_lines,
    connect,
    latest_lines,
    line_history,
    prune_history,
    record_lines,
)

from conftest import make_game

NOW = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "history.sqlite")
    yield conn
    conn.close()


def board(spread_dk=-6.5, price_dk=-110, spread_sharp=-6.5, price_sharp=-110):
    return make_game({
        "draftkings": {"spreads": [Outcome("Home Team", price_dk, spread_dk),
                                   Outcome("Away Team", -110, -spread_dk)]},
        "pinnacle": {"spreads": [Outcome("Home Team", price_sharp, spread_sharp),
                                 Outcome("Away Team", -110, -spread_sharp)]},
    })


def rows_for(game, cfg, markets=("spreads",)):
    return evaluate_games([game], cfg, markets)


def snap(point, price, book="draftkings", side="Home Team"):
    return LineSnapshot("evt", "spreads", side, book, point, price)


class TestRecordingOnlyChanges:
    def test_the_first_look_is_always_recorded(self, db):
        assert record_lines(db, [snap(-6.5, -110)], NOW) == 1

    def test_an_unchanged_line_writes_nothing(self, db):
        record_lines(db, [snap(-6.5, -110)], NOW)
        written = record_lines(db, [snap(-6.5, -110)], NOW + timedelta(hours=1))
        assert written == 0
        assert len(line_history(db, "evt")) == 1

    def test_a_moved_number_writes_a_row(self, db):
        record_lines(db, [snap(-6.5, -110)], NOW)
        assert record_lines(db, [snap(-7.0, -110)], NOW + timedelta(hours=1)) == 1

    def test_a_moved_price_on_the_same_number_writes_a_row(self, db):
        record_lines(db, [snap(-6.5, -110)], NOW)
        assert record_lines(db, [snap(-6.5, -105)], NOW + timedelta(hours=1)) == 1

    def test_the_same_number_written_differently_is_not_a_move(self, db):
        """6.50 and 6.5 are the same line, and a float compare would disagree."""
        record_lines(db, [snap(-6.5, -110)], NOW)
        assert record_lines(db, [snap(-6.50000000001, -110.0)], NOW + timedelta(hours=1)) == 0

    def test_two_snapshots_of_one_line_in_a_scan_write_once(self, db):
        assert record_lines(db, [snap(-6.5, -110), snap(-7.0, -110)], NOW) == 1

    def test_each_book_keeps_its_own_history(self, db):
        record_lines(db, [snap(-6.5, -110, "draftkings"), snap(-6.5, -110, "pinnacle")], NOW)
        record_lines(
            db, [snap(-7.0, -110, "draftkings"), snap(-6.5, -110, "pinnacle")],
            NOW + timedelta(hours=1),
        )
        books = [c["book"] for c in line_history(db, "evt")]
        assert books.count("draftkings") == 2
        assert books.count("pinnacle") == 1

    def test_latest_lines_reads_back_the_most_recent_value(self, db):
        record_lines(db, [snap(-6.5, -110)], NOW)
        record_lines(db, [snap(-7.0, -115)], NOW + timedelta(hours=1))
        assert latest_lines(db)[("evt", "spreads", "Home Team", "draftkings")] == (-7.0, -115.0)


class TestSnapshotsFromScan:
    def test_both_books_are_snapshotted(self, cfg):
        rows = rows_for(board(), cfg)
        taken = snapshots(rows, cfg)
        assert {s.book for s in taken} == {"draftkings", "pinnacle"}

    def test_markets_outside_the_history_list_are_skipped(self, cfg):
        game = make_game({
            "draftkings": {"h2h": [Outcome("Home Team", -140), Outcome("Away Team", 120)]},
            "pinnacle": {"h2h": [Outcome("Home Team", -135), Outcome("Away Team", 115)]},
        })
        assert snapshots(rows_for(game, cfg, ("h2h",)), cfg) == []

    def test_a_side_with_no_sharp_price_records_draftkings_alone(self, cfg):
        game = make_game({
            "draftkings": {"spreads": [Outcome("Home Team", -110, -6.5),
                                       Outcome("Away Team", -110, 6.5)]},
        })
        taken = snapshots(rows_for(game, cfg), cfg)
        assert {s.book for s in taken} == {"draftkings"}

    def test_the_consensus_count_is_stripped_from_the_book_name(self, cfg):
        """consensus(3) and consensus(2) are the same line, not two of them."""
        game = make_game({
            "draftkings": {"spreads": [Outcome("Home Team", -110, -6.5),
                                       Outcome("Away Team", -110, 6.5)]},
            "fanduel": {"spreads": [Outcome("Home Team", -110, -6.5),
                                    Outcome("Away Team", -110, 6.5)]},
            "betmgm": {"spreads": [Outcome("Home Team", -110, -6.5),
                                   Outcome("Away Team", -110, 6.5)]},
        })
        rows = rows_for(game, cfg)
        assert sharp_book_name(rows[0]) == "consensus"
        assert {s.book for s in snapshots(rows, cfg)} == {"draftkings", "consensus"}


class TestArrows:
    def test_no_baseline_means_no_arrow(self):
        assert move_for(None, -6.5, -110) is None

    def test_an_unchanged_line_has_not_moved(self):
        assert move_for((-6.5, -110, "t"), -6.5, -110) is None

    @pytest.mark.parametrize(
        "previous,current,direction",
        [(-6.5, -7.0, DOWN), (-7.0, -6.5, UP), (44.5, 45.5, UP), (45.5, 44.5, DOWN)],
    )
    def test_the_arrow_follows_the_number(self, previous, current, direction):
        move = move_for((previous, -110, "t"), current, -110)
        assert move.direction == direction
        assert move.previous_point == previous

    def test_a_price_only_move_points_at_the_better_price(self):
        """The number did not change, so the arrow is about the juice."""
        assert move_for((-6.5, -120, "t"), -6.5, -105).direction == UP
        assert move_for((-6.5, -105, "t"), -6.5, -120).direction == DOWN

    def test_the_previous_value_is_carried_for_the_tooltip(self):
        move = move_for((-6.5, -110, "2026-09-20T10:00:00Z"), -7.0, -115)
        payload = move.as_dict("spreads")
        assert payload["previous_number"] == "-6.5"
        assert payload["previous_price"] == "-110"
        assert payload["previous_at_et"]

    def test_moves_reads_the_value_from_the_arrow_cutoff(self, db, cfg):
        record_lines(db, [snap(-6.5, -110)], NOW - timedelta(hours=10))
        record_lines(db, [snap(-7.0, -110)], NOW - timedelta(hours=2))
        rows = rows_for(board(spread_dk=-7.0), cfg)
        found = moves(db, rows, cfg, NOW)
        move = found[("evt", "spreads", "Home Team", "draftkings")]
        assert move.previous_point == -6.5  # what it was six hours ago

    def test_a_move_older_than_the_window_gets_no_arrow(self, db, cfg):
        record_lines(db, [snap(-6.5, -110)], NOW - timedelta(hours=30))
        record_lines(db, [snap(-7.0, -110)], NOW - timedelta(hours=20))
        rows = rows_for(board(spread_dk=-7.0), cfg)
        assert moves(db, rows, cfg, NOW) == {}

    def test_a_line_first_seen_inside_the_window_compares_to_its_oldest_row(self, db, cfg):
        """Its baseline is the first number the board showed, not nothing."""
        record_lines(db, [snap(-6.5, -110)], NOW - timedelta(hours=3))
        rows = rows_for(board(spread_dk=-7.0), cfg)
        found = moves(db, rows, cfg, NOW)
        assert found[("evt", "spreads", "Home Team", "draftkings")].previous_point == -6.5

    def test_an_empty_history_produces_no_arrows(self, db, cfg):
        assert moves(db, rows_for(board(), cfg), cfg, NOW) == {}

    def test_the_live_value_decides_not_the_stored_one(self, db, cfg):
        """A scan whose history write failed shows no arrow, never a wrong one."""
        record_lines(db, [snap(-6.5, -110)], NOW - timedelta(hours=10))
        rows = rows_for(board(spread_dk=-6.5), cfg)  # unchanged live
        assert moves(db, rows, cfg, NOW) == {}

    def test_serialize_splits_the_two_books(self, db, cfg):
        record_lines(
            db,
            [snap(-6.5, -110, "draftkings"), snap(-6.5, -110, "pinnacle", "Home Team")],
            NOW - timedelta(hours=10),
        )
        rows = rows_for(board(spread_dk=-7.0, spread_sharp=-7.0), cfg)
        payload = serialize_moves(moves(db, rows, cfg, NOW), cfg)
        cell = payload[("evt", "spreads", "Home Team")]
        assert set(cell) == {"dk", "sharp"}


class TestPruning:
    def test_old_rows_go(self, db):
        record_lines(db, [snap(-6.5, -110)], NOW - timedelta(days=40))
        record_lines(db, [snap(-7.0, -110)], NOW - timedelta(days=39))
        record_lines(db, [snap(-7.5, -110)], NOW)
        assert prune_history(db, NOW - timedelta(days=30)) == 2
        assert len(line_history(db, "evt")) == 1

    def test_a_lines_last_row_is_never_pruned(self, db, cfg):
        """Otherwise the next scan re-records it and reports a move that never happened."""
        record_lines(db, [snap(-6.5, -110)], NOW - timedelta(days=40))
        assert prune_history(db, NOW - timedelta(days=30)) == 0
        assert record_lines(db, [snap(-6.5, -110)], NOW) == 0


class TestPanel:
    def test_it_lists_every_change_newest_first(self, db, cfg):
        record_lines(db, [snap(-6.5, -110)], NOW - timedelta(hours=30))
        record_lines(db, [snap(-7.0, -110)], NOW - timedelta(hours=10))
        record_lines(db, [snap(-7.5, -110)], NOW - timedelta(hours=1))
        payload = history_payload(db, cfg, "evt", NOW)
        assert payload["count"] == 3
        assert [c["number"] for c in payload["changes"]] == ["-7.5", "-7", "-6.5"]

    def test_it_stops_at_the_window(self, db, cfg):
        record_lines(db, [snap(-6.5, -110)], NOW - timedelta(hours=60))
        record_lines(db, [snap(-7.0, -110)], NOW - timedelta(hours=1))
        assert history_payload(db, cfg, "evt", NOW)["count"] == 1

    def test_a_game_with_no_history_is_empty_not_an_error(self, db, cfg):
        payload = history_payload(db, cfg, "nothing", NOW)
        assert payload["changes"] == [] and payload["count"] == 0

    def test_books_are_written_out(self, db, cfg):
        record_lines(db, [snap(-6.5, -110, "draftkings")], NOW)
        assert history_payload(db, cfg, "evt", NOW)["changes"][0]["book_label"] == "DraftKings"

    def test_timestamps_are_eastern(self):
        assert stamp_et("2026-09-20T18:15:00Z") == "Sun 2:15 PM"

    def test_a_bad_timestamp_does_not_raise(self):
        assert stamp_et("not a time") == ""
        assert stamp_et(None) == ""


class TestBaseline:
    def test_an_empty_table_has_no_baseline(self, db):
        assert baseline_lines(db, NOW) == {}


class TestFirstSeen:
    def test_the_opening_row_is_marked(self, db, cfg):
        """Otherwise the first scan after a deploy reads as a slate-wide move."""
        record_lines(db, [snap(-6.5, -110)], NOW - timedelta(hours=5))
        record_lines(db, [snap(-7.0, -110)], NOW - timedelta(hours=1))
        changes = history_payload(db, cfg, "evt", NOW)["changes"]
        assert [c["opened"] for c in changes] == [False, True]  # newest first

    def test_each_line_opens_once(self, db, cfg):
        record_lines(
            db, [snap(-6.5, -110, "draftkings"), snap(-6.5, -110, "pinnacle")], NOW,
        )
        changes = history_payload(db, cfg, "evt", NOW)["changes"]
        assert all(c["opened"] for c in changes)

    def test_an_opening_row_outside_the_window_does_not_mark_a_later_one(self, db, cfg):
        record_lines(db, [snap(-6.5, -110)], NOW - timedelta(hours=60))
        record_lines(db, [snap(-7.0, -110)], NOW - timedelta(hours=1))
        changes = history_payload(db, cfg, "evt", NOW)["changes"]
        assert len(changes) == 1 and changes[0]["opened"] is False
