"""Tests for the dashboard: auth, the JSON API and the scan schedule."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cfb_edge.config import Config
from cfb_edge.edges import evaluate_games, rank
from cfb_edge.scan import ScanOptions
from cfb_edge.web.app import apply_env_overrides, create_app, read_form_field
from cfb_edge.web.auth import COOKIE_NAME, MAX_FAILURES, Auth, AuthNotConfigured
from cfb_edge.web.service import Dashboard, markets_present, serialize_row

FIXTURE = Path(__file__).parent / "fixtures" / "sample_odds.json"
PASSWORD = "let-my-friends-in"


def make_dashboard(cfg: Config, **kwargs) -> Dashboard:
    return Dashboard(
        cfg,
        options=ScanOptions(
            markets=("h2h", "spreads", "totals"),
            min_edge=0.0,
            cache_file=FIXTURE,
            write_files=False,
            write_db=False,
        ),
        display_min_edge=1.0,
        **kwargs,
    )


@pytest.fixture
def web_cfg(tmp_path):
    cfg = Config(bankroll=10_000.0)
    cfg.cache_dir = tmp_path / "cache"
    cfg.db_path = tmp_path / "log.sqlite"
    cfg.out_dir = tmp_path / "runs"
    return cfg


@pytest.fixture
def dashboard(web_cfg):
    return make_dashboard(web_cfg)


@pytest.fixture
def client(web_cfg, dashboard):
    app = create_app(
        cfg=web_cfg,
        auth=Auth(password=PASSWORD, secret_key="test-secret"),
        dashboard=dashboard,
        start_scheduler=False,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client):
    client.post("/login", data={"password": PASSWORD})
    return client


@pytest.fixture
def loaded(signed_in, dashboard):
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(dashboard.refresh())
    return signed_in


class TestAuthUnit:
    def test_token_round_trip(self):
        auth = Auth(password="hunter2")
        assert auth.valid_token(auth.issue_token())

    def test_a_tampered_token_is_rejected(self):
        auth = Auth(password="hunter2")
        token = auth.issue_token()
        expires, _, signature = token.partition(".")
        assert not auth.valid_token(f"{int(expires) + 86400}.{signature}")

    def test_an_expired_token_is_rejected(self):
        auth = Auth(password="hunter2")
        assert not auth.valid_token(auth.issue_token(now=0))

    @pytest.mark.parametrize("junk", ["", None, "nonsense", "abc.def", "123"])
    def test_junk_tokens_are_rejected(self, junk):
        assert not Auth(password="hunter2").valid_token(junk)

    def test_a_token_from_another_password_is_rejected(self):
        stolen = Auth(password="old-password").issue_token()
        assert not Auth(password="new-password").valid_token(stolen)

    def test_an_explicit_secret_key_survives_a_password_change(self):
        first = Auth(password="a", secret_key="fixed")
        assert Auth(password="b", secret_key="fixed").valid_token(first.issue_token())

    def test_password_check(self):
        auth = Auth(password="hunter2")
        assert auth.check_password("hunter2")
        assert not auth.check_password("Hunter2")
        assert not auth.check_password("")

    def test_no_password_means_nothing_works(self):
        auth = Auth(password=None)
        assert not auth.configured
        assert not auth.valid_token("anything")
        with pytest.raises(AuthNotConfigured):
            auth.check_password("guess")

    def test_from_env(self):
        auth = Auth.from_env({"DASHBOARD_PASSWORD": "  spaced  ", "SECRET_KEY": "k"})
        assert auth.password == "spaced"
        assert auth.secret_key == "k"

    def test_blank_env_password_is_not_configured(self):
        assert not Auth.from_env({"DASHBOARD_PASSWORD": "   "}).configured


class TestThrottle:
    def test_blocks_after_repeated_failures(self):
        auth = Auth(password="hunter2")
        for _ in range(MAX_FAILURES):
            auth.record_failure("10.0.0.1")
        assert auth.throttled("10.0.0.1")
        assert auth.seconds_until_unthrottled("10.0.0.1") > 0

    def test_other_clients_are_unaffected(self):
        auth = Auth(password="hunter2")
        for _ in range(MAX_FAILURES):
            auth.record_failure("10.0.0.1")
        assert not auth.throttled("10.0.0.2")

    def test_failures_age_out(self):
        auth = Auth(password="hunter2")
        for _ in range(MAX_FAILURES):
            auth.record_failure("10.0.0.1", now=1000.0)
        assert auth.throttled("10.0.0.1", now=1000.0)
        assert not auth.throttled("10.0.0.1", now=1000.0 + 301)

    def test_a_success_clears_the_count(self):
        auth = Auth(password="hunter2")
        auth.record_failure("10.0.0.1")
        auth.clear_failures("10.0.0.1")
        assert not auth.throttled("10.0.0.1")


class TestAccessControl:
    def test_health_needs_no_password(self, client):
        body = client.get("/healthz").json()
        assert body["ok"] is True
        assert body["password_configured"] is True

    def test_the_dashboard_redirects_to_login(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_the_api_returns_401_not_a_redirect(self, client):
        assert client.get("/api/state").status_code == 401
        assert client.post("/api/refresh").status_code == 401

    def test_the_wrong_password_is_refused(self, client):
        response = client.post("/login", data={"password": "nope"}, follow_redirects=False)
        assert response.status_code == 401
        assert "Wrong password" in response.text
        assert COOKIE_NAME not in response.cookies

    def test_the_right_password_signs_you_in(self, client):
        response = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert client.get("/", follow_redirects=False).status_code == 200

    def test_the_session_cookie_is_locked_down(self, client):
        response = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        header = response.headers["set-cookie"]
        assert "HttpOnly" in header
        assert "SameSite=lax" in header

    def test_the_cookie_is_marked_secure_behind_https(self, client):
        response = client.post(
            "/login", data={"password": PASSWORD},
            headers={"x-forwarded-proto": "https"}, follow_redirects=False,
        )
        assert "Secure" in response.headers["set-cookie"]

    def test_a_forged_cookie_does_not_get_in(self, client):
        client.cookies.set(COOKIE_NAME, "99999999999.forged")
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.get("/api/state").status_code == 401

    def test_signing_out_clears_the_session(self, signed_in):
        signed_in.post("/logout", follow_redirects=False)
        assert signed_in.get("/", follow_redirects=False).status_code == 303

    def test_login_page_redirects_when_already_signed_in(self, signed_in):
        response = signed_in.get("/login", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"

    def test_repeated_guesses_get_throttled(self, client):
        for _ in range(MAX_FAILURES):
            client.post("/login", data={"password": "guess"})
        response = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert response.status_code == 429
        assert "Too many attempts" in response.text

    def test_the_throttle_is_per_forwarded_address(self, client):
        for _ in range(MAX_FAILURES):
            client.post("/login", data={"password": "guess"},
                        headers={"x-forwarded-for": "1.1.1.1"})
        response = client.post(
            "/login", data={"password": PASSWORD}, headers={"x-forwarded-for": "2.2.2.2"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    def test_a_missing_password_field_is_just_a_failed_login(self, client):
        assert client.post("/login", content=b"").status_code == 401


class TestUnconfigured:
    @pytest.fixture
    def open_client(self, web_cfg, dashboard):
        app = create_app(cfg=web_cfg, auth=Auth(password=None), dashboard=dashboard,
                         start_scheduler=False)
        with TestClient(app) as client:
            yield client

    def test_the_dashboard_refuses_to_serve(self, open_client):
        response = open_client.get("/")
        assert response.status_code == 503
        assert "DASHBOARD_PASSWORD is not set" in response.text

    def test_the_api_refuses_too(self, open_client):
        response = open_client.get("/api/state")
        assert response.status_code == 503
        assert "DASHBOARD_PASSWORD" in response.json()["detail"]

    def test_logging_in_is_impossible(self, open_client):
        assert open_client.post("/login", data={"password": "anything"}).status_code == 503

    def test_health_still_answers_and_says_so(self, open_client):
        body = open_client.get("/healthz").json()
        assert body["ok"] is True
        assert body["password_configured"] is False


class TestStateApi:
    def test_serves_the_ranked_board(self, loaded):
        payload = loaded.get("/api/state").json()
        rows = payload["rows"]
        # Every non-negative edge on the sample board, best first.
        assert len(rows) == 5
        edges = [row["edge_pct"] for row in rows]
        assert edges == sorted(edges, reverse=True)

    def test_rows_carry_what_the_table_needs(self, loaded):
        row = loaded.get("/api/state").json()["rows"][0]
        for key in ("matchup", "kickoff_et", "market_label", "pick", "dk_price",
                    "sharp_price", "fair_pct", "dk_pct", "edge_pct", "ev_per_100", "stake"):
            assert key in row, key
        assert row["dk_price"].startswith(("+", "-"))

    def test_kickoffs_are_eastern(self, loaded):
        rows = loaded.get("/api/state").json()["rows"]
        assert all(r["kickoff_et"].endswith(("AM", "PM")) for r in rows)
        noon = next(r for r in rows if r["kickoff_utc"] == "2026-09-20T16:00:00Z")
        assert noon["kickoff_et"] == "Sun 09/20 12:00 PM"

    def test_the_meta_block_carries_the_stamp(self, loaded):
        meta = loaded.get("/api/state").json()["meta"]
        assert meta["ready"] is True
        assert meta["updated_at_et"].endswith(("AM", "PM"))
        assert meta["updated_at_utc"].endswith("Z")
        assert meta["odds_fetched_at_et"].endswith(("AM", "PM"))
        assert meta["games"] == 4
        assert meta["error"] is None
        assert meta["default_min_edge"] == 1.0
        assert meta["refresh_minutes"] == 30.0

    def test_markets_are_offered_as_filters(self, loaded):
        meta = loaded.get("/api/state").json()["meta"]
        assert [m["key"] for m in meta["markets"]] == ["h2h", "spreads", "totals"]
        assert [m["label"] for m in meta["markets"]] == ["Moneyline", "Spread", "Total"]

    def test_the_board_is_sent_unfiltered_so_the_page_can_filter_down(self, loaded):
        """The page's edge filter has to be able to go below the default."""
        rows = loaded.get("/api/state").json()["rows"]
        assert any(row["edge_pct"] < 1.0 for row in rows)

    def test_before_the_first_scan_it_is_empty_but_valid(self, signed_in):
        payload = signed_in.get("/api/state").json()
        assert payload["rows"] == []
        assert payload["meta"]["ready"] is False
        assert payload["meta"]["updated_at_et"] is None

    def test_the_page_renders_with_the_title(self, signed_in, web_cfg):
        body = signed_in.get("/").text
        assert "<table" in body
        assert web_cfg.web_title in body
        assert 'id="minEdge"' in body and 'id="market"' in body


class TestRefreshEndpoint:
    def test_a_manual_refresh_updates_the_state(self, signed_in):
        payload = signed_in.post("/api/refresh").json()
        assert payload["refreshed"] is True
        assert payload["message"] == "updated"
        assert payload["meta"]["ready"] is True

    def test_a_second_refresh_is_rate_limited(self, signed_in):
        signed_in.post("/api/refresh")
        payload = signed_in.post("/api/refresh").json()
        assert payload["refreshed"] is False
        assert "try again" in payload["message"]

    def test_the_rate_limit_still_returns_the_current_board(self, signed_in):
        signed_in.post("/api/refresh")
        payload = signed_in.post("/api/refresh").json()
        assert payload["rows"]


class TestService:
    def test_serialize_row(self, sample_games, cfg):
        rows = rank(evaluate_games(sample_games, cfg, ("spreads",)), 1.0)
        data = serialize_row(rows[0])
        assert data["pick"] == "Michigan Wolverines +6.5"
        assert data["dk_price"] == "+110"
        assert data["sharp_price"] == "-110"
        assert data["edge_pct"] == 2.38
        assert data["translated"] is False
        assert data["kickoff_et"] == "Sun 09/20 12:00 PM"

    def test_a_translated_row_is_marked(self, sample_games, cfg, halfpoint_table):
        rows = rank(evaluate_games(sample_games, cfg, ("totals",), halfpoint_table), 1.0)
        translated = next(r for r in rows if r.status == "translated")
        data = serialize_row(translated)
        assert data["translated"] is True
        assert data["sharp_point"] == 53.0

    def test_markets_present_skips_markets_with_no_rows(self, sample_games, cfg):
        rows = rank(evaluate_games(sample_games, cfg, ("h2h", "spreads", "totals")), 1.0)
        markets = markets_present(("h2h", "spreads", "totals", "player_pass_yds"), rows)
        assert [m["key"] for m in markets] == ["h2h", "spreads", "totals"]

    def test_a_failed_scan_keeps_the_last_good_board(self, web_cfg):
        dash = make_dashboard(web_cfg)
        asyncio.run(dash.refresh())
        good_rows = list(dash.state.rows)
        assert good_rows

        dash.options = ScanOptions(cache_file=Path("/nonexistent/file.json"))
        assert asyncio.run(dash.refresh()) is False
        assert dash.state.rows == good_rows
        assert dash.state.error is not None
        assert dash.state.last_attempt_utc is not None

    def test_a_scan_that_never_succeeded_reports_the_error(self, web_cfg):
        dash = Dashboard(web_cfg, options=ScanOptions(cache_file=Path("/nope.json")))
        assert asyncio.run(dash.refresh()) is False
        assert dash.state.ready is False
        assert "Error" in dash.state.error or "error" in dash.state.error.lower()

    def test_the_scheduler_runs_a_scan_and_stops_cleanly(self, web_cfg):
        async def exercise():
            dash = make_dashboard(web_cfg, refresh_minutes=60)
            await dash.start()
            for _ in range(100):
                if dash.state.ready:
                    break
                await asyncio.sleep(0.05)
            await dash.stop()
            return dash.state

        state = asyncio.run(exercise())
        assert state.ready is True
        assert state.rows
        assert state.next_refresh_utc is not None

    def test_the_refresh_interval_has_a_floor(self, web_cfg):
        assert make_dashboard(web_cfg, refresh_minutes=0.0).refresh_minutes == 1.0

    def test_writes_are_off_by_default_in_these_tests(self, web_cfg):
        dash = make_dashboard(web_cfg)
        asyncio.run(dash.refresh())
        assert not web_cfg.out_dir.exists()
        assert not web_cfg.db_path.exists()


class TestFormParsing:
    @pytest.mark.parametrize(
        "body,expected",
        [
            (b"password=secret", "secret"),
            (b"password=p%40ss+word", "p@ss word"),
            (b"password=", ""),
            (b"other=1", ""),
            (b"", ""),
            (b"\xff\xfe", ""),
        ],
    )
    def test_reads_one_field(self, body, expected):
        assert read_form_field(body, "password") == expected

    def test_an_oversized_body_is_ignored(self):
        assert read_form_field(b"password=" + b"x" * 100_000, "password") == ""


class TestEnvOverrides:
    def test_numbers(self):
        cfg = apply_env_overrides(
            Config(), {"BANKROLL": "5000", "KELLY_FRACTION": "0.5", "MIN_EDGE": "2"}
        )
        assert (cfg.bankroll, cfg.kelly_fraction, cfg.min_edge) == (5000.0, 0.5, 2.0)

    def test_markets(self):
        cfg = apply_env_overrides(Config(), {"MARKETS": "spreads, totals"})
        assert cfg.markets == ("spreads", "totals")

    def test_data_dir_moves_everything_it_writes(self):
        cfg = apply_env_overrides(Config(), {"DATA_DIR": "/mnt/vol"})
        assert cfg.db_path == Path("/mnt/vol/cfb_edge.sqlite")
        assert cfg.cache_dir == Path("/mnt/vol/cache")

    def test_title(self):
        assert apply_env_overrides(Config(), {"DASHBOARD_TITLE": "Board"}).web_title == "Board"

    def test_nonsense_numbers_are_ignored(self):
        cfg = apply_env_overrides(Config(bankroll=1234.0), {"BANKROLL": "lots"})
        assert cfg.bankroll == 1234.0

    def test_a_useless_kelly_fraction_is_replaced(self):
        assert apply_env_overrides(Config(), {"KELLY_FRACTION": "0"}).kelly_fraction == 0.25

    def test_nothing_set_changes_nothing(self):
        cfg = apply_env_overrides(Config(bankroll=777.0), {})
        assert cfg.bankroll == 777.0
        assert cfg.markets == ("h2h", "spreads", "totals")


class TestServeCommand:
    def test_the_port_comes_from_the_environment(self, monkeypatch):
        import cfb_edge.cli as cli

        captured = {}
        monkeypatch.setenv("PORT", "9123")
        monkeypatch.setenv("DASHBOARD_PASSWORD", "x")
        monkeypatch.setattr(
            "uvicorn.run", lambda app, **kwargs: captured.update(app=app, **kwargs)
        )
        assert cli.main(["serve"]) == 0
        assert captured["port"] == 9123
        assert captured["app"] == "cfb_edge.web.app:app"
        assert captured["proxy_headers"] is True

    def test_an_explicit_port_wins(self, monkeypatch):
        import cfb_edge.cli as cli

        captured = {}
        monkeypatch.setenv("PORT", "9123")
        monkeypatch.setenv("DASHBOARD_PASSWORD", "x")
        monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.update(**kwargs))
        cli.main(["serve", "--port", "8080", "--host", "0.0.0.0"])
        assert (captured["port"], captured["host"]) == (8080, "0.0.0.0")

    def test_a_missing_password_warns_but_still_starts(self, monkeypatch, capsys):
        import cfb_edge.cli as cli

        monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
        monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: None)
        assert cli.main(["serve"]) == 0
        assert "DASHBOARD_PASSWORD is not set" in capsys.readouterr().err
