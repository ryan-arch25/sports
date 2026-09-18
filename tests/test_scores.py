"""Tests for the historical results fetcher and store."""

from __future__ import annotations

import pytest

import cfb_edge.scores as scores
from cfb_edge.scores import (
    CfbdClient,
    HistoricalGame,
    ScoresError,
    connect,
    extract_line,
    fetch_season,
    load_games,
    parse_game,
    parse_seasons,
    store_games,
    stored_seasons,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TestParseGame:
    def test_camel_case_payload(self):
        game = parse_game({
            "id": 1, "season": 2024, "week": 5, "seasonType": "regular",
            "startDate": "2024-09-28T16:00:00.000Z", "homeTeam": "Alabama",
            "awayTeam": "Georgia", "homePoints": 27, "awayPoints": 24,
        })
        assert (game.home_team, game.home_score, game.away_score) == ("Alabama", 27, 24)

    def test_snake_case_payload(self):
        game = parse_game({
            "id": 2, "season": 2018, "home_team": "Texas", "away_team": "Oklahoma",
            "home_points": 20, "away_points": 41,
        })
        assert game.home_margin == -21

    def test_unplayed_games_are_dropped(self):
        assert parse_game({"id": 3, "season": 2024, "homeTeam": "A", "awayTeam": "B"}) is None

    def test_garbage_is_dropped(self):
        assert parse_game({"id": "x", "season": "y", "homeTeam": "A", "awayTeam": "B",
                           "homePoints": "q", "awayPoints": 1}) is None


class TestDerivedFields:
    def test_favorite_margin_when_home_is_favored(self):
        game = HistoricalGame(1, 2024, 1, "regular", None, "H", "A", 31, 17, closing_spread=-7)
        assert game.favorite_margin == 14
        assert game.favorite_line == 7

    def test_favorite_margin_when_away_is_favored(self):
        game = HistoricalGame(1, 2024, 1, "regular", None, "H", "A", 17, 31, closing_spread=7)
        assert game.favorite_margin == 14

    def test_an_upset_gives_the_favorite_a_negative_margin(self):
        game = HistoricalGame(1, 2024, 1, "regular", None, "H", "A", 10, 20, closing_spread=-7)
        assert game.favorite_margin == -10

    def test_a_pickem_has_no_favorite(self):
        assert HistoricalGame(1, 2024, 1, "r", None, "H", "A", 10, 7, closing_spread=0).favorite_margin is None

    def test_missing_line_has_no_favorite(self):
        assert HistoricalGame(1, 2024, 1, "r", None, "H", "A", 10, 7).favorite_line is None

    def test_total_points(self):
        assert HistoricalGame(1, 2024, 1, "r", None, "H", "A", 31, 17).total_points == 48


class TestExtractLine:
    def test_provider_priority_wins(self):
        raw = {"lines": [
            {"provider": "numberfire", "spread": -3.0, "overUnder": 50.0},
            {"provider": "Bovada", "spread": -3.5, "overUnder": 52.5},
        ]}
        assert extract_line(raw, ["Bovada", "consensus"]) == (-3.5, 52.5, "Bovada")

    def test_falls_back_to_the_median_of_whatever_is_there(self):
        raw = {"lines": [
            {"provider": "a", "spread": -3.0, "overUnder": 50.0},
            {"provider": "b", "spread": -4.0, "overUnder": 52.0},
            {"provider": "c", "spread": -5.0, "overUnder": 54.0},
        ]}
        spread, total, provider = extract_line(raw, ["Bovada"])
        assert (spread, total) == (-4.0, 52.0)
        assert provider.startswith("median")

    def test_no_lines_at_all(self):
        assert extract_line({"lines": []}, ["Bovada"]) == (None, None, None)

    def test_snake_case_over_under(self):
        raw = {"lines": [{"provider": "Bovada", "spread": -7.0, "over_under": 44.5}]}
        assert extract_line(raw, ["Bovada"])[1] == 44.5


class TestClient:
    def test_requires_a_key(self):
        with pytest.raises(ScoresError, match="CFBD_API_KEY"):
            CfbdClient("")

    def test_sends_a_bearer_token(self, monkeypatch):
        captured = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            captured.update(url=url, params=params, headers=headers)
            return FakeResponse(200, [{"id": 1}])

        monkeypatch.setattr(scores.requests, "get", fake_get)
        assert CfbdClient("k").games(2024, "regular") == [{"id": 1}]
        assert captured["headers"]["Authorization"] == "Bearer k"
        assert captured["params"]["year"] == 2024

    def test_rejected_key(self, monkeypatch):
        monkeypatch.setattr(scores.requests, "get", lambda *a, **k: FakeResponse(401))
        with pytest.raises(ScoresError, match="key was rejected"):
            CfbdClient("k").games(2024, "regular")

    def test_server_error(self, monkeypatch):
        monkeypatch.setattr(scores.requests, "get", lambda *a, **k: FakeResponse(503, text="down"))
        with pytest.raises(ScoresError, match="503"):
            CfbdClient("k").lines(2024, "regular")

    def test_network_failure(self, monkeypatch):
        def fail(*a, **k):
            raise scores.requests.RequestException("reset")

        monkeypatch.setattr(scores.requests, "get", fail)
        with pytest.raises(ScoresError, match="reset"):
            CfbdClient("k").games(2024, "regular")


class TestFetchSeason:
    def test_joins_scores_to_closing_lines(self, monkeypatch):
        games = [{"id": 10, "season": 2024, "homeTeam": "Alabama", "awayTeam": "Georgia",
                  "homePoints": 27, "awayPoints": 24}]
        lines = [{"id": 10, "lines": [{"provider": "Bovada", "spread": -3.5, "overUnder": 52.5}]}]

        def fake_get(url, params=None, headers=None, timeout=None):
            if url.endswith("/games"):
                return FakeResponse(200, games if params["seasonType"] == "regular" else [])
            return FakeResponse(200, lines if params["seasonType"] == "regular" else [])

        monkeypatch.setattr(scores.requests, "get", fake_get)
        result = fetch_season(CfbdClient("k"), 2024, "fbs", ["Bovada"])
        assert len(result) == 1
        assert result[0].closing_spread == -3.5
        assert result[0].closing_total == 52.5

    def test_a_game_with_no_line_is_still_stored(self, monkeypatch):
        games = [{"id": 11, "season": 2024, "homeTeam": "A", "awayTeam": "B",
                  "homePoints": 10, "awayPoints": 7}]

        def fake_get(url, params=None, headers=None, timeout=None):
            if url.endswith("/games"):
                return FakeResponse(200, games if params["seasonType"] == "regular" else [])
            return FakeResponse(200, [])

        monkeypatch.setattr(scores.requests, "get", fake_get)
        result = fetch_season(CfbdClient("k"), 2024, "fbs", ["Bovada"])
        assert result[0].closing_spread is None


class TestStore:
    def test_round_trips(self, tmp_path, synthetic_history):
        conn = connect(tmp_path / "scores.sqlite")
        try:
            store_games(conn, synthetic_history[:50])
            loaded = load_games(conn)
            assert len(loaded) == 50
            assert loaded[0].closing_spread is not None
        finally:
            conn.close()

    def test_rewriting_a_season_does_not_duplicate(self, tmp_path, synthetic_history):
        conn = connect(tmp_path / "scores.sqlite")
        try:
            store_games(conn, synthetic_history[:20])
            store_games(conn, synthetic_history[:20])
            assert len(load_games(conn)) == 20
        finally:
            conn.close()

    def test_min_season_filter(self, tmp_path):
        conn = connect(tmp_path / "scores.sqlite")
        try:
            store_games(conn, [
                HistoricalGame(1, 2019, 1, "r", None, "H", "A", 10, 7, closing_spread=-3),
                HistoricalGame(2, 2024, 1, "r", None, "H", "A", 10, 7, closing_spread=-3),
            ])
            assert [g.season for g in load_games(conn, min_season=2024)] == [2024]
            assert stored_seasons(conn) == {2019: 1, 2024: 1}
        finally:
            conn.close()


class TestSeasonParsing:
    def test_a_range(self):
        assert parse_seasons("2019-2022") == [2019, 2020, 2021, 2022]

    def test_a_backwards_range_is_read_the_right_way_round(self):
        assert parse_seasons("2022-2019") == [2019, 2020, 2021, 2022]

    def test_a_list(self):
        assert parse_seasons("2019,2021") == [2019, 2021]

    def test_a_single_season(self):
        assert parse_seasons("2023") == [2023]

    def test_duplicates_collapse(self):
        assert parse_seasons("2019,2019-2020") == [2019, 2020]

    def test_the_default_is_the_last_ten_completed_seasons(self):
        seasons = parse_seasons(None)
        assert len(seasons) == 10

    def test_garbage_is_rejected(self):
        with pytest.raises(ScoresError):
            parse_seasons("last year")
