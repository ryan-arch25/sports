"""Tests for response caching and payload parsing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from cfb_edge.api import request_params
from cfb_edge.cache import latest_response, params_fingerprint, read_response, save_response
from cfb_edge.models import parse_games


class TestCache:
    def test_round_trips_a_response(self, tmp_path, sample_envelope):
        saved = save_response(
            tmp_path, "americanfootball_ncaaf", {"markets": "h2h"},
            sample_envelope["data"], quota={"remaining": "487"},
        )
        loaded = read_response(saved.path)
        assert loaded.data == sample_envelope["data"]
        assert loaded.quota["remaining"] == "487"

    def test_filenames_are_timestamped(self, tmp_path, sample_envelope):
        one = save_response(
            tmp_path, "ncaaf", {"markets": "h2h"}, sample_envelope["data"],
            fetched_at=datetime(2026, 9, 18, 14, 5, tzinfo=timezone.utc),
        )
        assert one.path.name.endswith("_20260918T140500Z.json")

    def test_latest_returns_the_newest_matching_pull(self, tmp_path):
        params = {"markets": "h2h"}
        for minute in (0, 30, 15):
            save_response(
                tmp_path, "ncaaf", params, [{"id": str(minute)}],
                fetched_at=datetime(2026, 9, 18, 14, minute, tzinfo=timezone.utc),
            )
        newest = latest_response(tmp_path, "ncaaf", params)
        assert newest.data == [{"id": "30"}]

    def test_different_requests_cache_separately(self, tmp_path):
        save_response(tmp_path, "ncaaf", {"markets": "h2h"}, [{"id": "a"}])
        assert latest_response(tmp_path, "ncaaf", {"markets": "totals"}) is None
        assert params_fingerprint({"markets": "h2h"}) != params_fingerprint({"markets": "totals"})

    def test_missing_cache_directory_is_not_an_error(self, tmp_path):
        assert latest_response(tmp_path / "nope", "ncaaf", {}) is None

    def test_age_is_measured_from_the_fetch_time(self, tmp_path):
        saved = save_response(
            tmp_path, "ncaaf", {}, [],
            fetched_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        assert read_response(saved.path).age_minutes == pytest.approx(20, abs=0.5)

    def test_api_key_never_reaches_the_cache(self, tmp_path, cfg, sample_envelope):
        params = request_params(cfg, ("h2h",))
        saved = save_response(tmp_path, cfg.sport, params, sample_envelope["data"])
        assert "apiKey" not in saved.path.read_text(encoding="utf-8")

    def test_a_bare_api_response_can_be_replayed(self, tmp_path, sample_envelope):
        path = tmp_path / "raw.json"
        path.write_text(json.dumps(sample_envelope["data"]), encoding="utf-8")
        assert len(read_response(path).data) == 4


class TestParsing:
    def test_reads_games_books_and_markets(self, sample_games):
        assert len(sample_games) == 4
        game = next(g for g in sample_games if g.event_id == "g1alabama")
        assert game.matchup == "Georgia Bulldogs @ Alabama Crimson Tide"
        assert set(game.books) == {"pinnacle", "draftkings", "fanduel"}
        assert game.market("pinnacle", "totals").outcome("Over").point == 53.0

    def test_commence_time_is_utc_aware(self, sample_games):
        assert all(g.commence_time.tzinfo is timezone.utc for g in sample_games)

    def test_games_are_sorted_by_kickoff(self, sample_games):
        times = [g.commence_time for g in sample_games]
        assert times == sorted(times)

    def test_malformed_events_are_skipped(self):
        games = parse_games([{"id": "x"}, {
            "id": "ok", "commence_time": "2026-09-20T23:30:00Z",
            "home_team": "H", "away_team": "A", "bookmakers": [],
        }])
        assert [g.event_id for g in games] == ["ok"]

    def test_malformed_outcomes_are_skipped(self):
        games = parse_games([{
            "id": "ok", "commence_time": "2026-09-20T23:30:00Z",
            "home_team": "H", "away_team": "A",
            "bookmakers": [{"key": "draftkings", "markets": [{"key": "h2h", "outcomes": [
                {"name": "H", "price": -110}, {"name": "A", "price": None},
            ]}]}],
        }])
        assert len(games[0].market("draftkings", "h2h").outcomes) == 1
