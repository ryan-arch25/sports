"""SQLite log of every run, so line movement and CLV can be reconstructed later."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from cfb_edge.edges import EdgeRow

SCHEMA_VERSION = 8


@dataclass(frozen=True)
class Table:
    """One table, declared once so the migration cannot drift from it."""

    columns: tuple[tuple[str, str], ...]
    constraints: tuple[str, ...] = ()
    indexes: tuple[tuple[str, str], ...] = ()


TABLES: dict[str, Table] = {
    "runs": Table(
        columns=(
            ("run_id", "TEXT PRIMARY KEY"),
            ("started_at_utc", "TEXT NOT NULL"),
            ("fetched_at_utc", "TEXT"),
            ("source", "TEXT"),
            ("cache_path", "TEXT"),
            ("sport", "TEXT"),
            ("markets", "TEXT"),
            ("min_edge_pct", "REAL"),
            ("bankroll", "REAL"),
            ("kelly_fraction", "REAL"),
            ("n_games", "INTEGER"),
            ("n_rows", "INTEGER"),
            ("n_bets", "INTEGER"),
            ("quota_remaining", "TEXT"),
            ("csv_path", "TEXT"),
            ("json_path", "TEXT"),
        ),
    ),
    "observations": Table(
        columns=(
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
            ("run_id", "TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE"),
            ("observed_at_utc", "TEXT NOT NULL"),
            ("event_id", "TEXT NOT NULL"),
            ("commence_time_utc", "TEXT NOT NULL"),
            ("home_team", "TEXT"),
            ("away_team", "TEXT"),
            ("market", "TEXT NOT NULL"),
            ("side", "TEXT NOT NULL"),
            ("dk_point", "REAL"),
            ("dk_price", "REAL"),
            ("dk_prob", "REAL"),
            ("sharp_source", "TEXT"),
            ("sharp_books", "TEXT"),
            ("sharp_point", "REAL"),
            ("sharp_price", "REAL"),
            ("sharp_hold", "REAL"),
            ("fair_prob", "REAL"),
            ("fair_american", "REAL"),
            ("edge_pct", "REAL"),
            ("ev_per_100", "REAL"),
            ("stake", "REAL"),
            ("status", "TEXT"),
            ("above_min_edge", "INTEGER"),
            ("translated_from", "REAL"),
            ("translation_source", "TEXT"),
            ("note", "TEXT"),
        ),
        constraints=("UNIQUE (run_id, event_id, market, side, dk_point)",),
        indexes=(
            ("idx_obs_event", "event_id, market, side"),
            ("idx_obs_kickoff", "commence_time_utc"),
            ("idx_obs_run", "run_id"),
            ("idx_obs_observed", "observed_at_utc"),
        ),
    ),
    "bets": Table(
        columns=(
            ("bet_id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
            ("placed_at_utc", "TEXT NOT NULL"),
            ("event_id", "TEXT NOT NULL"),
            ("commence_time_utc", "TEXT"),
            ("home_team", "TEXT"),
            ("away_team", "TEXT"),
            ("market", "TEXT NOT NULL"),
            ("side", "TEXT NOT NULL"),
            ("point", "REAL"),
            ("price", "REAL NOT NULL"),
            ("stake", "REAL NOT NULL"),
            ("book", "TEXT"),
            ("fair_prob", "REAL"),
            ("edge_pct", "REAL"),
            ("note", "TEXT"),
            ("person", "TEXT"),
            ("result", "TEXT"),
            ("settled_at_utc", "TEXT"),
            # "single" or "parlay". NULL on rows written before parlays existed,
            # which the log reads as a single.
            ("bet_type", "TEXT"),
        ),
        indexes=(
            ("idx_bets_event", "event_id, market, side"),
            ("idx_bets_person", "person"),
        ),
    ),
    # A parlay's legs. Singles keep their one selection on the bets row, so
    # nothing here needed backfilling when parlays arrived.
    "bet_legs": Table(
        columns=(
            ("leg_id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
            ("bet_id", "INTEGER NOT NULL"),
            ("leg_no", "INTEGER NOT NULL"),
            ("event_id", "TEXT NOT NULL"),
            ("commence_time_utc", "TEXT"),
            ("home_team", "TEXT"),
            ("away_team", "TEXT"),
            ("market", "TEXT NOT NULL"),
            ("side", "TEXT NOT NULL"),
            ("point", "REAL"),
            # The leg's own price, which is what lets a pushed leg be divided
            # back out of the combined price the way a sportsbook does it.
            ("price", "REAL"),
            ("result", "TEXT"),
            ("settled_at_utc", "TEXT"),
        ),
        constraints=("UNIQUE (bet_id, leg_no)",),
        indexes=(("idx_legs_bet", "bet_id"),),
    ),
    "line_history": Table(
        columns=(
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
            ("event_id", "TEXT NOT NULL"),
            ("market", "TEXT NOT NULL"),
            ("side", "TEXT NOT NULL"),
            # Logical book name for DraftKings, or the sharp line's own label,
            # which may be "pinnacle", "circa" or "consensus(3)".
            ("book", "TEXT NOT NULL"),
            ("recorded_at_utc", "TEXT NOT NULL"),
            ("point", "REAL"),
            ("price", "REAL"),
            ("run_id", "TEXT"),
        ),
        indexes=(
            ("idx_history_event", "event_id, recorded_at_utc"),
            ("idx_history_line", "event_id, market, side, book, recorded_at_utc"),
        ),
    ),
    "alerts": Table(
        columns=(
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
            ("sent_at_utc", "TEXT NOT NULL"),
            ("channel", "TEXT NOT NULL"),
            # The Eastern date the alert belongs to. Dedupe is per day, so a
            # line that flickers above and below the threshold is announced once.
            ("alert_date_et", "TEXT NOT NULL"),
            ("kind", "TEXT NOT NULL"),  # "edge" or "summary"
            ("event_id", "TEXT NOT NULL"),
            ("market", "TEXT NOT NULL"),
            ("side", "TEXT NOT NULL"),
            ("edge_pct", "REAL"),
            ("run_id", "TEXT"),
        ),
        constraints=(
            "UNIQUE (channel, alert_date_et, kind, event_id, market, side)",
        ),
    ),
    "notifications": Table(
        columns=(
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
            ("sent_at_utc", "TEXT NOT NULL"),
            ("channel", "TEXT NOT NULL"),
            ("event_id", "TEXT NOT NULL"),
            ("market", "TEXT NOT NULL"),
            ("side", "TEXT NOT NULL"),
            ("line_key", "TEXT NOT NULL"),
            ("price", "REAL"),
            ("edge_pct", "REAL"),
            ("run_id", "TEXT"),
        ),
        constraints=("UNIQUE (channel, event_id, market, side, line_key, price)",),
    ),
}

# What the scan writes. Nothing here touches the bet log, so a problem with the
# Log tab's tables can never stop the board from being scored.
SCAN_TABLES = ("runs", "observations")
LOG_TABLES = ("bets", "bet_legs", "notifications")
HISTORY_TABLES = ("line_history",)
ALERT_TABLES = ("alerts",)

# Views are rebuilt every time rather than created-if-missing, so an old
# definition left by an earlier version cannot linger.
VIEWS: dict[str, tuple[tuple[str, ...], str]] = {
    "closing_lines": (
        ("observations",),
        """
        CREATE VIEW closing_lines AS
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
         AND o.observed_at_utc = last.last_observed
        """,
    ),
    "clv": (
        ("observations",),
        """
        CREATE VIEW clv AS
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
          ON c.event_id = o.event_id AND c.market = o.market AND c.side = o.side
        """,
    ),
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_table_sql(name: str, table: Table) -> str:
    body = [f"    {column} {declaration}" for column, declaration in table.columns]
    body.extend(f"    {constraint}" for constraint in table.constraints)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n" + ",\n".join(body) + "\n)"


def _alter_type(declaration: str) -> str:
    """The part of a column declaration ALTER TABLE will accept.

    SQLite refuses to add a NOT NULL column without a default, and cannot add a
    primary key or a foreign key at all, so only the storage type carries over.
    An existing database always has those original columns anyway; this is for
    the ones added later, which are all plain and nullable.
    """
    return declaration.split()[0]


def existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection, tables: Sequence[str] | None = None) -> list[str]:
    """Bring a database up to the current schema, in place.

    Order matters and is the whole point: create tables, then add columns that
    later versions introduced, and only then build indexes and views. An index
    over a column that an ALTER has not added yet is exactly the failure this
    exists to prevent.

    Returns a description of anything it changed, which is empty for a database
    that was already current.
    """
    wanted = list(tables) if tables is not None else list(TABLES)
    changes: list[str] = []

    for name in wanted:
        table = TABLES[name]
        before = existing_columns(conn, name)
        conn.execute(create_table_sql(name, table))
        if not before:
            changes.append(f"created {name}")
        else:
            for column, declaration in table.columns:
                if column not in before:
                    conn.execute(
                        f"ALTER TABLE {name} ADD COLUMN {column} {_alter_type(declaration)}"
                    )
                    changes.append(f"added {name}.{column}")

    for name in wanted:
        for index, columns in TABLES[name].indexes:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {index} ON {name} ({columns})")

    for view, (needs, sql) in VIEWS.items():
        if all(table in wanted for table in needs):
            conn.execute(f"DROP VIEW IF EXISTS {view}")
            conn.execute(sql)

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return changes


def init_db(conn: sqlite3.Connection) -> None:
    """Every table, for anything that touches the bet log."""
    migrate(conn)


def init_scan_db(conn: sqlite3.Connection) -> None:
    """Only what a scan writes, so the log's tables cannot block one."""
    migrate(conn, SCAN_TABLES)


def init_history_db(conn: sqlite3.Connection) -> None:
    migrate(conn, HISTORY_TABLES)


def init_alert_db(conn: sqlite3.Connection) -> None:
    migrate(conn, ALERT_TABLES)


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
    init_scan_db(conn)
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


# -- line history ---------------------------------------------------------
#
# One row per *change*. A scan that finds every number where it left it writes
# nothing, which is what keeps this table small enough to query on every page
# load: a line that never moves costs one row for the whole week.

HistoryKey = tuple[str, str, str, str]  # event_id, market, side, book


@dataclass(frozen=True)
class LineSnapshot:
    """What a book was showing for one side when a scan looked."""

    event_id: str
    market: str
    side: str
    book: str
    point: float | None
    price: float | None

    @property
    def key(self) -> HistoryKey:
        return (self.event_id, self.market, self.side, self.book)

    @property
    def value(self) -> tuple[float | None, float | None]:
        return (self.point, self.price)


def _same_value(a: tuple[float | None, float | None], b: tuple[float | None, float | None]) -> bool:
    """Equal to the precision a sportsbook actually posts.

    Prices and points both arrive as floats, and a book that re-sends the same
    number can send it as 6.5 or 6.50; an exact float comparison would call
    that a move and fill the table with rows that say nothing.
    """
    for left, right in zip(a, b):
        if left is None and right is None:
            continue
        if left is None or right is None:
            return False
        if abs(float(left) - float(right)) >= 1e-6:
            return False
    return True


def latest_lines(conn: sqlite3.Connection) -> dict[HistoryKey, tuple[float | None, float | None]]:
    """The most recent recorded value for every line, to compare a scan against."""
    rows = conn.execute(
        """
        SELECT h.event_id, h.market, h.side, h.book, h.point, h.price
        FROM line_history h
        JOIN (
            SELECT event_id, market, side, book, MAX(id) AS last_id
            FROM line_history
            GROUP BY event_id, market, side, book
        ) last ON last.last_id = h.id
        """
    ).fetchall()
    return {(r[0], r[1], r[2], r[3]): (r[4], r[5]) for r in rows}


def record_lines(
    conn: sqlite3.Connection,
    snapshots: Sequence[LineSnapshot],
    recorded_at: datetime,
    run_id: str | None = None,
) -> int:
    """Append the lines that moved. Returns how many rows were written."""
    init_history_db(conn)
    known = latest_lines(conn)
    stamp = _iso(recorded_at)
    payload = []
    seen: set[HistoryKey] = set()
    for snapshot in snapshots:
        if snapshot.key in seen:
            continue  # one row per line per scan, whatever the caller passed
        seen.add(snapshot.key)
        previous = known.get(snapshot.key)
        if previous is not None and _same_value(previous, snapshot.value):
            continue
        payload.append((
            snapshot.event_id, snapshot.market, snapshot.side, snapshot.book,
            stamp, snapshot.point, snapshot.price, run_id,
        ))
    if payload:
        conn.executemany(
            """
            INSERT INTO line_history (
                event_id, market, side, book, recorded_at_utc, point, price, run_id
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            payload,
        )
        conn.commit()
    return len(payload)


def baseline_lines(
    conn: sqlite3.Connection, cutoff: datetime
) -> dict[HistoryKey, tuple[float | None, float | None, str]]:
    """What each line was showing as of `cutoff`, for the movement arrows.

    A line first seen *after* the cutoff has no value from before it, so its
    oldest recorded row stands in: that is still the number the board was
    showing when the group last looked, which is what the arrow is about.
    """
    init_history_db(conn)
    at_cutoff = conn.execute(
        """
        SELECT h.event_id, h.market, h.side, h.book, h.point, h.price, h.recorded_at_utc
        FROM line_history h
        JOIN (
            SELECT event_id, market, side, book, MAX(id) AS last_id
            FROM line_history
            WHERE recorded_at_utc <= ?
            GROUP BY event_id, market, side, book
        ) last ON last.last_id = h.id
        """,
        (_iso(cutoff),),
    ).fetchall()
    baseline = {(r[0], r[1], r[2], r[3]): (r[4], r[5], r[6]) for r in at_cutoff}

    oldest = conn.execute(
        """
        SELECT h.event_id, h.market, h.side, h.book, h.point, h.price, h.recorded_at_utc
        FROM line_history h
        JOIN (
            SELECT event_id, market, side, book, MIN(id) AS first_id
            FROM line_history
            WHERE recorded_at_utc > ?
            GROUP BY event_id, market, side, book
        ) first ON first.first_id = h.id
        """,
        (_iso(cutoff),),
    ).fetchall()
    for row in oldest:
        key = (row[0], row[1], row[2], row[3])
        baseline.setdefault(key, (row[4], row[5], row[6]))
    return baseline


def line_history(
    conn: sqlite3.Connection,
    event_id: str,
    since: datetime | None = None,
    markets: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Every recorded change for one game, newest first."""
    init_history_db(conn)
    sql = [
        "SELECT recorded_at_utc, market, side, book, point, price, id",
        "FROM line_history WHERE event_id = ?",
    ]
    params: list[Any] = [event_id]
    if since is not None:
        sql.append("AND recorded_at_utc >= ?")
        params.append(_iso(since))
    if markets:
        sql.append(f"AND market IN ({','.join('?' for _ in markets)})")
        params.extend(markets)
    sql.append("ORDER BY recorded_at_utc DESC, id DESC")
    rows = conn.execute(" ".join(sql), params).fetchall()
    return [
        {
            "recorded_at_utc": r[0], "market": r[1], "side": r[2],
            "book": r[3], "point": r[4], "price": r[5], "id": r[6],
        }
        for r in rows
    ]


def first_seen_ids(conn: sqlite3.Connection, event_id: str) -> set[int]:
    """The row that opened each line, which is where it started, not a move."""
    init_history_db(conn)
    rows = conn.execute(
        "SELECT MIN(id) FROM line_history WHERE event_id = ? "
        "GROUP BY market, side, book",
        (event_id,),
    ).fetchall()
    return {r[0] for r in rows if r[0] is not None}


def prune_history(conn: sqlite3.Connection, before: datetime) -> int:
    """Drop old rows, but never a line's most recent one.

    A number that has not moved in a month is represented by a single old row.
    Deleting it would not just lose the history: the next scan would see an
    unknown line, write it again, and report a move that never happened.
    """
    init_history_db(conn)
    cursor = conn.execute(
        """
        DELETE FROM line_history
        WHERE recorded_at_utc < ?
          AND id NOT IN (
              SELECT MAX(id) FROM line_history GROUP BY event_id, market, side, book
          )
        """,
        (_iso(before),),
    )
    conn.commit()
    return cursor.rowcount
