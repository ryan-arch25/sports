"""Tests for the shared bet log behind the dashboard's Log tab."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cfb_edge.store import connect, init_db
from cfb_edge.web.betlog import (
    LOST,
    OPEN,
    PUSH,
    WON,
    BetLogError,
    add_bet,
    clv_points,
    list_bets,
    log_payload,
    profit,
    settle,
    summarize,
)


def observation(conn, **kwargs):
    defaults = dict(
        run_id="r1", observed_at_utc="2026-09-18T14:05:00Z", event_id="g2michigan",
        commence_time_utc="2026-09-20T16:00:00Z", home_team="Michigan Wolverines",
        away_team="Ohio State Buckeyes", market="spreads", side="Michigan Wolverines",
        dk_point=6.5, dk_price=110, dk_prob=0.4762, sharp_source="circa",
        sharp_books="circasports", sharp_point=6.5, sharp_price=-110, sharp_hold=0.045,
        fair_prob=0.5, fair_american=100, edge_pct=2.38, ev_per_100=5.0, stake=25.0,
        status="priced", above_min_edge=1, note="",
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
def db(tmp_path):
    conn = connect(tmp_path / "log.sqlite")
    init_db(conn)
    observation(conn)
    yield conn
    conn.close()


def a_bet(**kwargs):
    payload = dict(
        person="RS", event_id="g2michigan", market="spreads",
        side="Michigan Wolverines", point=6.5, price=110, stake=25,
    )
    payload.update(kwargs)
    return payload


class TestAdding:
    def test_records_the_bet_with_the_game_details(self, db):
        bet_id = add_bet(db, a_bet())
        bet = list_bets(db)[0]
        assert bet["bet_id"] == bet_id
        assert bet["person"] == "RS"
        assert bet["matchup"] == "Ohio State Buckeyes @ Michigan Wolverines"
        assert bet["pick"] == "Michigan Wolverines +6.5"
        assert bet["price"] == "+110"
        assert bet["result"] == OPEN

    def test_a_new_bet_is_open(self, db):
        add_bet(db, a_bet())
        assert list_bets(db)[0]["profit"] is None

    def test_the_name_is_trimmed_and_capped(self, db):
        add_bet(db, a_bet(person="  " + "x" * 80 + "  "))
        assert len(list_bets(db)[0]["person"]) == 40

    def test_an_explicit_date_is_kept(self, db):
        add_bet(db, a_bet(placed_on="2026-09-17"))
        assert list_bets(db)[0]["placed_on"] == "2026-09-17"

    def test_a_missing_date_defaults_to_now(self, db):
        add_bet(db, a_bet())
        assert list_bets(db)[0]["placed_on"] == datetime.now(timezone.utc).date().isoformat()

    @pytest.mark.parametrize("bad,message", [
        ({"person": ""}, "who placed it"),
        ({"person": "   "}, "who placed it"),
        ({"event_id": ""}, "pick a game"),
        ({"market": ""}, "pick a game"),
        ({"price": 0}, "American odds"),
        ({"price": "abc"}, "must be a number"),
        ({"stake": 0}, "greater than zero"),
        ({"stake": -5}, "greater than zero"),
        ({"placed_on": "last tuesday"}, "2026-09-20"),
    ])
    def test_rubbish_is_refused(self, db, bad, message):
        with pytest.raises(BetLogError, match=message):
            add_bet(db, a_bet(**bad))

    def test_a_game_the_scan_never_saw_still_records(self, db):
        add_bet(db, a_bet(event_id="unknown", home_team="A", away_team="B"))
        assert list_bets(db)[0]["matchup"] == "B @ A"


class TestSettling:
    def test_a_win_pays_the_price(self, db):
        bet_id = add_bet(db, a_bet(price=110, stake=25))
        settle(db, bet_id, WON)
        bet = list_bets(db)[0]
        assert bet["result"] == WON
        assert bet["profit"] == pytest.approx(27.5)

    def test_a_loss_costs_the_stake(self, db):
        bet_id = add_bet(db, a_bet(stake=25))
        settle(db, bet_id, LOST)
        assert list_bets(db)[0]["profit"] == pytest.approx(-25.0)

    def test_a_push_is_flat(self, db):
        bet_id = add_bet(db, a_bet())
        settle(db, bet_id, PUSH)
        assert list_bets(db)[0]["profit"] == 0.0

    def test_a_result_can_be_taken_back(self, db):
        bet_id = add_bet(db, a_bet())
        settle(db, bet_id, WON)
        settle(db, bet_id, OPEN)
        bet = list_bets(db)[0]
        assert bet["result"] == OPEN and bet["profit"] is None

    def test_an_unknown_result_is_refused(self, db):
        bet_id = add_bet(db, a_bet())
        with pytest.raises(BetLogError, match="must be one of"):
            settle(db, bet_id, "maybe")

    def test_settling_a_bet_that_is_not_there(self, db):
        with pytest.raises(BetLogError, match="no bet #99"):
            settle(db, 99, WON)

    @pytest.mark.parametrize("price,stake,result,expected", [
        (110, 100, WON, 110.0), (-110, 110, WON, 100.0), (150, 50, LOST, -50.0),
        (-200, 40, PUSH, 0.0), (120, 10, OPEN, None),
    ])
    def test_profit_arithmetic(self, price, stake, result, expected):
        got = profit(price, stake, result)
        assert got is None if expected is None else got == pytest.approx(expected)


class TestClosingLineValue:
    def test_a_price_that_beat_the_close(self, db):
        add_bet(db, a_bet(price=150))
        # The sharp book closed at -110, well short of +150.
        assert list_bets(db)[0]["clv_pct"] > 0

    def test_a_price_that_did_not(self, db):
        add_bet(db, a_bet(price=-200))
        assert list_bets(db)[0]["clv_pct"] < 0

    def test_it_uses_the_last_price_before_kickoff(self, db):
        observation(db, run_id="r2", observed_at_utc="2026-09-20T15:59:00Z", sharp_price=-150)
        observation(db, run_id="r3", observed_at_utc="2026-09-21T00:00:00Z", sharp_price=1000)
        add_bet(db, a_bet(price=110))
        bet = list_bets(db)[0]
        assert bet["closing_price"] == "-150"  # not the post-kickoff observation
        assert bet["closing_source"] == "circa"

    def test_a_bet_with_no_scan_history_is_unscored(self, db):
        add_bet(db, a_bet(event_id="never-scanned", market="h2h", side="Someone"))
        assert list_bets(db)[0]["clv_pct"] is None

    def test_the_arithmetic(self):
        # +150 is 40%; a close of -110 is 52.38%, so the market moved 12.38 your way.
        assert clv_points(150, -110) == pytest.approx(12.38, abs=0.01)
        assert clv_points(-110, 150) == pytest.approx(-12.38, abs=0.01)
        assert clv_points(110, 110) == pytest.approx(0.0)


class TestSummary:
    def _log(self, conn, *bets):
        for payload, result in bets:
            bet_id = add_bet(conn, payload)
            if result:
                settle(conn, bet_id, result)
        return log_payload(conn)

    def test_the_group_record(self, db):
        payload = self._log(
            db,
            (a_bet(person="RS", price=110, stake=100), WON),
            (a_bet(person="RS", price=-110, stake=110), LOST),
            (a_bet(person="JT", price=100, stake=50), PUSH),
            (a_bet(person="JT", price=120, stake=50), None),
        )
        overall = payload["overall"]
        assert (overall["won"], overall["lost"], overall["push"]) == (1, 1, 1)
        assert overall["open"] == 1
        assert overall["bets"] == 4
        assert overall["profit"] == pytest.approx(0.0)  # +110 then -110
        assert overall["staked"] == pytest.approx(210.0)  # a push stakes nothing

    def test_roi(self, db):
        payload = self._log(db, (a_bet(price=100, stake=100), WON))
        assert payload["overall"]["roi_pct"] == pytest.approx(100.0)

    def test_roi_is_undefined_with_nothing_settled(self, db):
        payload = self._log(db, (a_bet(), None))
        assert payload["overall"]["roi_pct"] is None

    def test_per_person_breakdown(self, db):
        payload = self._log(
            db,
            (a_bet(person="RS", price=110, stake=100), WON),
            (a_bet(person="JT", price=-110, stake=110), LOST),
        )
        people = {p["person"]: p for p in payload["people"]}
        assert people["RS"]["profit"] == pytest.approx(110.0)
        assert people["JT"]["profit"] == pytest.approx(-110.0)
        # Most profitable first.
        assert [p["person"] for p in payload["people"]] == ["RS", "JT"]

    def test_average_clv_covers_open_bets_too(self, db):
        payload = self._log(db, (a_bet(price=150), None))
        assert payload["overall"]["avg_clv_pct"] is not None
        assert payload["overall"]["scored"] == 1

    def test_an_empty_log(self, db):
        payload = log_payload(db)
        assert payload["bets"] == []
        assert payload["people"] == []
        assert payload["overall"]["bets"] == 0
        assert payload["overall"]["roi_pct"] is None

    def test_summarize_is_pure(self):
        bets = [
            {"person": "A", "result": WON, "stake": 10.0, "profit": 9.0, "clv_pct": 1.0},
            {"person": "A", "result": LOST, "stake": 10.0, "profit": -10.0, "clv_pct": -2.0},
        ]
        overall = summarize(bets)["overall"]
        assert overall["profit"] == pytest.approx(-1.0)
        assert overall["avg_clv_pct"] == pytest.approx(-0.5)
