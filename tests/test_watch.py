"""Tests for --watch: the rescan loop and its change detection."""

from __future__ import annotations

import json
from pathlib import Path

from cfb_edge.cli import apply_overrides, build_parser, normalize_argv, scan_options
from cfb_edge.edges import EdgeRow
from cfb_edge.models import parse_commence_time, parse_games
from cfb_edge.scan import scan_key
from cfb_edge.watch import DROPPED, EDGE, NEW, NUMBER, PRICE, diff_bets, watch

FIXTURE = Path(__file__).parent / "fixtures" / "sample_odds.json"


def row(**kwargs) -> EdgeRow:
    defaults = dict(
        event_id="evt", commence_time=parse_commence_time("2026-09-20T23:30:00Z"),
        home_team="Home", away_team="Away", market="totals", side="Over",
        dk_point=51.5, dk_price=-110, dk_prob=0.5238, sharp_source="pinnacle",
        sharp_books="pinnacle", sharp_point=51.5, sharp_price=-105, sharp_hold=0.02,
        fair_prob=0.55, fair_american=-122, edge=0.0262, ev_per_100=5.0, stake=10.0,
        status="priced",
    )
    defaults.update(kwargs)
    return EdgeRow(**defaults)


class TestDiff:
    def test_everything_is_new_on_the_first_pass(self):
        changes = diff_bets({}, [row()], edge_delta_pct=0.25)
        assert [c.kind for c in changes] == [NEW]
        assert changes[0].description == "new"

    def test_an_unchanged_line_is_silent(self):
        before = row()
        assert diff_bets({scan_key(before): before}, [row()], 0.25) == []

    def test_a_price_move_is_reported(self):
        before = row()
        changes = diff_bets({scan_key(before): before}, [row(dk_price=-105)], 0.25)
        assert changes[0].kind == PRICE
        assert changes[0].description == "price -110→-105"

    def test_a_number_move_is_reported_and_outranks_a_price_move(self):
        before = row()
        changes = diff_bets({scan_key(before): before}, [row(dk_point=52.5, dk_price=-105)], 0.25)
        assert changes[0].kind == NUMBER
        assert "51.5→52.5" in changes[0].description

    def test_an_edge_move_past_the_threshold_is_reported(self):
        before = row(edge=0.02)
        changes = diff_bets({scan_key(before): before}, [row(edge=0.04)], edge_delta_pct=0.25)
        assert changes[0].kind == EDGE
        assert changes[0].description == "edge 2.0%→4.0%"

    def test_an_edge_move_under_the_threshold_is_ignored(self):
        before = row(edge=0.0200)
        assert diff_bets({scan_key(before): before}, [row(edge=0.0210)], 0.25) == []

    def test_a_departed_line_is_only_reported_on_request(self):
        before = row()
        state = {scan_key(before): before}
        assert diff_bets(state, [], 0.25) == []
        changes = diff_bets(state, [], 0.25, include_dropped=True)
        assert [c.kind for c in changes] == [DROPPED]

    def test_changes_are_ranked_by_edge_with_departures_last(self):
        small, big = row(side="Under", edge=0.01), row(edge=0.05)
        gone = row(market="spreads", side="Home", edge=0.09)
        state = {scan_key(gone): gone}
        changes = diff_bets(state, [small, big], 0.25, include_dropped=True)
        assert [c.kind for c in changes] == [NEW, NEW, DROPPED]
        assert changes[0].row is big

    def test_a_line_is_tracked_across_a_number_change(self):
        """The key ignores the number, so a moved line is not 'new' plus 'dropped'."""
        before = row()
        changes = diff_bets({scan_key(before): before}, [row(dk_point=53.0)], 0.25,
                            include_dropped=True)
        assert [c.kind for c in changes] == [NUMBER]


class TestLoop:
    def _args(self, tmp_path, cfg, *extra):
        argv = normalize_argv([
            "watch", "--cache-file", str(FIXTURE), "--out-dir", str(tmp_path / "runs"),
            "--db", str(tmp_path / "log.sqlite"), "--no-color", "--interval", "0",
            "--min-edge", "1", "--bankroll", "10000", *extra,
        ])
        args = build_parser().parse_args(argv)
        apply_overrides(cfg, args)  # keep every artifact inside tmp_path
        return args

    def test_first_pass_reports_everything_then_goes_quiet(self, tmp_path, cfg, capsys):
        args = self._args(tmp_path, cfg, "--iterations", "2")
        assert watch(cfg, args, scan_options(args, cfg, ("h2h", "spreads", "totals"))) == 0
        out = capsys.readouterr().out
        assert out.count("new ") >= 3
        assert "nothing new" in out

    def test_a_moved_price_shows_up_on_the_second_pass(self, tmp_path, cfg, capsys, monkeypatch):
        moved = json.loads(FIXTURE.read_text())
        for game in moved["data"]:
            if game["id"] != "g2michigan":
                continue
            for book in game["bookmakers"]:
                if book["key"] != "draftkings":
                    continue
                for market in book["markets"]:
                    if market["key"] == "spreads":
                        for outcome in market["outcomes"]:
                            if outcome["name"].startswith("Michigan"):
                                outcome["price"] = 125
        second = tmp_path / "moved.json"
        second.write_text(json.dumps(moved))

        args = self._args(tmp_path, cfg, "--iterations", "2")
        options = scan_options(args, cfg, ("h2h", "spreads", "totals"))
        calls = {"n": 0}
        real_parse = parse_games

        def alternating(payload):
            calls["n"] += 1
            if calls["n"] > 1:
                return real_parse(json.loads(second.read_text())["data"])
            return real_parse(payload)

        monkeypatch.setattr("cfb_edge.scan.parse_games", alternating)
        watch(cfg, args, options)
        out = capsys.readouterr().out
        assert "price +110→+125" in out

    def test_a_failed_scan_does_not_end_the_watch(self, tmp_path, cfg, capsys, monkeypatch):
        args = self._args(tmp_path, cfg, "--iterations", "1")
        monkeypatch.setattr(
            "cfb_edge.watch.run_scan",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert watch(cfg, args, scan_options(args, cfg, ("h2h",))) == 1
        assert "scan failed" in capsys.readouterr().err

    def test_a_replay_does_not_force_a_refresh(self, tmp_path, cfg):
        """--cache-file means 'score this snapshot', so the loop must not refetch."""
        args = self._args(tmp_path, cfg, "--iterations", "1")
        options = scan_options(args, cfg, ("totals",))
        assert options.refresh is False
        assert watch(cfg, args, options) == 0

    def test_each_pass_is_logged_to_sqlite(self, tmp_path, cfg, capsys):
        import sqlite3

        args = self._args(tmp_path, cfg, "--iterations", "2")
        watch(cfg, args, scan_options(args, cfg, ("totals",)))
        capsys.readouterr()
        conn = sqlite3.connect(tmp_path / "log.sqlite")
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        conn.close()
