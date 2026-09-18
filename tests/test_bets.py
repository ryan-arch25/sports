"""Tests for logging placed bets and scoring them against the close."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone

import pytest

from cfb_edge.bets import (
    BetError,
    add_bet,
    clv_rows,
    closing_line,
    cmd_bets_list,
    cmd_clv_report,
    match_side,
    resolve_event,
)
from cfb_edge.store import connect, init_db


def observation(conn, **kwargs):
    defaults = dict(
        run_id="r1", observed_at_utc="2026-09-18T14:05:00Z", event_id="g2michigan",
        commence_time_utc="2026-09-20T16:00:00Z", home_team="Michigan Wolverines",
        away_team="Ohio State Buckeyes", market="spreads", side="Michigan Wolverines",
        dk_point=6.5, dk_price=110, dk_prob=0.4762, sharp_source="circa",
        sharp_books="circasports", sharp_point=6.5, sharp_price=-110, sharp_hold=0.045,
        fair_prob=0.5, fair_american=100, edge_pct=2.38, ev_per_100=5.0, stake=25.0,
        status="priced", above_min_edge=1, translated_from=None, note="",
    )
    defaults.update(kwargs)
    conn.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at_utc) VALUES (?, ?)",
        (defaults["run_id"], defaults["observed_at_utc"]),
    )
    columns = ", ".join(defaults)
    marks = ", ".join("?" for _ in defaults)
    conn.execute(
        f"INSERT OR REPLACE INTO observations ({columns}) VALUES ({marks})",
        tuple(defaults.values()),
    )
    conn.commit()


@pytest.fixture
def db(tmp_path, cfg):
    cfg.db_path = tmp_path / "log.sqlite"
    conn = connect(cfg.db_path)
    init_db(conn)
    conn.execute(
        "INSERT INTO runs (run_id, started_at_utc) VALUES ('r1', '2026-09-18T14:05:00Z')"
    )
    observation(conn)
    observation(conn, market="totals", side="Over", dk_point=51.5, dk_price=-110,
                event_id="g1alabama", home_team="Alabama Crimson Tide",
                away_team="Georgia Bulldogs", commence_time_utc="2026-09-20T23:30:00Z")
    observation(conn, market="totals", side="Under", dk_point=51.5, dk_price=-110,
                event_id="g1alabama", home_team="Alabama Crimson Tide",
                away_team="Georgia Bulldogs", commence_time_utc="2026-09-20T23:30:00Z")
    yield conn, cfg
    conn.close()


def bet_args(**kwargs) -> argparse.Namespace:
    defaults = dict(
        event="g2michigan", market="spreads", side="Michigan Wolverines", point=6.5,
        price=110, stake=25.0, book=None, placed_at=None, note="",
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestResolveEvent:
    def test_by_event_id(self, db):
        conn, _ = db
        assert resolve_event(conn, "g2michigan")["home_team"] == "Michigan Wolverines"

    def test_by_team_name(self, db):
        conn, _ = db
        assert resolve_event(conn, "Buckeyes")["event_id"] == "g2michigan"

    def test_case_insensitive(self, db):
        conn, _ = db
        assert resolve_event(conn, "michigan")["event_id"] == "g2michigan"

    def test_an_unknown_game_explains_itself(self, db):
        conn, _ = db
        with pytest.raises(BetError, match="no game in the run log"):
            resolve_event(conn, "Rutgers")

    def test_an_ambiguous_match_lists_the_candidates(self, db):
        conn, _ = db
        observation(conn, event_id="g9", home_team="Michigan State Spartans",
                    away_team="Penn State", commence_time_utc="2026-09-21T16:00:00Z")
        with pytest.raises(BetError, match="more than one game"):
            resolve_event(conn, "Michigan")


class TestSideMatching:
    def test_exact(self, db):
        conn, _ = db
        assert match_side(conn, "g2michigan", "spreads", "Michigan Wolverines") == "Michigan Wolverines"

    def test_partial(self, db):
        conn, _ = db
        assert match_side(conn, "g2michigan", "spreads", "michigan") == "Michigan Wolverines"

    def test_over_under(self, db):
        conn, _ = db
        assert match_side(conn, "g1alabama", "totals", "over") == "Over"

    def test_unknown_side_lists_what_is_known(self, db):
        conn, _ = db
        with pytest.raises(BetError, match="Known:"):
            match_side(conn, "g1alabama", "totals", "Push")


class TestAddBet:
    def test_stores_the_bet_with_the_fair_price_of_the_day(self, db):
        conn, _ = db
        bet_id = add_bet(conn, bet_args())
        bet = conn.execute("SELECT * FROM bets WHERE bet_id = ?", (bet_id,)).fetchone()
        assert bet["side"] == "Michigan Wolverines"
        assert bet["fair_prob"] == pytest.approx(0.5)
        assert bet["edge_pct"] == pytest.approx(2.38)
        assert bet["book"] == "draftkings"
        assert bet["commence_time_utc"] == "2026-09-20T16:00:00Z"

    def test_a_bet_on_a_line_the_tool_never_saw_has_no_fair_price(self, db):
        conn, _ = db
        bet_id = add_bet(conn, bet_args(point=7.5))
        bet = conn.execute("SELECT * FROM bets WHERE bet_id = ?", (bet_id,)).fetchone()
        assert bet["fair_prob"] is None

    def test_another_book_is_recorded(self, db):
        conn, _ = db
        bet_id = add_bet(conn, bet_args(book="fanduel"))
        assert conn.execute(
            "SELECT book FROM bets WHERE bet_id = ?", (bet_id,)
        ).fetchone()["book"] == "fanduel"

    def test_an_explicit_placed_at_is_kept(self, db):
        conn, _ = db
        bet_id = add_bet(conn, bet_args(placed_at="2026-09-17T12:00:00Z"))
        assert conn.execute(
            "SELECT placed_at_utc FROM bets WHERE bet_id = ?", (bet_id,)
        ).fetchone()["placed_at_utc"] == "2026-09-17T12:00:00Z"

    @pytest.mark.parametrize("bad", [{"price": 0}, {"price": -50}])
    def test_an_impossible_price_is_rejected(self, db, bad):
        conn, _ = db
        with pytest.raises(BetError, match="bad price"):
            add_bet(conn, bet_args(**bad))

    def test_a_zero_stake_is_rejected(self, db):
        conn, _ = db
        with pytest.raises(BetError, match="stake"):
            add_bet(conn, bet_args(stake=0))


class TestClosingLine:
    def test_picks_the_last_observation_before_kickoff(self, db):
        conn, _ = db
        observation(conn, run_id="r2", observed_at_utc="2026-09-20T15:59:00Z", dk_price=-120)
        close = closing_line(conn, "g2michigan", "spreads", "Michigan Wolverines")
        assert close["dk_price"] == -120

    def test_ignores_observations_taken_after_kickoff(self, db):
        conn, _ = db
        conn.execute("INSERT INTO runs (run_id, started_at_utc) VALUES ('r3', 'x')")
        observation(conn, run_id="r3", observed_at_utc="2026-09-21T00:00:00Z", dk_price=-300)
        close = closing_line(conn, "g2michigan", "spreads", "Michigan Wolverines")
        assert close["dk_price"] == 110


class TestClv:
    def _settled(self, conn):
        """Move kickoff into the past so the bet is scoreable.

        Observations keep their order but are pulled back before kickoff, which
        is what makes the last one the closing line.
        """
        now = datetime.now(timezone.utc)
        kickoff = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        rows = conn.execute(
            "SELECT id, observed_at_utc FROM observations ORDER BY observed_at_utc"
        ).fetchall()
        for offset, row in enumerate(sorted({r["observed_at_utc"] for r in rows})):
            stamp = (now - timedelta(hours=6 - offset)).isoformat().replace("+00:00", "Z")
            conn.execute(
                "UPDATE observations SET observed_at_utc = ? WHERE observed_at_utc = ?",
                (stamp, row),
            )
        conn.execute("UPDATE observations SET commence_time_utc = ?", (kickoff,))
        conn.execute("UPDATE bets SET commence_time_utc = ?", (kickoff,))
        conn.commit()

    def test_a_shortening_price_is_positive_clv(self, db):
        conn, cfg = db
        add_bet(conn, bet_args())
        observation(conn, run_id="r1b", observed_at_utc="2026-09-19T00:00:00Z", dk_price=-120)
        self._settled(conn)
        rows = clv_rows(conn, cfg)
        assert len(rows) == 1
        assert rows[0].closing_price == -120
        assert rows[0].clv_pct == pytest.approx((120 / 220 - 100 / 210) * 100, abs=1e-6)
        assert rows[0].beat_close is True

    def test_a_drifting_price_is_negative_clv(self, db):
        conn, cfg = db
        add_bet(conn, bet_args())
        observation(conn, run_id="r1b", observed_at_utc="2026-09-19T00:00:00Z", dk_price=150)
        self._settled(conn)
        assert clv_rows(conn, cfg)[0].beat_close is False

    def test_open_games_are_excluded_by_default(self, db):
        conn, cfg = db
        add_bet(conn, bet_args())
        assert clv_rows(conn, cfg) == []
        assert len(clv_rows(conn, cfg, include_open=True)) == 1

    def test_a_number_change_is_flagged(self, db):
        conn, cfg = db
        add_bet(conn, bet_args(event="g1alabama", market="totals", side="Over",
                               point=51.5, price=-110))
        observation(conn, run_id="r1b", observed_at_utc="2026-09-19T00:00:00Z",
                    event_id="g1alabama", market="totals", side="Over", dk_point=52.5,
                    dk_price=-110, home_team="Alabama Crimson Tide",
                    away_team="Georgia Bulldogs", commence_time_utc="2026-09-20T23:30:00Z")
        self._settled(conn)
        row = clv_rows(conn, cfg)[0]
        assert row.closing_point == 52.5
        assert "closed on 52.5" in row.note
        assert row.clv_pct == pytest.approx(0.0)  # same price, different number

    def test_the_half_point_table_adjusts_for_a_number_change(self, db, halfpoint_table, tmp_path):
        from cfb_edge.halfpoint import save_table

        conn, cfg = db
        cfg.halfpoint_table_path = save_table(halfpoint_table, tmp_path / "hp.json")
        add_bet(conn, bet_args(event="g1alabama", market="totals", side="Over",
                               point=51.5, price=-110))
        observation(conn, run_id="r1b", observed_at_utc="2026-09-19T00:00:00Z",
                    event_id="g1alabama", market="totals", side="Over", dk_point=52.5,
                    dk_price=-110, home_team="Alabama Crimson Tide",
                    away_team="Georgia Bulldogs", commence_time_utc="2026-09-20T23:30:00Z")
        self._settled(conn)
        row = clv_rows(conn, cfg)[0]
        # The total moved up, so the Over bettor's number is the better one.
        assert row.clv_adjusted_pct is not None and row.clv_adjusted_pct > 0
        assert row.beat_close is True

    def test_a_bet_at_another_book_says_so(self, db):
        conn, cfg = db
        add_bet(conn, bet_args(book="fanduel"))
        self._settled(conn)
        assert "bet at fanduel" in clv_rows(conn, cfg)[0].note

    def test_a_bet_with_no_closing_observation_is_reported_not_scored(self, db):
        conn, cfg = db
        add_bet(conn, bet_args())
        conn.execute("DELETE FROM observations")
        self._settled(conn)
        row = clv_rows(conn, cfg)[0]
        assert row.clv_pct is None
        assert row.note == "no closing observation"


class TestCommands:
    def test_list_prints_the_bets(self, db, capsys):
        conn, cfg = db
        add_bet(conn, bet_args())
        args = argparse.Namespace(open=False, limit=50)
        assert cmd_bets_list(cfg, args) == 0
        out = capsys.readouterr().out
        assert "Michigan Wolverines +6.5" in out
        assert "$25.00" in out

    def test_list_with_nothing_logged(self, db, capsys):
        _, cfg = db
        assert cmd_bets_list(cfg, argparse.Namespace(open=False, limit=50)) == 0
        assert "no bets logged yet" in capsys.readouterr().out

    def test_clv_report_writes_a_csv(self, db, tmp_path, capsys):
        conn, cfg = db
        add_bet(conn, bet_args())
        out_csv = tmp_path / "clv.csv"
        args = argparse.Namespace(limit=100, csv=out_csv, all=True)
        assert cmd_clv_report(cfg, args) == 0
        rows = list(csv.DictReader(out_csv.open(encoding="utf-8")))
        assert rows[0]["pick"] == "Michigan Wolverines +6.5"
        assert "beat the close" in capsys.readouterr().out

    def test_clv_report_with_nothing_settled(self, db, capsys):
        _, cfg = db
        args = argparse.Namespace(limit=100, csv=None, all=False)
        assert cmd_clv_report(cfg, args) == 0
        assert "no settled bets" in capsys.readouterr().out
