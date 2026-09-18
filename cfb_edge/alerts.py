"""Discord alerts from the dashboard: new edges, and a summary each morning.

This is a different job from `notify.py`, which is the CLI's `--notify` flag.
That one announces a line/price pair once and is driven by a person running a
command. This one runs unattended behind the scan loop, so its rules are about
not becoming noise:

* an edge is announced when it *appears* -- it was not there on the previous
  scan -- rather than every time a scan finds it;
* and never twice on the same Eastern day, whatever it does in between. A line
  that crosses the threshold, falls back under it and crosses again has not
  told anyone anything new, and a group chat that says so every half hour is a
  group chat people mute.

Nothing is sent when DISCORD_WEBHOOK_URL is unset, and no failure here is
allowed to reach the board.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

from cfb_edge.config import Config
from cfb_edge.edges import EdgeRow
from cfb_edge.oddsmath import format_american
from cfb_edge.report import ET, kickoff_et
from cfb_edge.store import connect, init_alert_db

log = logging.getLogger("cfb_edge.alerts")

CHANNEL = "discord-dashboard"
EDGE = "edge"
SUMMARY = "summary"
SUMMARY_TOP_N = 5
# Discord rejects a message body over 2000 characters, so a busy Saturday is
# split rather than truncated: an alert nobody receives is worse than two.
MAX_CONTENT = 1900


class AlertError(RuntimeError):
    pass


@dataclass
class AlertOutcome:
    """What one pass did, for the logs and for the tests."""

    announced: list[EdgeRow] = field(default_factory=list)
    summary_sent: bool = False
    messages: list[str] = field(default_factory=list)
    reason: str | None = None

    @property
    def sent_anything(self) -> bool:
        return bool(self.messages)


# -- selection ------------------------------------------------------------


def line_id(row: EdgeRow) -> tuple[str, str, str]:
    """What counts as "the same line" for dedupe: the side, not the number.

    Deliberately ignores the point and the price. DraftKings moving 6.5 to 7
    does not make this a new thing to tell people about; it is the same side of
    the same game, and the morning's message already named it.
    """
    return (row.event_id, row.market, row.side)


def qualifying(rows: Iterable[EdgeRow], min_edge_pct: float) -> list[EdgeRow]:
    keep = [
        r for r in rows
        if r.is_bet and r.edge_pct is not None and r.edge_pct >= min_edge_pct
    ]
    keep.sort(key=lambda r: (-(r.edge_pct or 0.0), r.commence_time, r.matchup))
    return keep


def et_date(moment: datetime) -> str:
    return moment.astimezone(ET).strftime("%Y-%m-%d")


def already_alerted(conn: sqlite3.Connection, day: str, kind: str) -> set[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT event_id, market, side FROM alerts "
        "WHERE channel = ? AND alert_date_et = ? AND kind = ?",
        (CHANNEL, day, kind),
    ).fetchall()
    return {(r[0], r[1], r[2]) for r in rows}


def record_alerts(
    conn: sqlite3.Connection,
    day: str,
    kind: str,
    keys: Sequence[tuple[str, str, str]],
    now: datetime,
    edges: Sequence[float | None] | None = None,
    run_id: str | None = None,
) -> None:
    stamp = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    edges = list(edges or [None] * len(keys))
    conn.executemany(
        """
        INSERT OR IGNORE INTO alerts (
            sent_at_utc, channel, alert_date_et, kind, event_id, market, side,
            edge_pct, run_id
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        [
            (stamp, CHANNEL, day, kind, key[0], key[1], key[2], edge, run_id)
            for key, edge in zip(keys, edges)
        ],
    )
    conn.commit()


# -- message text ---------------------------------------------------------


def format_line(row: EdgeRow) -> str:
    """One bet, one line. Everything needed to find it in the DraftKings app."""
    sharp = format_american(row.sharp_price) if row.sharp_price is not None else "n/a"
    parts = [
        f"**{row.pick}** ({row.market_label})",
        f"{row.matchup}",
        f"DK {format_american(row.dk_price)} vs {row.sharp_source} {sharp}",
        f"**{row.edge_pct:+.2f}%**",
        f"{kickoff_et(row.commence_time)} ET",
    ]
    return " · ".join(parts)


def chunk(header: str, lines: Sequence[str], limit: int = MAX_CONTENT) -> list[str]:
    """Split a header plus body lines into bodies Discord will accept."""
    messages: list[str] = []
    current = header
    for line in lines:
        candidate = f"{current}\n{line}"
        if len(candidate) > limit and current != header:
            messages.append(current)
            current = f"{header} (cont.)\n{line}"
        elif len(candidate) > limit:
            messages.append(current)
            current = line[:limit]
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def edge_messages(rows: Sequence[EdgeRow], min_edge_pct: float) -> list[str]:
    count = len(rows)
    header = (
        f"**{count} new edge{'' if count == 1 else 's'} at {min_edge_pct:g}%+**"
    )
    return chunk(header, [format_line(row) for row in rows])


def summary_messages(
    rows: Sequence[EdgeRow], min_edge_pct: float, top_n: int = SUMMARY_TOP_N
) -> list[str]:
    count = len(rows)
    header = f"**Good morning — {count} edge{'' if count == 1 else 's'} above {min_edge_pct:g}% today**"
    if not rows:
        return [header]
    body = [f"{i}. {format_line(row)}" for i, row in enumerate(rows[:top_n], start=1)]
    if count > top_n:
        body.append(f"…and {count - top_n} more on the dashboard.")
    return chunk(header, body)


# -- delivery -------------------------------------------------------------


def post(webhook_url: str, content: str, username: str | None = None, timeout: float = 15.0) -> None:
    import requests

    payload: dict[str, Any] = {"content": content}
    if username:
        payload["username"] = username
    try:
        response = requests.post(webhook_url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise AlertError(f"could not reach the Discord webhook: {exc}") from exc
    if not response.ok:
        raise AlertError(f"{response.status_code} from Discord: {response.text[:200]}")


# -- the morning summary --------------------------------------------------


def games_today(rows: Iterable[EdgeRow], day: str) -> bool:
    return any(et_date(row.commence_time) == day for row in rows)


def summary_due(cfg: Config, now: datetime) -> bool:
    """Is this inside the morning window the summary is allowed to go out in?

    The scan loop is what wakes this code, so the message lands on the first
    scan after the hour rather than on the hour exactly. The window stops a
    container that started in the evening from posting a "today" summary for a
    slate that has already kicked off.
    """
    local = now.astimezone(ET)
    opens = local.replace(
        hour=int(cfg.discord_summary_hour_et), minute=0, second=0, microsecond=0
    )
    return opens <= local < opens + timedelta(hours=cfg.discord_summary_window_hours)


# -- the pass itself ------------------------------------------------------


class Alerter:
    """Holds the previous scan's qualifying lines, so "new" means new."""

    def __init__(self, cfg: Config, sender: Callable[[str, str, str | None], None] | None = None):
        self.cfg = cfg
        self._previous: set[tuple[str, str, str]] = set()
        self._sender = sender or (lambda url, content, username: post(url, content, username))

    @property
    def configured(self) -> bool:
        return bool(self.cfg.discord_webhook_url)

    def run(
        self,
        rows: Sequence[EdgeRow],
        now: datetime | None = None,
        run_id: str | None = None,
    ) -> AlertOutcome:
        """One pass over a finished scan: announce what is new, then the summary."""
        now = now or datetime.now(timezone.utc)
        current = qualifying(rows, self.cfg.discord_alert_min_edge)
        previous, self._previous = self._previous, {line_id(r) for r in current}
        if not self.configured:
            # Still tracked above, so turning the webhook on mid-slate does not
            # dump the whole board into the channel at once.
            return AlertOutcome(reason="no DISCORD_WEBHOOK_URL set")

        conn = connect(self.cfg.db_path)
        try:
            init_alert_db(conn)
            return self._send(conn, rows, current, previous, now, run_id)
        finally:
            conn.close()

    def _send(
        self,
        conn: sqlite3.Connection,
        rows: Sequence[EdgeRow],
        current: Sequence[EdgeRow],
        previous: set[tuple[str, str, str]],
        now: datetime,
        run_id: str | None,
    ) -> AlertOutcome:
        outcome = AlertOutcome()
        day = et_date(now)
        sent_today = already_alerted(conn, day, EDGE)
        fresh = [
            row for row in current
            if line_id(row) not in previous and line_id(row) not in sent_today
        ]
        if fresh:
            messages = edge_messages(fresh, self.cfg.discord_alert_min_edge)
            self._deliver(messages, outcome)
            record_alerts(
                conn, day, EDGE, [line_id(r) for r in fresh], now,
                [r.edge_pct for r in fresh], run_id,
            )
            outcome.announced = list(fresh)

        if self._summary_pending(conn, rows, day, now):
            today = [r for r in qualifying(rows, self.cfg.discord_summary_min_edge)
                     if et_date(r.commence_time) == day]
            self._deliver(
                summary_messages(today, self.cfg.discord_summary_min_edge), outcome
            )
            record_alerts(conn, day, SUMMARY, [("", "", "")], now, run_id=run_id)
            outcome.summary_sent = True

        if not outcome.messages:
            outcome.reason = "nothing new to announce"
        return outcome

    def _summary_pending(
        self, conn: sqlite3.Connection, rows: Sequence[EdgeRow], day: str, now: datetime
    ) -> bool:
        if not summary_due(self.cfg, now):
            return False
        if ("", "", "") in already_alerted(conn, day, SUMMARY):
            return False
        return games_today(rows, day)

    def _deliver(self, messages: Sequence[str], outcome: AlertOutcome) -> None:
        url = self.cfg.discord_webhook_url or ""
        for content in messages:
            self._sender(url, content, self.cfg.discord_username)
            outcome.messages.append(content)


__all__ = [
    "Alerter",
    "AlertError",
    "AlertOutcome",
    "CHANNEL",
    "EDGE",
    "SUMMARY",
    "chunk",
    "edge_messages",
    "et_date",
    "format_line",
    "line_id",
    "qualifying",
    "summary_due",
    "summary_messages",
]
