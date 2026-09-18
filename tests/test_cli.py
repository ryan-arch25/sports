"""End-to-end tests: CLI, output files and the SQLite log."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from cfb_edge.cli import main, resolve_markets
from cfb_edge.config import Config, ConfigError, load_config, load_dotenv
from cfb_edge.report import kickoff_et
from cfb_edge.store import SCHEMA_VERSION, connect, init_db

FIXTURE = str(Path(__file__).parent / "fixtures" / "sample_odds.json")


def run(tmp_path, *extra, min_edge="1"):
    argv = [
        "--cache-file", FIXTURE,
        "--out-dir", str(tmp_path / "runs"),
        "--db", str(tmp_path / "log.sqlite"),
        "--min-edge", min_edge,
        "--bankroll", "10000",
        "--no-color",
        *extra,
    ]
    return main(argv)


class TestRun:
    def test_exits_cleanly_and_prints_the_ranked_table(self, tmp_path, capsys):
        assert run(tmp_path) == 0
        out = capsys.readouterr().out
        assert "GAME" in out and "EDGE" in out and "STAKE" in out
        # Best edge first: Michigan +6.5 at 2.4%.
        body = out.split("EDGE")[1]
        assert body.index("Michigan Wolverines +6.5") < body.index("Georgia Bulldogs ML")

    def test_flags_the_different_number_section(self, tmp_path, capsys):
        run(tmp_path)
        out = capsys.readouterr().out
        assert "Different number" in out
        assert "Under 51.5" in out

    def test_hide_different_number(self, tmp_path, capsys):
        run(tmp_path, "--hide-different-number")
        assert "Different number" not in capsys.readouterr().out

    def test_min_edge_filters_the_table(self, tmp_path, capsys):
        run(tmp_path, min_edge="2.3")
        out = capsys.readouterr().out
        assert "Michigan Wolverines +6.5" in out
        assert "Georgia Bulldogs ML" not in out

    def test_no_bets_message(self, tmp_path, capsys):
        run(tmp_path, min_edge="25")
        assert "No DraftKings lines at or above a 25% edge." in capsys.readouterr().out

    def test_market_filter(self, tmp_path, capsys):
        run(tmp_path, "--market", "totals", min_edge="0")
        out = capsys.readouterr().out
        assert "markets: totals" in out
        assert "Moneyline" not in out

    def test_limit(self, tmp_path, capsys):
        run(tmp_path, "--limit", "1", min_edge="0")
        out = capsys.readouterr().out
        assert "1 bet(s) at >= 0% edge" in out
        assert "Michigan Wolverines +6.5" in out
        assert "Georgia Bulldogs ML" not in out

    def test_unknown_market_is_a_usage_error(self, tmp_path, capsys):
        assert run(tmp_path, "--market", "player_props") == 2
        assert "unknown market" in capsys.readouterr().err

    def test_cache_only_without_a_cache_fails_cleanly(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert main(["--cache-only", "--no-db", "--no-files"]) == 1
        assert "--cache-only" in capsys.readouterr().err

    def test_missing_api_key_explains_itself(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("ODDS_API_KEY", raising=False)
        assert main(["--no-db", "--no-files"]) == 1
        assert "ODDS_API_KEY" in capsys.readouterr().err


class TestOutputFiles:
    def test_writes_csv_and_json(self, tmp_path, capsys):
        run(tmp_path)
        capsys.readouterr()
        csv_files = list((tmp_path / "runs").glob("*.csv"))
        json_files = list((tmp_path / "runs").glob("*.json"))
        assert len(csv_files) == len(json_files) == 1

    def test_csv_holds_every_evaluated_line(self, tmp_path, capsys):
        run(tmp_path)
        capsys.readouterr()
        path = next((tmp_path / "runs").glob("*.csv"))
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        assert len(rows) == 20
        assert rows[0]["status"] == "priced"
        assert rows[0]["above_min_edge"] == "True"
        assert {r["status"] for r in rows} == {
            "priced", "different_number", "no_sharp_line"
        }

    def test_csv_kickoff_is_eastern(self, tmp_path, capsys):
        run(tmp_path)
        capsys.readouterr()
        path = next((tmp_path / "runs").glob("*.csv"))
        row = next(csv.DictReader(path.open(encoding="utf-8")))
        assert row["kickoff_et"].endswith("AM") or row["kickoff_et"].endswith("PM")

    def test_json_carries_run_metadata(self, tmp_path, capsys):
        run(tmp_path)
        capsys.readouterr()
        payload = json.loads(next((tmp_path / "runs").glob("*.json")).read_text())
        assert payload["meta"]["min_edge_pct"] == 1.0
        assert payload["meta"]["games"] == 4
        assert payload["meta"]["markets"] == ["h2h", "spreads", "totals"]
        assert payload["meta"]["status_counts"]["priced"] == 16
        assert len(payload["bets"]) == 20
        assert payload["bets"][0]["pick"] == "Michigan Wolverines +6.5"

    def test_no_files_skips_writing(self, tmp_path, capsys):
        run(tmp_path, "--no-files")
        capsys.readouterr()
        assert not (tmp_path / "runs").exists()


class TestSqliteLog:
    def test_logs_the_run_and_every_observation(self, tmp_path, capsys):
        run(tmp_path)
        capsys.readouterr()
        conn = sqlite3.connect(tmp_path / "log.sqlite")
        conn.row_factory = sqlite3.Row
        runs = conn.execute("SELECT * FROM runs").fetchall()
        assert len(runs) == 1
        assert runs[0]["n_games"] == 4
        assert runs[0]["n_rows"] == 20
        assert runs[0]["n_bets"] == 4
        assert runs[0]["source"] == "cache-file"
        observations = conn.execute("SELECT * FROM observations").fetchall()
        assert len(observations) == 20
        conn.close()

    def test_appends_across_runs(self, tmp_path, capsys):
        run(tmp_path)
        run(tmp_path)
        capsys.readouterr()
        conn = sqlite3.connect(tmp_path / "log.sqlite")
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 40
        conn.close()

    def test_clv_view_pairs_each_observation_with_the_closing_line(self, tmp_path, capsys):
        run(tmp_path)
        capsys.readouterr()
        conn = sqlite3.connect(tmp_path / "log.sqlite")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM clv WHERE side = 'Michigan Wolverines' AND market = 'spreads'"
        ).fetchone()
        assert row["closing_dk_price"] == row["dk_price"]
        assert row["clv_prob_delta"] == pytest.approx(0.0)
        conn.close()

    def test_no_db_skips_logging(self, tmp_path, capsys):
        run(tmp_path, "--no-db")
        capsys.readouterr()
        assert not (tmp_path / "log.sqlite").exists()

    def test_schema_is_idempotent(self, tmp_path):
        with connect(tmp_path / "x.sqlite") as conn:
            init_db(conn)
            init_db(conn)
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


class TestConfig:
    def test_defaults(self):
        cfg = Config()
        assert cfg.min_edge == 1.0
        assert cfg.kelly_fraction == 0.25
        assert cfg.sharp_priority == ("pinnacle", "circa")
        assert cfg.target_book == "draftkings"

    def test_toml_overrides(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            """
            bankroll = 2500
            kelly_fraction = 0.5
            min_edge = 2.0
            markets = ["spreads"]

            [books]
            sharp_priority = ["circa"]
            consensus = ["fanduel"]

            [paths]
            db_path = "db/bets.sqlite"

            [cache]
            max_age_minutes = 45
            """,
            encoding="utf-8",
        )
        cfg = load_config(path)
        assert cfg.bankroll == 2500
        assert cfg.kelly_fraction == 0.5
        assert cfg.min_edge == 2.0
        assert cfg.markets == ("spreads",)
        assert cfg.sharp_priority == ("circa",)
        assert cfg.cache_max_age_minutes == 45
        assert cfg.db_path == tmp_path / "db/bets.sqlite"

    def test_table_style_bankroll_section(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            "[bankroll]\namount = 7500\nkelly_fraction = 0.125\nmax_bet_pct = 1.5\n",
            encoding="utf-8",
        )
        cfg = load_config(path)
        assert (cfg.bankroll, cfg.kelly_fraction, cfg.max_bet_pct) == (7500, 0.125, 1.5)

    def test_missing_config_file_is_an_error(self, tmp_path):
        with pytest.raises(ConfigError):
            load_config(tmp_path / "nope.toml")

    def test_bad_kelly_fraction_is_rejected(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("kelly_fraction = 0\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(path)

    def test_dotenv_is_read_without_clobbering_the_environment(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text('# key\nODDS_API_KEY="abc123"\nexport OTHER=1\n', encoding="utf-8")
        monkeypatch.delenv("ODDS_API_KEY", raising=False)
        loaded = load_dotenv(env)
        assert loaded["ODDS_API_KEY"] == "abc123"
        assert loaded["OTHER"] == "1"

        monkeypatch.setenv("ODDS_API_KEY", "from-shell")
        load_dotenv(env)
        import os

        assert os.environ["ODDS_API_KEY"] == "from-shell"

    def test_api_book_keys_include_aliases(self):
        keys = Config().api_book_keys()
        assert keys[0] == "draftkings"
        assert "circasports" in keys and "williamhill_us" in keys

    def test_resolve_markets(self):
        cfg = Config()
        assert resolve_markets(None, cfg) == ("h2h", "spreads", "totals")
        assert resolve_markets(["totals"], cfg) == ("totals",)
        assert resolve_markets(["totals,spreads"], cfg) == ("totals", "spreads")
        assert resolve_markets(["totals", "totals"], cfg) == ("totals",)
        with pytest.raises(ConfigError):
            resolve_markets(["nope"], cfg)


def test_kickoff_is_rendered_in_eastern_time(sample_games):
    game = next(g for g in sample_games if g.event_id == "g1alabama")  # 23:30 UTC
    assert kickoff_et(game.commence_time).endswith("7:30 PM")


class TestPipeAndInterrupt:
    def test_broken_pipe_exits_cleanly(self, tmp_path):
        """`cfb-edge | head` must not traceback."""
        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            f"{sys.executable} -m cfb_edge --cache-file {FIXTURE} --no-db --no-files "
            f"--no-color | head -3",
            shell=True, cwd=repo_root, capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "BrokenPipeError" not in result.stderr
        assert "Traceback" not in result.stderr

    def test_keyboard_interrupt_exits_130(self, tmp_path, monkeypatch, capsys):
        import cfb_edge.cli as cli

        def explode(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "_print_report", explode)
        assert run(tmp_path, "--no-db", "--no-files") == 130
        assert "interrupted" in capsys.readouterr().err


class TestSubcommands:
    def test_bare_flags_still_mean_scan(self):
        from cfb_edge.cli import normalize_argv

        assert normalize_argv(["--min-edge", "2"])[0] == "scan"
        assert normalize_argv([]) == ["scan"]
        assert normalize_argv(["scan", "--min-edge", "2"])[0] == "scan"
        assert normalize_argv(["watch"])[0] == "watch"
        assert normalize_argv(["--help"]) == ["--help"]
        assert normalize_argv(["--version"]) == ["--version"]

    def test_explicit_scan_matches_the_shorthand(self, tmp_path, capsys):
        run(tmp_path)
        shorthand = capsys.readouterr().out
        main([
            "scan", "--cache-file", FIXTURE, "--out-dir", str(tmp_path / "runs2"),
            "--db", str(tmp_path / "log2.sqlite"), "--min-edge", "1",
            "--bankroll", "10000", "--no-color",
        ])
        explicit = capsys.readouterr().out
        assert _table_body(shorthand) == _table_body(explicit)

    @pytest.mark.parametrize("command", ["scores", "halfpoint", "bets"])
    def test_a_subcommand_with_no_action_shows_its_help(self, command, capsys):
        with pytest.raises(SystemExit):
            main([command])
        assert "usage" in capsys.readouterr().out.lower()

    def test_halfpoint_show_without_a_table(self, tmp_path, capsys):
        assert main(["halfpoint", "show", "--table", str(tmp_path / "none.json")]) == 1
        assert "no half-point table" in capsys.readouterr().err

    def test_halfpoint_build_without_results(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert main(["halfpoint", "build", "--db", str(tmp_path / "log.sqlite")]) == 1
        assert "no historical results" in capsys.readouterr().err

    def test_scores_info_without_results(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert main(["scores", "info"]) == 0
        assert "no results stored" in capsys.readouterr().out

    def test_scores_fetch_without_a_key(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CFBD_API_KEY", raising=False)
        assert main(["scores", "fetch", "--seasons", "2024"]) == 1
        assert "CFBD_API_KEY" in capsys.readouterr().err

    def test_a_built_table_is_used_by_the_next_scan(self, tmp_path, capsys, halfpoint_table):
        from cfb_edge.halfpoint import save_table

        table_path = save_table(halfpoint_table, tmp_path / "hp.json")
        config = tmp_path / "config.toml"
        config.write_text(
            f'[paths]\nhalfpoint_table = "{table_path}"\n'
            f'db_path = "{tmp_path / "log.sqlite"}"\n'
            f'out_dir = "{tmp_path / "runs"}"\n',
            encoding="utf-8",
        )
        assert main([
            "scan", "--config", str(config), "--cache-file", FIXTURE,
            "--no-files", "--no-db", "--min-edge", "1", "--no-color",
        ]) == 0
        out = capsys.readouterr().out
        assert "priced through the half-point table" in out
        assert "Over 51.5" in out
        assert "@53 (pinnacle)" in out

    def test_a_broken_table_is_a_warning_not_a_crash(self, tmp_path, capsys):
        table_path = tmp_path / "hp.json"
        table_path.write_text('{"version": 99}', encoding="utf-8")
        config = tmp_path / "config.toml"
        config.write_text(f'[paths]\nhalfpoint_table = "{table_path}"\n', encoding="utf-8")
        assert main([
            "scan", "--config", str(config), "--cache-file", FIXTURE,
            "--no-files", "--no-db", "--no-color",
        ]) == 0
        assert "half-point table ignored" in capsys.readouterr().out


def _table_body(text: str) -> list[str]:
    """The printed rows, minus the run id and timing lines that always differ."""
    return [
        line for line in text.splitlines()
        if line and not line.startswith(("cfb-edge", "odds:", "logged to", "wrote"))
    ]


class TestNotifyWiring:
    def test_dry_run_prints_the_payload(self, tmp_path, capsys):
        run(tmp_path, "--notify-dry-run", "--no-db")
        out = capsys.readouterr().out
        assert "discord dry run" in out
        assert "dry run; nothing sent" in out

    def test_without_the_flag_nothing_is_announced(self, tmp_path, capsys):
        run(tmp_path)
        assert "discord" not in capsys.readouterr().out

    def test_a_webhook_failure_does_not_fail_the_scan(self, tmp_path, capsys, monkeypatch):
        import cfb_edge.notify as notify

        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/hook")

        def fail(*args, **kwargs):
            raise notify.requests.RequestException("no route")

        monkeypatch.setattr(notify.requests, "post", fail)
        assert run(tmp_path, "--notify") == 0
        assert "could not reach" in capsys.readouterr().err


class TestPropsWiring:
    def test_props_are_off_unless_asked_for(self, tmp_path, capsys, monkeypatch):
        import cfb_edge.api as api

        monkeypatch.setattr(
            api.requests, "get", lambda *a, **k: pytest.fail("should not call the API")
        )
        assert run(tmp_path) == 0
        assert "per-game requests" not in capsys.readouterr().out

    def test_props_outside_the_window_spend_nothing(self, tmp_path, capsys, monkeypatch):
        import cfb_edge.api as api

        monkeypatch.setattr(
            api.requests, "get", lambda *a, **k: pytest.fail("should not call the API")
        )
        # The sample board kicks off more than an hour out.
        assert run(tmp_path, "--props", "--props-window", "1", "--yes") == 0
        assert "no games inside the props window" in capsys.readouterr().out


class TestLineDiffColumn:
    def test_the_table_has_the_column(self, tmp_path, capsys):
        run(tmp_path)
        out = capsys.readouterr().out
        assert "LINE DIFF" in out

    def test_a_moved_number_shows_its_difference(self, tmp_path, capsys, halfpoint_table):
        from cfb_edge.halfpoint import save_table

        table_path = save_table(halfpoint_table, tmp_path / "hp.json")
        config = tmp_path / "config.toml"
        config.write_text(f'[paths]\nhalfpoint_table = "{table_path}"\n', encoding="utf-8")
        assert main([
            "scan", "--config", str(config), "--cache-file", FIXTURE,
            "--no-files", "--no-db", "--min-edge", "1", "--no-color",
        ]) == 0
        line = next(
            row for row in capsys.readouterr().out.splitlines() if "Over 51.5" in row
        )
        # DK's 51.5 is a point and a half below Pinnacle's 53, which helps the Over.
        assert "+1.5" in line

    def test_same_number_rows_show_a_dash(self, tmp_path, capsys):
        run(tmp_path)
        line = next(
            row for row in capsys.readouterr().out.splitlines()
            if "Michigan Wolverines +6.5" in row
        )
        assert "+6.5" in line
        columns = [c for c in line.split("  ") if c.strip()]
        assert "-" in columns

    def test_the_csv_carries_the_difference(self, tmp_path, capsys):
        run(tmp_path)
        capsys.readouterr()
        path = next((tmp_path / "runs").glob("*.csv"))
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        assert "line_diff" in rows[0]
        totals = {r["pick"]: r["line_diff"] for r in rows if r["market"] == "totals"}
        assert float(totals["Over 51.5"]) == 1.5
        assert float(totals["Under 51.5"]) == -1.5

    def test_the_json_carries_the_difference(self, tmp_path, capsys):
        run(tmp_path)
        capsys.readouterr()
        payload = json.loads(next((tmp_path / "runs").glob("*.json")).read_text())
        over = next(b for b in payload["bets"] if b["pick"] == "Over 51.5")
        assert over["line_diff"] == 1.5

    def test_the_flagged_section_shows_it_too(self, tmp_path, capsys):
        run(tmp_path)
        out = capsys.readouterr().out
        flagged = out.split("Different number")[1]
        assert "LINE DIFF" in flagged
        assert "+1.5" in flagged and "-1.5" in flagged
