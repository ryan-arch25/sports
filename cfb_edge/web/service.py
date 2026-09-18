"""Runs the scan on a schedule and holds the latest result in memory.

The dashboard never scans on request: a background task refreshes every
`refresh_minutes` and every page load reads whatever the last successful scan
produced. A failed refresh leaves the previous results in place and is reported
alongside them, so a dead API key shows up as a stale banner rather than an
empty page.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from cfb_edge.config import Config
from cfb_edge.edges import TRANSLATED, EdgeRow
from cfb_edge.models import MARKETS, market_label
from cfb_edge.oddsmath import format_american
from cfb_edge.report import kickoff_et
from cfb_edge.scan import ScanOptions, ScanResult, persist, run_scan

log = logging.getLogger("cfb_edge.web")

DEFAULT_REFRESH_MINUTES = 30.0
# A manual refresh inside this window is ignored. The odds cache means a
# refresh is usually free, but this stops the button being held down.
MANUAL_REFRESH_COOLDOWN_SECONDS = 30.0


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def serialize_row(row: EdgeRow) -> dict[str, Any]:
    """One table row, with everything the page needs to render and filter."""
    return {
        "event_id": row.event_id,
        "matchup": row.matchup,
        "home_team": row.home_team,
        "away_team": row.away_team,
        "kickoff_et": kickoff_et(row.commence_time),
        "kickoff_utc": _iso(row.commence_time),
        "market": row.market,
        "market_label": row.market_label,
        "pick": row.pick,
        "side": row.side,
        "point": row.dk_point,
        "dk_price": format_american(row.dk_price),
        "sharp_price": format_american(row.sharp_price),
        "sharp_source": row.sharp_source,
        "sharp_point": row.sharp_point,
        "fair_pct": None if row.fair_prob is None else round(row.fair_prob * 100, 1),
        "dk_pct": None if row.dk_prob is None else round(row.dk_prob * 100, 1),
        "edge_pct": None if row.edge_pct is None else round(row.edge_pct, 2),
        "ev_per_100": None if row.ev_per_100 is None else round(row.ev_per_100, 2),
        "stake": None if row.stake is None else round(row.stake, 2),
        "status": row.status,
        "translated": row.status == TRANSLATED,
        "note": row.note,
    }


@dataclass
class DashboardState:
    """What the page renders. Always safe to serve, even before the first scan."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    markets: list[dict[str, str]] = field(default_factory=list)
    updated_at_utc: str | None = None
    updated_at_et: str | None = None
    odds_fetched_at_utc: str | None = None
    odds_fetched_at_et: str | None = None
    odds_source: str | None = None
    games: int = 0
    flagged_different_number: int = 0
    quota_remaining: str | None = None
    bankroll: float = 0.0
    kelly_fraction: float = 0.25
    default_min_edge: float = 1.0
    refresh_minutes: float = DEFAULT_REFRESH_MINUTES
    next_refresh_utc: str | None = None
    last_attempt_utc: str | None = None
    error: str | None = None
    scanning: bool = False

    @property
    def ready(self) -> bool:
        return self.updated_at_utc is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "meta": {
                "markets": self.markets,
                "updated_at_utc": self.updated_at_utc,
                "updated_at_et": self.updated_at_et,
                "odds_fetched_at_utc": self.odds_fetched_at_utc,
                "odds_fetched_at_et": self.odds_fetched_at_et,
                "odds_source": self.odds_source,
                "games": self.games,
                "flagged_different_number": self.flagged_different_number,
                "quota_remaining": self.quota_remaining,
                "bankroll": self.bankroll,
                "kelly_fraction": self.kelly_fraction,
                "default_min_edge": self.default_min_edge,
                "refresh_minutes": self.refresh_minutes,
                "next_refresh_utc": self.next_refresh_utc,
                "last_attempt_utc": self.last_attempt_utc,
                "error": self.error,
                "ready": self.ready,
                "scanning": self.scanning,
            },
        }


class Dashboard:
    """Owns the scan schedule and the latest results."""

    def __init__(
        self,
        cfg: Config,
        options: ScanOptions | None = None,
        refresh_minutes: float = DEFAULT_REFRESH_MINUTES,
        display_min_edge: float | None = None,
        floor_edge: float = 0.0,
        write_outputs: bool = True,
    ):
        self.cfg = cfg
        self.refresh_minutes = max(float(refresh_minutes), 1.0)
        self.display_min_edge = cfg.min_edge if display_min_edge is None else display_min_edge
        self.floor_edge = floor_edge
        # The scan itself keeps every non-negative edge so the page can filter
        # downward; `display_min_edge` is only where the filter starts.
        self.options = options or ScanOptions(
            markets=tuple(cfg.markets),
            min_edge=floor_edge,
            write_files=write_outputs,
            write_db=write_outputs,
        )
        self.state = DashboardState(
            bankroll=cfg.bankroll,
            kelly_fraction=cfg.kelly_fraction,
            default_min_edge=self.display_min_edge,
            refresh_minutes=self.refresh_minutes,
        )
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._last_manual_refresh = 0.0

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Kick off the first scan and the repeating schedule."""
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="cfb-edge-scan-loop")

    async def stop(self) -> None:
        self._stopping.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                pass

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            await self.refresh()
            self._set_next_refresh()
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.refresh_minutes * 60.0
                )
            except asyncio.TimeoutError:
                continue

    def _set_next_refresh(self) -> None:
        self.state.next_refresh_utc = _iso(
            datetime.now(timezone.utc) + timedelta(minutes=self.refresh_minutes)
        )

    # -- scanning ---------------------------------------------------------

    async def refresh(self) -> bool:
        """Run one scan. Returns True when it produced fresh results."""
        if self._lock.locked():
            return False  # a scan is already running; let it finish
        async with self._lock:
            self.state.scanning = True
            try:
                return await asyncio.to_thread(self._scan_once)
            finally:
                self.state.scanning = False

    async def manual_refresh(self, now: float | None = None) -> tuple[bool, str]:
        """The refresh button. Cheap when the odds cache is still warm."""
        now = time.monotonic() if now is None else now
        if self._lock.locked():
            return False, "a scan is already running"
        if now - self._last_manual_refresh < MANUAL_REFRESH_COOLDOWN_SECONDS:
            wait = int(MANUAL_REFRESH_COOLDOWN_SECONDS - (now - self._last_manual_refresh))
            return False, f"just refreshed; try again in {wait}s"
        self._last_manual_refresh = now
        ok = await self.refresh()
        return ok, "updated" if ok else (self.state.error or "refresh failed")

    def _scan_once(self) -> bool:
        """Blocking: run the pipeline and fold the result into the state."""
        self.state.last_attempt_utc = _iso(datetime.now(timezone.utc))
        try:
            result = run_scan(self.cfg, self.options)
            if self.options.write_files or self.options.write_db:
                persist(self.cfg, result, self.options)
        except Exception as exc:  # noqa: BLE001 - any failure must not kill the loop
            self.state.error = f"{type(exc).__name__}: {exc}"
            log.warning("scan failed: %s", self.state.error)
            return False
        self.apply(result)
        return True

    def apply(self, result: ScanResult) -> None:
        """Replace the served state with a finished scan."""
        now = datetime.now(timezone.utc)
        rows = [serialize_row(row) for row in result.bets]
        self.state.rows = rows
        self.state.markets = markets_present(result.markets, result.bets)
        self.state.updated_at_utc = _iso(now)
        self.state.updated_at_et = kickoff_et(now)
        self.state.odds_fetched_at_utc = _iso(result.snapshot.fetched_at)
        self.state.odds_fetched_at_et = kickoff_et(result.snapshot.fetched_at)
        self.state.odds_source = result.source
        self.state.games = len(result.games)
        self.state.flagged_different_number = result.status_counts.get("different_number", 0)
        self.state.quota_remaining = result.snapshot.quota.get("remaining")
        self.state.bankroll = self.cfg.bankroll
        self.state.kelly_fraction = self.cfg.kelly_fraction
        self.state.error = None


def markets_present(scanned: Sequence[str], rows: Sequence[EdgeRow]) -> list[dict[str, str]]:
    """Filter options for the page, in the order the markets were scanned."""
    with_rows = {row.market for row in rows}
    ordered = [m for m in scanned if m in with_rows]
    ordered += [m for m in MARKETS if m in with_rows and m not in ordered]
    return [{"key": market, "label": market_label(market)} for market in ordered]
