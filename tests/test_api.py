"""Tests for the API client, using a stub in place of the network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import cfb_edge.api as api
from cfb_edge.api import OddsApiError, fetch_odds, get_odds, request_params
from cfb_edge.cache import save_response


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def api_cfg(cfg, tmp_path):
    cfg.api_key = "test-key"
    cfg.cache_dir = tmp_path / "cache"
    return cfg


class TestRequestParams:
    def test_asks_for_american_odds_and_every_book(self, cfg):
        params = request_params(cfg, ("h2h", "spreads"))
        assert params["oddsFormat"] == "american"
        assert params["markets"] == "h2h,spreads"
        assert params["regions"] == "us"
        for key in ("draftkings", "pinnacle", "circasports", "fanduel", "betmgm", "williamhill_us"):
            assert key in params["bookmakers"]

    def test_never_contains_the_key(self, cfg):
        cfg.api_key = "secret"
        assert "secret" not in str(request_params(cfg, ("h2h",)))


class TestFetch:
    def test_sends_the_key_and_returns_quota(self, api_cfg, monkeypatch):
        captured = {}

        def fake_get(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse(
                200, [{"id": "abc"}], {"x-requests-remaining": "480", "x-requests-used": "20"}
            )

        monkeypatch.setattr(api.requests, "get", fake_get)
        data, quota, _ = fetch_odds(api_cfg, ("h2h",))
        assert data == [{"id": "abc"}]
        assert quota == {"remaining": "480", "used": "20"}
        assert captured["url"].endswith("/sports/americanfootball_ncaaf/odds")
        assert captured["params"]["apiKey"] == "test-key"

    def test_missing_key_is_reported_before_any_request(self, cfg, monkeypatch):
        cfg.api_key = None

        def explode(*args, **kwargs):
            raise AssertionError("should not reach the network")

        monkeypatch.setattr(api.requests, "get", explode)
        with pytest.raises(OddsApiError, match="ODDS_API_KEY"):
            fetch_odds(cfg, ("h2h",))

    @pytest.mark.parametrize(
        "status,message",
        [(401, "key was rejected"), (429, "quota exhausted"), (422, "bad parameters"), (500, "500")],
    )
    def test_http_errors_are_translated(self, api_cfg, monkeypatch, status, message):
        monkeypatch.setattr(
            api.requests, "get", lambda *a, **k: FakeResponse(status, text="boom")
        )
        with pytest.raises(OddsApiError, match=message):
            fetch_odds(api_cfg, ("h2h",))

    def test_network_failure_is_translated(self, api_cfg, monkeypatch):
        def fail(*args, **kwargs):
            raise api.requests.RequestException("connection reset")

        monkeypatch.setattr(api.requests, "get", fail)
        with pytest.raises(OddsApiError, match="connection reset"):
            fetch_odds(api_cfg, ("h2h",))

    def test_unexpected_payload_shape_is_rejected(self, api_cfg, monkeypatch):
        monkeypatch.setattr(api.requests, "get", lambda *a, **k: FakeResponse(200, {"message": "hi"}))
        with pytest.raises(OddsApiError, match="expected a list"):
            fetch_odds(api_cfg, ("h2h",))


class TestCachePolicy:
    def test_fresh_cache_is_reused_without_a_request(self, api_cfg, monkeypatch):
        params = request_params(api_cfg, ("h2h",))
        save_response(api_cfg.cache_dir, api_cfg.sport, params, [{"id": "cached"}])
        monkeypatch.setattr(
            api.requests, "get", lambda *a, **k: pytest.fail("should not call the API")
        )
        snapshot, source = get_odds(api_cfg, ("h2h",))
        assert source == "cache"
        assert snapshot.data == [{"id": "cached"}]

    def test_stale_cache_triggers_a_fetch_and_is_saved(self, api_cfg, monkeypatch):
        params = request_params(api_cfg, ("h2h",))
        save_response(
            api_cfg.cache_dir, api_cfg.sport, params, [{"id": "old"}],
            fetched_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        monkeypatch.setattr(api.requests, "get", lambda *a, **k: FakeResponse(200, [{"id": "new"}]))
        snapshot, source = get_odds(api_cfg, ("h2h",))
        assert source == "api"
        assert snapshot.data == [{"id": "new"}]
        assert snapshot.path.exists()

    def test_max_age_override(self, api_cfg, monkeypatch):
        params = request_params(api_cfg, ("h2h",))
        save_response(
            api_cfg.cache_dir, api_cfg.sport, params, [{"id": "old"}],
            fetched_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        )
        monkeypatch.setattr(api.requests, "get", lambda *a, **k: FakeResponse(200, [{"id": "new"}]))
        assert get_odds(api_cfg, ("h2h",), max_age_minutes=60)[1] == "cache"
        assert get_odds(api_cfg, ("h2h",), max_age_minutes=5)[1] == "api"

    def test_refresh_ignores_a_fresh_cache(self, api_cfg, monkeypatch):
        params = request_params(api_cfg, ("h2h",))
        save_response(api_cfg.cache_dir, api_cfg.sport, params, [{"id": "cached"}])
        monkeypatch.setattr(api.requests, "get", lambda *a, **k: FakeResponse(200, [{"id": "fresh"}]))
        snapshot, source = get_odds(api_cfg, ("h2h",), refresh=True)
        assert (source, snapshot.data) == ("api", [{"id": "fresh"}])

    def test_cache_only_uses_a_stale_pull_rather_than_the_api(self, api_cfg, monkeypatch):
        params = request_params(api_cfg, ("h2h",))
        save_response(
            api_cfg.cache_dir, api_cfg.sport, params, [{"id": "old"}],
            fetched_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        monkeypatch.setattr(
            api.requests, "get", lambda *a, **k: pytest.fail("should not call the API")
        )
        assert get_odds(api_cfg, ("h2h",), cache_only=True)[1] == "cache"

    def test_cache_only_with_nothing_cached_raises(self, api_cfg):
        with pytest.raises(OddsApiError, match="--cache-only"):
            get_odds(api_cfg, ("h2h",), cache_only=True)

    def test_a_different_market_set_does_not_reuse_the_cache(self, api_cfg, monkeypatch):
        save_response(
            api_cfg.cache_dir, api_cfg.sport, request_params(api_cfg, ("h2h",)), [{"id": "h2h"}]
        )
        monkeypatch.setattr(api.requests, "get", lambda *a, **k: FakeResponse(200, [{"id": "totals"}]))
        snapshot, source = get_odds(api_cfg, ("totals",))
        assert (source, snapshot.data) == ("api", [{"id": "totals"}])
