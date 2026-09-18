"""SQLite log of every run, so line movement and CLV can be reconstructed later."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from cfb_edge.edges import EdgeRow

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    started_at_utc    TEXT NOT NULL,
    fetched_at_utc    TEXT,
    source            TEXT,
    cache_path        TEXT,
    sport             TEXT,
    markets           TEXT,
    min_edge_pct      REAL,
    bankroll          REAL,
    kelly_fraction    REAL,
    n_games           INTEGER,
    n_rows            INTEGER,
    n_bets            INTEGER,
    quota_remaining   TEXT,
    csv_path          TEXT,
    json_path         TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    observed_at_utc   TEXT NOT NULL,
    event_id          TEXT NOT NULL,
    commence_time_utc TEXT NOT NULL,
    home_team         TEXT,
    away_team         TEXT,
    market            TEXT NOT NULL,
    side              TEXT NOT NULL,
    dk_point          REAL,
    dk_price          REAL,
    dk_prob           REAL,
    sharp_source      TEXT,
    sharp_books       TEXT,
    sharp_point       REAL,
    sharp_price       REAL,
    sharp_hold        REAL,
    fair_prob         REAL,
    fair_american     REAL,
    edge_pct          REAL,
    ev_per_100        REAL,
    stake             REAL,
    status            TEXT,
    above_min_edge    INTEGER,
    translated_from   REAL,
    translation_source TEXT,
    note              TEXT,
    UNIQUE (run_id, event_id, market, side, dk_point)
);

-- Bets you actually placed, logged with `cfb-edge bets add`.
CREATE TABLE IF NOT EXISTS bets (
    bet_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    placed_at_utc     TEXT NOT NULL,
    event_id          TEXT NOT NULL,
    commence_time_utc TEXT,
    home_team         TEXT,
    away_team         TEXT,
    market            TEXT NOT NULL,
    side              TEXT NOT NULL,
    point             REAL,
    price             REAL NOT NULL,
    stake             REAL NOT NULL,
    book              TEXT,
    fair_prob         REAL,
    edge_pct          REAL,
    note              TEXT,
    person            TEXT,
    result            TEXT,
    settled_at_utc    TEXT
);

-- One row per alert actually sent, so the same price is never announced twice.
CREATE TABLE IF NOT EXISTS notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at_utc  TEXT NOT NULL,
    channel      TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    market       TEXT NOT NULL,
    side         TEXT NOT NULL,
    line_key     TEXT NOT NULL,
    price        REAL,
    edge_pct     REAL,
    run_id       TEXT,
    UNIQUE (channel, event_id, market, side, line_key, price)
);

CREATE INDEX IF NOT EXISTS idx_bets_event ON bets (event_id, market, side);
CREATE INDEX IF NOT EXISTS idx_bets_person ON bets (person);
CREATE INDEX IF NOT EXISTS idx_obs_event ON observations (event_id, market, side);
CREATE INDEX IF NOT EXISTS idx_obs_kickoff ON observations (commence_time_utc);
CREATE INDEX IF NOT EXISTS idx_obs_run ON observations (run_id);
CREATE INDEX IF NOT EXISTS idx_obs_observed ON observations (observed_at_utc);

-- Last snapshot of each side taken before kickoff: the closing line.
CREATE VIEW IF NOT EXISTS closing_lines AS
SELECT o.*
FROM observations o
JOIN (
    SELECT event_id, market, side, MAX(observed_at_utc) AS last_observed
    FROM observations
    WHERE observed_at_utc <= commence_time_utc
    GROUP BY event_id, market, side
) last
  ON o.event_id = last.event_id
 AND o.market = last.market
 AND o.side = last.side
 AND o.observed_at_utc = last.last_observed;

-- Every observation paired with that side's closing line. A positive
-- clv_prob_delta means the market moved toward the bet after it was logged.
CREATE VIEW IF NOT EXISTS clv AS
SELECT
    o.run_id,
    o.observed_at_utc,
    o.event_id,
    o.commence_time_utc,
    o.away_team || ' @ ' || o.home_team AS matchup,
    o.market,
    o.side,
    o.dk_point,
    o.dk_price,
    o.dk_prob,
    o.edge_pct,
    o.stake,
    o.status,
    c.observed_at_utc AS closing_observed_at_utc,
    c.dk_point        AS closing_dk_point,
    c.dk_price        AS closing_dk_price,
    c.dk_prob         AS closing_dk_prob,
    c.fair_prob       AS closing_fair_prob,
    (c.dk_prob - o.dk_prob) AS clv_prob_delta
FROM observations o
JOIN closing_lines c
  ON c.event_id = o.event_id AND c.market = o.market AND c.side = o.side;
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that later versions introduced, leaving older rows intact."""
    for table, column, decl in (
        ("observations", "translated_from", "REAL"),
        ("observations", "translation_source", "TEXT"),
        ("bets", "person", "TEXT"),
        ("bets", "result", "TEXT"),
        ("bets", "settled_at_utc", "TEXT"),
    ):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _is_bet(row: EdgeRow, min_edge_pct: float) -> bool:
    return bool(row.is_bet and row.edge is not None and row.edge * 100 >= min_edge_pct)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def log_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    started_at: datetime,
    fetched_at: datetime,
    source: str,
    cache_path: str | None,
    sport: str,
    markets: Sequence[str],
    min_edge_pct: float,
    bankroll: float,
    kelly_fraction: float,
    n_games: int,
    rows: Sequence[EdgeRow],
    quota_remaining: str | None = None,
    csv_path: str | None = None,
    json_path: str | None = None,
) -> int:
    """Insert the run header and one observation per evaluated DK line."""
    init_db(conn)
    observed_at = _iso(fetched_at)
    n_bets = sum(1 for r in rows if _is_bet(r, min_edge_pct))
    conn.execute(
        """
        INSERT OR REPLACE INTO runs (
            run_id, started_at_utc, fetched_at_utc, source, cache_path, sport, markets,
            min_edge_pct, bankroll, kelly_fraction, n_games, n_rows, n_bets,
            quota_remaining, csv_path, json_path
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id, _iso(started_at), observed_at, source, cache_path, sport,
            ",".join(markets), min_edge_pct, bankroll, kelly_fraction, n_games,
            len(rows), n_bets, quota_remaining, csv_path, json_path,
        ),
    )
    payload: list[tuple[Any, ...]] = []
    for row in rows:
        payload.append((
            run_id, observed_at, row.event_id, _iso(row.commence_time), row.home_team,
            row.away_team, row.market, row.side, row.dk_point, row.dk_price, row.dk_prob,
            row.sharp_source, row.sharp_books, row.sharp_point, row.sharp_price,
            row.sharp_hold, row.fair_prob, row.fair_american, row.edge_pct, row.ev_per_100,
            row.stake, row.status, int(_is_bet(row, min_edge_pct)), row.translated_from,
            row.translation_source, row.note,
        ))
    conn.executemany(
        """
        INSERT OR REPLACE INTO observations (
            run_id, observed_at_utc, event_id, commence_time_utc, home_team, away_team,
            market, side, dk_point, dk_price, dk_prob, sharp_source, sharp_books,
            sharp_point, sharp_price, sharp_hold, fair_prob, fair_american, edge_pct,
            ev_per_100, stake, status, above_min_edge, translated_from,
            translation_source, note
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        payload,
    )
    conn.commit()
    return len(payload)
