"""Tests for the schema migration that upgrades a database in place."""

from __future__ import annotations

import sqlite3

import pytest

from cfb_edge.store import (
    LOG_TABLES,
    SCAN_TABLES,
    SCHEMA_VERSION,
    TABLES,
    connect,
    create_table_sql,
    existing_columns,
    init_db,
    init_scan_db,
    migrate,
)

# The schema as it stood before the Log tab: `bets` has no person, result or
# settled_at, which is the database sitting on the deployed volume.
BEFORE_THE_LOG_TAB = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, started_at_utc TEXT NOT NULL, fetched_at_utc TEXT,
    source TEXT, sport TEXT, markets TEXT, n_games INTEGER);
CREATE TABLE observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, observed_at_utc TEXT NOT NULL,
    event_id TEXT NOT NULL, commence_time_utc TEXT NOT NULL, home_team TEXT, away_team TEXT,
    market TEXT NOT NULL, side TEXT NOT NULL, dk_point REAL, dk_price REAL,
    sharp_price REAL, sharp_source TEXT, fair_prob REAL, edge_pct REAL, status TEXT);
CREATE TABLE bets (
    bet_id INTEGER PRIMARY KEY AUTOINCREMENT, placed_at_utc TEXT NOT NULL,
    event_id TEXT NOT NULL, market TEXT NOT NULL, side TEXT NOT NULL,
    point REAL, price REAL NOT NULL, stake REAL NOT NULL);
INSERT INTO runs (run_id, started_at_utc) VALUES ('old-run', '2026-09-01T00:00:00Z');
INSERT INTO observations (run_id, observed_at_utc, event_id, commence_time_utc, market, side)
    VALUES ('old-run', '2026-09-01T00:00:00Z', 'g1', '2026-09-02T00:00:00Z', 'spreads', 'Team');
INSERT INTO bets (placed_at_utc, event_id, market, side, price, stake)
    VALUES ('2026-09-01T00:00:00Z', 'g1', 'spreads', 'Team', -110, 50);
"""


@pytest.fixture
def old_db(tmp_path):
    path = tmp_path / "old.sqlite"
    raw = sqlite3.connect(path)
    raw.executescript(BEFORE_THE_LOG_TAB)
    raw.commit()
    raw.close()
    conn = connect(path)
    yield conn
    conn.close()


class TestUpgradingInPlace:
    def test_the_old_database_reproduces_the_failure_without_migrating(self, old_db):
        """The index is what raised: IF NOT EXISTS cannot save an absent column."""
        with pytest.raises(sqlite3.OperationalError, match="no such column: person"):
            old_db.execute("CREATE INDEX IF NOT EXISTS idx_bets_person ON bets (person)")

    def test_migrating_adds_every_missing_column(self, old_db):
        changes = migrate(old_db)
        assert "added bets.person" in changes
        assert "added bets.result" in changes
        assert "added bets.settled_at_utc" in changes
        assert "person" in existing_columns(old_db, "bets")

    def test_it_keeps_the_rows_that_were_already_there(self, old_db):
        migrate(old_db)
        bet = old_db.execute("SELECT stake, person FROM bets").fetchone()
        assert bet["stake"] == 50.0
        assert bet["person"] is None
        assert old_db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert old_db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1

    def test_it_creates_tables_that_are_missing_entirely(self, old_db):
        changes = migrate(old_db)
        assert "created notifications" in changes
        assert existing_columns(old_db, "notifications")

    def test_the_indexes_come_after_the_columns(self, old_db):
        migrate(old_db)
        indexes = {row[0] for row in old_db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "idx_bets_person" in indexes

    def test_running_it_again_changes_nothing(self, old_db):
        migrate(old_db)
        assert migrate(old_db) == []

    def test_a_fresh_database_reports_its_tables_as_created(self, tmp_path):
        conn = connect(tmp_path / "new.sqlite")
        try:
            changes = migrate(conn)
            assert sorted(changes) == sorted(f"created {name}" for name in TABLES)
        finally:
            conn.close()

    def test_it_stamps_the_version(self, old_db):
        migrate(old_db)
        assert old_db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    def test_views_are_rebuilt_rather_than_left_stale(self, old_db):
        old_db.execute("CREATE VIEW clv AS SELECT 1 AS wrong")
        migrate(old_db)
        columns = {row[1] for row in old_db.execute("PRAGMA table_info(clv)")}
        assert "wrong" not in columns
        assert "clv_prob_delta" in columns


class TestScanIsolation:
    def test_a_scan_migration_leaves_the_log_tables_alone(self, old_db):
        migrate(old_db, SCAN_TABLES)
        assert "person" not in existing_columns(old_db, "bets")
        assert not existing_columns(old_db, "notifications")

    def test_a_scan_migration_still_fixes_what_a_scan_writes(self, old_db):
        changes = migrate(old_db, SCAN_TABLES)
        assert "added observations.translation_source" in changes
        assert "added runs.bankroll" in changes

    def test_logging_a_run_works_on_a_database_from_before_the_log_tab(
        self, old_db, sample_games, cfg
    ):
        """The failure that broke every scan on the deployment."""
        from datetime import datetime, timezone

        from cfb_edge.edges import evaluate_games
        from cfb_edge.store import log_run

        rows = evaluate_games(sample_games, cfg, ("h2h",))
        now = datetime.now(timezone.utc)
        written = log_run(
            old_db, run_id="r1", started_at=now, fetched_at=now, source="cache",
            cache_path=None, sport="americanfootball_ncaaf", markets=("h2h",),
            min_edge_pct=1.0, bankroll=1000.0, kelly_fraction=0.25,
            n_games=len(sample_games), rows=rows,
        )
        assert written == len(rows)
        assert "person" not in existing_columns(old_db, "bets")  # still untouched

    def test_the_log_tables_are_named_apart_from_the_scan_ones(self):
        assert set(SCAN_TABLES).isdisjoint(LOG_TABLES)
        assert set(SCAN_TABLES) | set(LOG_TABLES) == set(TABLES)


class TestDeclaration:
    def test_every_table_builds_valid_sql(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "x.sqlite")
        for name, table in TABLES.items():
            conn.execute(create_table_sql(name, table))
        conn.close()

    def test_a_column_added_later_is_nullable_so_alter_accepts_it(self):
        """ALTER TABLE ADD COLUMN cannot take NOT NULL without a default."""
        for name in ("person", "result", "settled_at_utc"):
            declaration = dict(TABLES["bets"].columns)[name]
            assert "NOT NULL" not in declaration

    def test_init_helpers_agree_with_migrate(self, tmp_path):
        full = connect(tmp_path / "full.sqlite")
        scan = connect(tmp_path / "scan.sqlite")
        try:
            init_db(full)
            init_scan_db(scan)
            assert existing_columns(full, "bets")
            assert not existing_columns(scan, "bets")
        finally:
            full.close()
            scan.close()
