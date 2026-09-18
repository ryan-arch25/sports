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
    derive_ticket_result,
    list_bets,
    log_payload,
    profit,
    settle,
    settle_leg,
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
        ({"event_id": ""}, "needs a game"),
        ({"market": ""}, "needs a game"),
        ({"market": "nonsense"}, "market must be one of"),
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


# -- parlays ---------------------------------------------------------------


def a_leg(**kwargs):
    leg = dict(
        event_id="g2michigan", market="spreads", side="Michigan Wolverines",
        point=6.5, price=110,
    )
    leg.update(kwargs)
    return leg


def a_parlay(**kwargs):
    payload = dict(
        person="RS", bet_type="parlay", price=905, stake=20,
        legs=[a_leg(), a_leg(market="h2h", side="Michigan Wolverines", point=None, price=230)],
    )
    payload.update(kwargs)
    return payload


class TestParlays:
    def test_it_stores_one_bet_with_its_legs(self, db):
        add_bet(db, a_parlay())
        bets = list_bets(db)
        assert len(bets) == 1
        bet = bets[0]
        assert bet["bet_type"] == "parlay"
        assert bet["matchup"] == "2-leg parlay"
        assert [leg["leg_no"] for leg in bet["legs"]] == [1, 2]
        assert bet["price"] == "+905"

    def test_the_everyone_table_counts_it_as_one_bet(self, db):
        add_bet(db, a_parlay())
        add_bet(db, a_bet())
        assert summarize(list_bets(db))["overall"]["bets"] == 2

    def test_each_leg_keeps_its_own_price_and_pick(self, db):
        add_bet(db, a_parlay())
        legs = list_bets(db)[0]["legs"]
        assert legs[0]["pick"] == "Michigan Wolverines +6.5"
        assert legs[0]["price"] == "+110"
        assert legs[1]["pick"] == "Michigan Wolverines ML"

    @pytest.mark.parametrize("count", [0, 1, 11])
    def test_a_parlay_needs_two_to_ten_legs(self, db, count):
        with pytest.raises(BetLogError, match="between 2 and 10"):
            add_bet(db, a_parlay(legs=[a_leg(side=f"s{i}") for i in range(count)]))

    def test_ten_legs_is_allowed(self, db):
        add_bet(db, a_parlay(legs=[a_leg(side=f"Team {i}") for i in range(10)]))
        assert len(list_bets(db)[0]["legs"]) == 10

    def test_the_same_selection_twice_is_refused(self, db):
        with pytest.raises(BetLogError, match="twice"):
            add_bet(db, a_parlay(legs=[a_leg(), a_leg()]))

    def test_a_leg_needs_a_price(self, db):
        """Without it a pushed leg could not be divided back out later."""
        with pytest.raises(BetLogError, match="leg price"):
            add_bet(db, a_parlay(legs=[a_leg(), a_leg(market="h2h", point=None, price=None)]))

    def test_legs_must_be_a_list(self, db):
        with pytest.raises(BetLogError, match="list of legs"):
            add_bet(db, a_parlay(legs="two"))

    def test_an_unknown_bet_type_is_refused(self, db):
        with pytest.raises(BetLogError, match="type must be one of"):
            add_bet(db, a_bet(bet_type="teaser"))


class TestParlaySettling:
    def parlay(self, db, **kwargs):
        return add_bet(db, a_parlay(**kwargs))

    def test_a_ticket_starts_open(self, db):
        self.parlay(db)
        assert list_bets(db)[0]["result"] == OPEN

    def test_one_graded_leg_leaves_it_open(self, db):
        bet_id = self.parlay(db)
        settle_leg(db, bet_id, 1, WON)
        bet = list_bets(db)[0]
        assert bet["result"] == OPEN
        assert bet["legs"][0]["result"] == WON

    def test_every_leg_won_wins_the_ticket(self, db):
        bet_id = self.parlay(db)
        settle_leg(db, bet_id, 1, WON)
        settle_leg(db, bet_id, 2, WON)
        bet = list_bets(db)[0]
        assert bet["result"] == WON
        assert bet["profit"] == pytest.approx(20 * (10.05 - 1), rel=1e-6)

    def test_one_lost_leg_loses_the_ticket_immediately(self, db):
        bet_id = self.parlay(db)
        settle_leg(db, bet_id, 1, LOST)
        bet = list_bets(db)[0]
        assert bet["result"] == LOST
        assert bet["profit"] == -20

    def test_a_lost_leg_beats_an_open_one(self, db):
        """You do not wait on the rest once one has gone down."""
        bet_id = self.parlay(db)
        settle_leg(db, bet_id, 2, LOST)
        assert list_bets(db)[0]["result"] == LOST

    def test_a_pushed_leg_drops_out_and_the_payout_shrinks(self, db):
        bet_id = self.parlay(db)
        settle_leg(db, bet_id, 1, PUSH)   # +110 leg, decimal 2.10
        settle_leg(db, bet_id, 2, WON)
        bet = list_bets(db)[0]
        assert bet["result"] == WON
        # 10.05 combined / 2.10 for the pushed leg leaves 4.7857
        assert bet["profit"] == pytest.approx(20 * (10.05 / 2.10 - 1), rel=1e-6)

    def test_every_leg_pushed_is_a_push(self, db):
        bet_id = self.parlay(db)
        settle_leg(db, bet_id, 1, PUSH)
        settle_leg(db, bet_id, 2, PUSH)
        bet = list_bets(db)[0]
        assert bet["result"] == PUSH
        assert bet["profit"] == 0.0

    def test_a_leg_can_be_reopened(self, db):
        bet_id = self.parlay(db)
        settle_leg(db, bet_id, 1, WON)
        settle_leg(db, bet_id, 2, WON)
        settle_leg(db, bet_id, 2, OPEN)
        assert list_bets(db)[0]["result"] == OPEN

    def test_reopening_the_ticket_reopens_every_leg(self, db):
        bet_id = self.parlay(db)
        settle_leg(db, bet_id, 1, WON)
        settle_leg(db, bet_id, 2, LOST)
        settle(db, bet_id, OPEN)
        bet = list_bets(db)[0]
        assert bet["result"] == OPEN
        assert {leg["result"] for leg in bet["legs"]} == {OPEN}

    def test_a_ticket_cannot_be_graded_directly(self, db):
        bet_id = self.parlay(db)
        with pytest.raises(BetLogError, match="one leg at a time"):
            settle(db, bet_id, WON)

    def test_a_single_has_no_legs_to_settle(self, db):
        bet_id = add_bet(db, a_bet())
        with pytest.raises(BetLogError, match="not a parlay"):
            settle_leg(db, bet_id, 1, WON)

    def test_an_unknown_leg_is_refused(self, db):
        bet_id = self.parlay(db)
        with pytest.raises(BetLogError, match="no leg 7"):
            settle_leg(db, bet_id, 7, WON)

    def test_the_ticket_carries_no_clv_of_its_own(self, db):
        bet_id = self.parlay(db)
        settle_leg(db, bet_id, 1, WON)
        assert list_bets(db)[0]["clv_pct"] is None

    def test_but_each_leg_does(self, db):
        self.parlay(db)
        assert list_bets(db)[0]["legs"][0]["clv_pct"] is not None


class TestDeriveResult:
    @pytest.mark.parametrize("legs,expected", [
        ([WON, WON], WON),
        ([WON, LOST], LOST),
        ([LOST, OPEN], LOST),
        ([WON, OPEN], OPEN),
        ([PUSH, WON], WON),
        ([PUSH, PUSH], PUSH),
        ([PUSH, OPEN], OPEN),
        ([], OPEN),
    ])
    def test_it(self, legs, expected):
        assert derive_ticket_result(legs) == expected


# -- custom and alt lines --------------------------------------------------


@pytest.fixture
def tables(cfg):
    from cfb_edge.halfpoint import estimated_table

    return [estimated_table(cfg)]


class TestCustomLines:
    def test_an_alt_spread_the_scan_never_priced_still_records(self, db):
        add_bet(db, a_bet(point=9.5, price=180))
        bet = list_bets(db)[0]
        assert bet["pick"] == "Michigan Wolverines +9.5"
        assert bet["price"] == "+180"

    def test_a_team_total_records(self, db):
        add_bet(db, a_bet(
            market="team_totals", side="Michigan Wolverines Over", point=24.5, price=-115,
        ))
        assert list_bets(db)[0]["pick"] == "Michigan Wolverines Over 24.5"

    def test_a_moneyline_needs_no_line_number(self, db):
        add_bet(db, a_bet(market="h2h", side="Michigan Wolverines", point=None, price=230))
        assert list_bets(db)[0]["pick"] == "Michigan Wolverines ML"

    def test_a_line_on_a_moneyline_is_dropped(self, db):
        """A number here would make the bet unfindable in the scan history."""
        add_bet(db, a_bet(market="h2h", side="Michigan Wolverines", point=3.5, price=230))
        assert list_bets(db)[0]["pick"] == "Michigan Wolverines ML"

    def test_a_spread_without_a_line_is_refused(self, db):
        with pytest.raises(BetLogError, match="needs a line number"):
            add_bet(db, a_bet(point=None))

    def test_a_market_outside_the_list_is_refused(self, db):
        with pytest.raises(BetLogError, match="market must be one of"):
            add_bet(db, a_bet(market="player_pass_yds"))


class TestTranslatedCLV:
    def test_the_market_number_is_not_estimated(self, db, tables):
        add_bet(db, a_bet(point=6.5, price=110))
        bet = list_bets(db, tables=tables)[0]
        assert bet["clv_pct"] is not None
        assert bet["clv_estimated"] is False

    def test_an_alt_line_is_translated_and_tagged(self, db, tables):
        """DK's +9.5 is a different bet from the +6.5 the scan closed on."""
        add_bet(db, a_bet(point=9.5, price=180))
        bet = list_bets(db, tables=tables)[0]
        assert bet["clv_pct"] is not None
        assert bet["clv_estimated"] is True

    @pytest.mark.parametrize("point,better", [(9.5, True), (4.5, False)])
    def test_the_translation_follows_the_side_of_the_number(self, db, tables, point, better):
        """At one price, more points on your side is the better bet, and fewer
        is the worse one. The translated close has to move that way or the sign
        of the CLV would be telling people the opposite of the truth."""
        add_bet(db, a_bet(point=6.5, price=110))
        on_market = list_bets(db, tables=tables)[0]["clv_pct"]
        add_bet(db, a_bet(point=point, price=110))
        moved = list_bets(db, tables=tables)[0]["clv_pct"]
        assert (moved > on_market) is better

    def test_without_a_table_an_alt_line_shows_a_dash(self, db):
        add_bet(db, a_bet(point=9.5, price=180))
        bet = list_bets(db)[0]
        assert bet["clv_pct"] is None
        assert bet["closing_price"] is None

    def test_a_move_past_three_points_is_not_guessed(self, db, tables):
        add_bet(db, a_bet(point=11.5, price=260))
        assert list_bets(db, tables=tables)[0]["clv_pct"] is None

    def test_a_team_total_is_never_translated(self, db, tables):
        """The game-total distribution does not describe one team's points."""
        observation(
            db, market="team_totals", side="Michigan Wolverines Over",
            dk_point=24.5, sharp_point=24.5,
        )
        add_bet(db, a_bet(
            market="team_totals", side="Michigan Wolverines Over", point=26.5, price=140,
        ))
        assert list_bets(db, tables=tables)[0]["clv_pct"] is None

    def test_a_team_total_on_the_number_still_scores(self, db, tables):
        observation(
            db, market="team_totals", side="Michigan Wolverines Over",
            dk_point=24.5, sharp_point=24.5,
        )
        add_bet(db, a_bet(
            market="team_totals", side="Michigan Wolverines Over", point=24.5, price=140,
        ))
        assert list_bets(db, tables=tables)[0]["clv_pct"] is not None

    def test_a_game_with_no_closing_line_shows_a_dash(self, db, tables):
        add_bet(db, a_bet(event_id="never-scanned", home_team="A", away_team="B"))
        bet = list_bets(db, tables=tables)[0]
        assert bet["clv_pct"] is None
        assert bet["closing_price"] is None

    def test_a_parlay_leg_on_an_alt_line_is_tagged_too(self, db, tables):
        add_bet(db, a_parlay(legs=[
            a_leg(point=9.5, price=180),
            a_leg(market="h2h", side="Michigan Wolverines", point=None, price=230),
        ]))
        legs = list_bets(db, tables=tables)[0]["legs"]
        assert legs[0]["clv_estimated"] is True

    def test_the_closing_price_is_restated_on_the_logged_number(self, db, tables):
        """Showing the +6.5 close beside a +9.5 bet would be two different bets."""
        add_bet(db, a_bet(point=9.5, price=180))
        on_market = add_bet(db, a_bet(point=6.5, price=110))
        bets = {b["bet_id"]: b for b in list_bets(db, tables=tables)}
        assert bets[on_market]["closing_price"] != bets[on_market - 1]["closing_price"]
