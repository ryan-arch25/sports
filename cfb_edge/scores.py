"""Historical results, used to build the half-point value table.

Scores come from the CollegeFootballData API (a free key, CFBD_API_KEY). Final
scores alone are not enough: the value of a half point depends on where the
market set the line, so closing spreads and totals are pulled alongside them
and stored in the same row.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Sequence

import requests

from cfb_edge.config import Config

CFBD_BASE = "https://api.collegefootballdata.com"
SEASON_TYPES = ("regular", "postseason")

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id        INTEGER PRIMARY KEY,
    season         INTEGER NOT NULL,
    week           INTEGER,
    season_type    TEXT,
    start_date     TEXT,
    home_team      TEXT NOT NULL,
    away_team      TEXT NOT NULL,
    home_score     INTEGER NOT NULL,
    away_score     INTEGER NOT NULL,
    neutral_site   INTEGER DEFAULT 0,
    closing_spread REAL,
    closing_total  REAL,
    line_provider  TEXT,
    fetched_at_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_games_season ON games (season);
CREATE INDEX IF NOT EXISTS idx_games_spread ON games (closing_spread);
CREATE INDEX IF NOT EXISTS idx_games_total ON games (closing_total);
"""


class ScoresError(RuntimeError):
    pass


@dataclass
class HistoricalGame:
    game_id: int
    season: int
    week: int | None
    season_type: str
    start_date: str | None
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    neutral_site: bool = False
    closing_spread: float | None = None  # home team spread: -7 means home favored by 7
    closing_total: float | None = None
    line_provider: str | None = None

    @property
    def home_margin(self) -> int:
        return self.home_score - self.away_score

    @property
    def total_points(self) -> int:
        return self.home_score + self.away_score

    @property
    def favorite_margin(self) -> int | None:
        """Margin from the favorite's point of view. None for a pick'em."""
        if self.closing_spread is None or self.closing_spread == 0:
            return None
        return self.home_margin if self.closing_spread < 0 else -self.home_margin

    @property
    def favorite_line(self) -> float | None:
        """Points the favorite laid, always >= 0."""
        if self.closing_spread is None:
            return None
        return abs(self.closing_spread)


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _first(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """CFBD has shipped both snake_case and camelCase; accept either."""
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return default


def parse_game(raw: dict[str, Any]) -> HistoricalGame | None:
    home_score = _first(raw, "homePoints", "home_points", "home_score")
    away_score = _first(raw, "awayPoints", "away_points", "away_score")
    game_id = _first(raw, "id", "game_id")
    home_team = _first(raw, "homeTeam", "home_team")
    away_team = _first(raw, "awayTeam", "away_team")
    season = _first(raw, "season", "year")
    if None in (home_score, away_score, game_id, home_team, away_team, season):
        return None
    try:
        return HistoricalGame(
            game_id=int(game_id),
            season=int(season),
            week=_first(raw, "week"),
            season_type=str(_first(raw, "seasonType", "season_type", default="regular")),
            start_date=_first(raw, "startDate", "start_date"),
            home_team=str(home_team),
            away_team=str(away_team),
            home_score=int(home_score),
            away_score=int(away_score),
            neutral_site=bool(_first(raw, "neutralSite", "neutral_site", default=False)),
        )
    except (TypeError, ValueError):
        return None


def extract_line(raw: dict[str, Any], provider_priority: Sequence[str]) -> tuple[float | None, float | None, str | None]:
    """Pick a closing spread and total out of a CFBD /lines record."""
    lines = raw.get("lines") or []
    by_provider: dict[str, dict[str, Any]] = {}
    for line in lines:
        provider = str(_first(line, "provider", default="") or "")
        if provider:
            by_provider[provider.lower()] = line

    for provider in provider_priority:
        line = by_provider.get(provider.lower())
        if line is None:
            continue
        spread = _first(line, "spread")
        total = _first(line, "overUnder", "over_under")
        if spread is not None or total is not None:
            return (
                None if spread is None else float(spread),
                None if total is None else float(total),
                provider,
            )

    spreads = [float(_first(l, "spread")) for l in lines if _first(l, "spread") is not None]
    totals = [
        float(_first(l, "overUnder", "over_under"))
        for l in lines
        if _first(l, "overUnder", "over_under") is not None
    ]
    if not spreads and not totals:
        return None, None, None
    return (
        median(spreads) if spreads else None,
        median(totals) if totals else None,
        f"median({len(lines)})",
    )


class CfbdClient:
    """Minimal CollegeFootballData client (only what the table needs)."""

    def __init__(self, api_key: str, timeout: float = 60.0, base_url: str = CFBD_BASE):
        if not api_key:
            raise ScoresError(
                "CFBD_API_KEY is not set. Get a free key at "
                "https://collegefootballdata.com/key and put it in .env as "
                "CFBD_API_KEY=..."
            )
        self.api_key = api_key
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            response = requests.get(
                f"{self.base_url}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ScoresError(f"request to CollegeFootballData failed: {exc}") from exc
        if response.status_code == 401:
            raise ScoresError("401 from CollegeFootballData: the API key was rejected.")
        if not response.ok:
            raise ScoresError(
                f"{response.status_code} from CollegeFootballData: {response.text[:200]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ScoresError(f"CollegeFootballData returned invalid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise ScoresError(f"expected a list, got {type(data).__name__}")
        return data

    def games(self, season: int, season_type: str, division: str = "fbs") -> list[dict[str, Any]]:
        return self._get(
            "/games",
            {"year": season, "seasonType": season_type, "division": division},
        )

    def lines(self, season: int, season_type: str) -> list[dict[str, Any]]:
        return self._get("/lines", {"year": season, "seasonType": season_type})


def fetch_season(
    client: CfbdClient,
    season: int,
    division: str,
    provider_priority: Sequence[str],
) -> list[HistoricalGame]:
    """Every completed game of a season, joined to its closing line."""
    games: dict[int, HistoricalGame] = {}
    for season_type in SEASON_TYPES:
        for raw in client.games(season, season_type, division):
            game = parse_game(raw)
            if game is not None:
                games[game.game_id] = game
        for raw in client.lines(season, season_type):
            game_id = _first(raw, "id", "gameId", "game_id")
            if game_id is None or int(game_id) not in games:
                continue
            spread, total, provider = extract_line(raw, provider_priority)
            game = games[int(game_id)]
            game.closing_spread = spread
            game.closing_total = total
            game.line_provider = provider
    return list(games.values())


def store_games(conn: sqlite3.Connection, games: Iterable[HistoricalGame]) -> int:
    init_db(conn)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = [
        (
            g.game_id, g.season, g.week, g.season_type, g.start_date, g.home_team,
            g.away_team, g.home_score, g.away_score, int(g.neutral_site),
            g.closing_spread, g.closing_total, g.line_provider, now,
        )
        for g in games
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO games (
            game_id, season, week, season_type, start_date, home_team, away_team,
            home_score, away_score, neutral_site, closing_spread, closing_total,
            line_provider, fetched_at_utc
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def load_games(conn: sqlite3.Connection, min_season: int | None = None) -> list[HistoricalGame]:
    init_db(conn)
    sql = "SELECT * FROM games"
    params: tuple = ()
    if min_season is not None:
        sql += " WHERE season >= ?"
        params = (min_season,)
    rows = conn.execute(sql, params).fetchall()
    return [
        HistoricalGame(
            game_id=row["game_id"], season=row["season"], week=row["week"],
            season_type=row["season_type"], start_date=row["start_date"],
            home_team=row["home_team"], away_team=row["away_team"],
            home_score=row["home_score"], away_score=row["away_score"],
            neutral_site=bool(row["neutral_site"]), closing_spread=row["closing_spread"],
            closing_total=row["closing_total"], line_provider=row["line_provider"],
        )
        for row in rows
    ]


def stored_seasons(conn: sqlite3.Connection) -> dict[int, int]:
    init_db(conn)
    return {
        row["season"]: row["n"]
        for row in conn.execute(
            "SELECT season, COUNT(*) AS n FROM games GROUP BY season ORDER BY season"
        )
    }


def parse_seasons(text: str | None, default_count: int = 10) -> list[int]:
    """'2015-2024', '2023', '2019,2021' or None for the last N completed seasons."""
    if not text:
        end = datetime.now(timezone.utc).year - 1
        return list(range(end - default_count + 1, end + 1))
    seasons: list[int] = []
    for part in str(text).split(","):
        chunk = part.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            try:
                lo, hi = int(start), int(end)
            except ValueError as exc:
                raise ScoresError(f"could not read season range {chunk!r}") from exc
            if hi < lo:
                lo, hi = hi, lo
            seasons.extend(range(lo, hi + 1))
        else:
            try:
                seasons.append(int(chunk))
            except ValueError as exc:
                raise ScoresError(f"could not read season {chunk!r}") from exc
    return sorted(set(seasons))


def cmd_scores_fetch(cfg: Config, args: argparse.Namespace) -> int:
    seasons = parse_seasons(args.seasons)
    conn = connect(cfg.scores_db_path)
    try:
        existing = stored_seasons(conn)
        todo = [s for s in seasons if args.refresh or s not in existing]
        skipped = [s for s in seasons if s not in todo]
        if skipped:
            print(f"already stored: {', '.join(str(s) for s in skipped)} (use --refresh to redo)")
        if not todo:
            print("nothing to fetch")
            return 0

        client = CfbdClient(os.environ.get("CFBD_API_KEY", ""))
        total = 0
        for season in todo:
            games = fetch_season(client, season, args.division, cfg.line_providers)
            stored = store_games(conn, games)
            with_lines = sum(1 for g in games if g.closing_spread is not None)
            total += stored
            print(f"{season}: stored {stored} games ({with_lines} with a closing line)")
        print(f"\n{total} games written to {cfg.scores_db_path}")
        print("next: cfb-edge halfpoint build")
        return 0
    except ScoresError as exc:
        print(f"scores error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_scores_info(cfg: Config, args: argparse.Namespace) -> int:
    conn = connect(cfg.scores_db_path)
    try:
        seasons = stored_seasons(conn)
        if not seasons:
            print(f"no results stored in {cfg.scores_db_path}")
            print("run: cfb-edge scores fetch --seasons 2015-2024")
            return 0
        games = load_games(conn)
        with_spread = sum(1 for g in games if g.closing_spread is not None)
        with_total = sum(1 for g in games if g.closing_total is not None)
        print(f"{cfg.scores_db_path}: {len(games)} games across {len(seasons)} seasons")
        print(f"  seasons: {min(seasons)}-{max(seasons)}")
        print(f"  with a closing spread: {with_spread} ({with_spread / len(games):.0%})")
        print(f"  with a closing total:  {with_total} ({with_total / len(games):.0%})")
        for season, count in seasons.items():
            print(f"    {season}: {count}")
        return 0
    finally:
        conn.close()
