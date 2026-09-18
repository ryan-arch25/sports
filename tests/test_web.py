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

    def test_the_board_is_sent_unfiltered_so_the_page_can_filter_down(self, loaded, dashboard):
        """The page's edge filter has to be able to go below the default.

        The scan keeps everything from the floor upwards and the page filters
        from `default_min_edge`, so the slider can always be dialled down.
        """
        payload = loaded.get("/api/state").json()
        assert dashboard.options.min_edge == 0.0
        assert payload["meta"]["default_min_edge"] > dashboard.options.min_edge
        assert all(row["edge_pct"] >= 0.0 for row in payload["rows"])

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


class TestDataDirectories:
    def test_creates_what_the_scan_writes_to(self, tmp_path):
        from cfb_edge.web.app import ensure_data_dirs

        cfg = apply_env_overrides(Config(), {"DATA_DIR": str(tmp_path / "vol")})
        assert ensure_data_dirs(cfg) == []
        assert cfg.cache_dir.is_dir()
        assert cfg.out_dir.is_dir()
        assert cfg.db_path.parent.is_dir()

    def test_is_idempotent_and_keeps_what_is_there(self, tmp_path):
        from cfb_edge.web.app import ensure_data_dirs

        cfg = apply_env_overrides(Config(), {"DATA_DIR": str(tmp_path / "vol")})
        ensure_data_dirs(cfg)
        (cfg.cache_dir / "odds.json").write_text("{}", encoding="utf-8")
        assert ensure_data_dirs(cfg) == []
        assert (cfg.cache_dir / "odds.json").exists()

    def test_reports_a_directory_it_cannot_create(self, tmp_path, monkeypatch):
        from cfb_edge.web.app import ensure_data_dirs

        def refuse(self, *args, **kwargs):
            raise PermissionError(13, "Permission denied")

        cfg = apply_env_overrides(Config(), {"DATA_DIR": str(tmp_path / "vol")})
        monkeypatch.setattr(Path, "mkdir", refuse)
        problems = ensure_data_dirs(cfg)
        assert len(problems) == 3
        assert all("could not create" in p for p in problems)

    def test_reports_a_directory_it_cannot_write_to(self, tmp_path, monkeypatch):
        """The volume exists but is still owned by root."""
        from cfb_edge.web.app import ensure_data_dirs

        cfg = apply_env_overrides(Config(), {"DATA_DIR": str(tmp_path / "vol")})
        ensure_data_dirs(cfg)
        monkeypatch.setattr("cfb_edge.web.app.os.access", lambda path, mode: False)
        problems = ensure_data_dirs(cfg)
        assert problems and all("is not writable by uid" in p for p in problems)

    def test_startup_records_the_problems(self, web_cfg, dashboard, monkeypatch, caplog):
        monkeypatch.setattr(
            "cfb_edge.web.app.ensure_data_dirs", lambda cfg: ["/data is not writable by uid 10001"]
        )
        app = create_app(cfg=web_cfg, auth=Auth(password=PASSWORD), dashboard=dashboard,
                         start_scheduler=False)
        with caplog.at_level("ERROR"), TestClient(app) as client:
            body = client.get("/healthz").json()
        assert body["data_dir_problems"] == ["/data is not writable by uid 10001"]
        assert "mounted volume" in caplog.text

    def test_health_reports_a_clean_data_directory(self, client, web_cfg):
        body = client.get("/healthz").json()
        assert body["data_dir_problems"] == []
        assert body["data_dir"] == str(web_cfg.db_path.parent)

    def test_startup_creates_the_directories_for_real(self, tmp_path, dashboard):
        cfg = apply_env_overrides(Config(), {"DATA_DIR": str(tmp_path / "vol")})
        app = create_app(cfg=cfg, auth=Auth(password=PASSWORD), dashboard=dashboard,
                         start_scheduler=False)
        with TestClient(app):
            pass
        assert (tmp_path / "vol" / "cache").is_dir()
        assert (tmp_path / "vol" / "runs").is_dir()


class TestLineDiffInTheDashboard:
    def test_rows_carry_the_difference(self, loaded):
        rows = loaded.get("/api/state").json()["rows"]
        assert all("line_diff" in row for row in rows)

    def test_same_number_rows_report_zero(self, loaded):
        rows = loaded.get("/api/state").json()["rows"]
        spread = next(r for r in rows if r["pick"] == "Michigan Wolverines +6.5")
        assert spread["line_diff"] == 0

    def test_a_moneyline_has_nothing_to_compare(self, loaded):
        rows = loaded.get("/api/state").json()["rows"]
        moneyline = next(r for r in rows if r["market"] == "h2h")
        assert moneyline["line_diff"] is None

    def test_a_moved_number_reports_both_directions(self, web_cfg, halfpoint_table, tmp_path):
        import asyncio

        from cfb_edge.halfpoint import save_table

        web_cfg.halfpoint_table_path = save_table(halfpoint_table, tmp_path / "hp.json")
        dash = make_dashboard(web_cfg)
        asyncio.run(dash.refresh())
        by_pick = {row["pick"]: row for row in dash.state.rows}
        assert by_pick["Over 51.5"]["line_diff"] == pytest.approx(1.5)
        assert by_pick["Over 51.5"]["translated"] is True

    def test_the_page_has_the_column(self, signed_in):
        body = signed_in.get("/").text
        assert "Line" in body
        assert 'class="num better"' in body or "better" in body
        assert "line_diff" in body  # the renderer reads it


class TestSlateApi:
    def test_the_state_carries_the_slate(self, loaded):
        payload = loaded.get("/api/state").json()
        assert "slate" in payload
        assert [day["label"] for day in payload["slate"]] == ["Sunday, September 20"]

    def test_every_game_is_there_not_just_the_bettable_ones(self, loaded):
        payload = loaded.get("/api/state").json()
        games = [g for day in payload["slate"] for g in day["games"]]
        assert len(games) == payload["meta"]["games"] == 4
        # The ranked view only keeps four sides; the slate keeps every game.
        assert len(games) > len({row["event_id"] for row in payload["rows"]})

    def test_a_game_with_no_sharp_line_still_appears(self, loaded):
        games = [g for day in loaded.get("/api/state").json()["slate"] for g in day["games"]]
        boise = next(g for g in games if g["event_id"] == "g4boise")
        assert boise["sides"][0]["h2h"]["verdict"] is None
        assert boise["sides"][0]["spread"] is None

    def test_cells_carry_the_verdict_and_the_edge(self, loaded):
        games = [g for day in loaded.get("/api/state").json()["slate"] for g in day["games"]]
        michigan = next(g for g in games if g["event_id"] == "g2michigan")
        underdog = next(s for s in michigan["sides"] if s["label"] == "Michigan Wolverines")
        assert underdog["spread"]["verdict"] == "better"
        assert underdog["spread"]["edge_pct"] == pytest.approx(2.38, abs=0.01)
        assert underdog["spread"]["number"] == "+6.5"
        assert underdog["spread"]["sharp_number"] == "+6.5"

    def test_before_the_first_scan_the_slate_is_empty(self, signed_in):
        assert signed_in.get("/api/state").json()["slate"] == []

    def test_the_page_ships_both_tabs_and_a_search_box(self, signed_in):
        body = signed_in.get("/").text
        assert 'data-tab="edges"' in body and 'data-tab="slate"' in body
        assert 'id="search"' in body
        assert "renderSlate" in body


class TestEstimatedLabelling:
    def test_a_row_priced_by_the_estimate_serializes_the_flag(self, cfg):
        """What the page reads to decide whether to print `est.`."""
        from cfb_edge.edges import evaluate_market
        from cfb_edge.halfpoint import estimated_table
        from cfb_edge.models import Outcome
        from cfb_edge.web.service import serialize_row

        from conftest import make_game

        game = make_game({
            "draftkings": {"totals": [Outcome("Over", -110, 51.5), Outcome("Under", -110, 51.5)]},
            "pinnacle": {"totals": [Outcome("Over", -105, 53.0), Outcome("Under", -105, 53.0)]},
        })
        row = evaluate_market(game, "totals", cfg, estimated_table(cfg))[0]
        data = serialize_row(row)
        assert data["translated"] is True
        assert data["estimated"] is True
        assert data["line_diff"] == pytest.approx(1.5)

    def test_nothing_is_left_flagged_as_a_different_number(self, web_cfg, tmp_path):
        import asyncio

        web_cfg.halfpoint_table_path = tmp_path / "no-table.json"
        dash = make_dashboard(web_cfg)
        asyncio.run(dash.refresh())
        assert dash.state.flagged_different_number == 0
        assert dash.state.priced_from_estimate == 2

    def test_a_built_table_is_not_labelled_an_estimate(self, web_cfg, tmp_path, halfpoint_table):
        import asyncio

        from cfb_edge.halfpoint import save_table

        web_cfg.halfpoint_table_path = save_table(halfpoint_table, tmp_path / "hp.json")
        dash = make_dashboard(web_cfg)
        asyncio.run(dash.refresh())
        moved = next(r for r in dash.state.rows if r["pick"] == "Over 51.5")
        assert moved["translated"] is True
        assert moved["estimated"] is False
        assert dash.state.priced_from_estimate == 0

    def test_the_slate_marks_estimated_cells(self, web_cfg, tmp_path):
        import asyncio

        web_cfg.halfpoint_table_path = tmp_path / "no-table.json"
        dash = make_dashboard(web_cfg)
        asyncio.run(dash.refresh())
        games = [g for day in dash.state.slate for g in day["games"]]
        alabama = next(g for g in games if g["event_id"] == "g1alabama")
        assert alabama["sides"][0]["total"]["estimated"] is True
        assert alabama["sides"][0]["spread"]["estimated"] is False

    def test_the_page_renders_the_label(self, signed_in):
        body = signed_in.get("/").text
        # Edges keeps the pill; the Slate carries one tag per row instead.
        assert 'row.estimated ? "est." : "\u00bdpt"' in body
        assert 'side.estimated ? "est." : ""' in body


class TestLogApi:
    def bet(self, **kwargs):
        payload = {
            "person": "RS", "event_id": "g2michigan", "market": "spreads",
            "side": "Michigan Wolverines", "point": 6.5, "price": 110, "stake": 25,
        }
        payload.update(kwargs)
        return payload

    def test_the_log_needs_the_password(self, client):
        assert client.get("/api/log").status_code == 401
        assert client.post("/api/log", json=self.bet()).status_code == 401
        assert client.post("/api/log/settle", json={"bet_id": 1, "result": "won"}).status_code == 401

    def test_an_empty_log(self, signed_in):
        payload = signed_in.get("/api/log").json()
        assert payload["bets"] == []
        assert payload["overall"]["bets"] == 0

    def test_adding_returns_the_whole_log(self, loaded):
        payload = loaded.post("/api/log", json=self.bet()).json()
        assert len(payload["bets"]) == 1
        assert payload["bets"][0]["pick"] == "Michigan Wolverines +6.5"
        assert payload["overall"]["open"] == 1

    def test_settling_updates_the_record(self, loaded):
        bet_id = loaded.post("/api/log", json=self.bet()).json()["bets"][0]["bet_id"]
        payload = loaded.post(
            "/api/log/settle", json={"bet_id": bet_id, "result": "won"}
        ).json()
        assert payload["overall"]["won"] == 1
        assert payload["overall"]["profit"] == pytest.approx(27.5)
        assert payload["bets"][0]["result"] == "won"

    def test_closing_line_value_comes_from_the_scan_history(self, web_cfg, tmp_path):
        """The log reads the sharp book's last pre-kickoff price out of the runs."""
        import asyncio

        from cfb_edge.scan import ScanOptions

        dash = Dashboard(
            web_cfg,
            options=ScanOptions(
                markets=("h2h", "spreads", "totals"), min_edge=0.0,
                cache_file=FIXTURE, write_files=False, write_db=True,
            ),
            display_min_edge=1.0,
        )
        app = create_app(
            cfg=web_cfg, auth=Auth(password=PASSWORD, secret_key="t"),
            dashboard=dash, start_scheduler=False,
        )
        with TestClient(app) as client:
            client.post("/login", data={"password": PASSWORD})
            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(dash.refresh())
            bet = client.post("/api/log", json=self.bet(price=150)).json()["bets"][0]
        assert bet["closing_price"] == "-110"
        assert bet["closing_source"] == "circa"
        assert bet["clv_pct"] > 0

    def test_two_people_are_tracked_apart(self, loaded):
        loaded.post("/api/log", json=self.bet(person="RS"))
        payload = loaded.post("/api/log", json=self.bet(person="JT", stake=50)).json()
        assert sorted(p["person"] for p in payload["people"]) == ["JT", "RS"]

    @pytest.mark.parametrize("bad", [
        {"person": ""}, {"stake": 0}, {"price": 0}, {"event_id": ""},
    ])
    def test_a_bad_bet_is_a_400_with_a_reason(self, loaded, bad):
        response = loaded.post("/api/log", json=self.bet(**bad))
        assert response.status_code == 400
        assert response.json()["detail"]

    def test_a_body_that_is_not_json(self, loaded):
        assert loaded.post("/api/log", content=b"nonsense").status_code == 400

    def test_a_body_that_is_not_an_object(self, loaded):
        assert loaded.post("/api/log", json=[1, 2, 3]).status_code == 400

    def test_settling_something_that_does_not_exist(self, loaded):
        response = loaded.post("/api/log/settle", json={"bet_id": 999, "result": "won"})
        assert response.status_code == 400

    def test_settle_needs_both_fields(self, loaded):
        assert loaded.post("/api/log/settle", json={"bet_id": 1}).status_code == 400


class TestPageShell:
    def test_three_tabs(self, signed_in):
        body = signed_in.get("/").text
        for tab in ("edges", "slate", "log"):
            assert 'data-tab="' + tab + '"' in body

    def test_the_tab_bar_is_sticky(self, signed_in):
        body = signed_in.get("/").text
        assert "nav.tabs {" in body
        assert "position: sticky; top: 0;" in body.split("nav.tabs {")[1][:200]

    def test_a_theme_toggle_that_defaults_to_dark(self, signed_in):
        body = signed_in.get("/").text
        assert '<html lang="en" data-theme="dark">' in body
        assert 'id="theme"' in body
        assert ':root[data-theme="light"]' in body

    def test_the_fonts_are_asked_for(self, signed_in):
        body = signed_in.get("/").text
        assert "fonts.googleapis.com" in body
        assert "Barlow+Condensed" in body and "IBM+Plex+Mono" in body

    def test_no_gradients_shadows_or_animations(self, signed_in):
        """The brief asked for none of these."""
        body = signed_in.get("/").text
        style = body.split("<style>")[1].split("</style>")[0]
        for banned in ("gradient", "box-shadow", "@keyframes", "animation:", "transition:"):
            assert banned not in style, banned

    def test_the_slate_says_no_line_rather_than_a_dash(self, signed_in):
        assert '"no line"' in signed_in.get("/").text

    def test_the_best_bets_strip_is_there(self, signed_in):
        body = signed_in.get("/").text
        assert "No edges above 1% right now." in body
        assert "renderBest" in body
