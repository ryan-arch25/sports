"""Discord alerts for lines that clear the notification threshold.

Nothing is sent unless --notify is passed and a webhook URL is configured, and
each line/price combination is announced at most once: the run log remembers
what has already gone out, so a --watch loop does not repeat itself every
fifteen minutes.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import requests

from cfb_edge.config import Config
from cfb_edge.edges import TRANSLATED, EdgeRow
from cfb_edge.oddsmath import format_american
from cfb_edge.report import kickoff_et
from cfb_edge.scan import ScanResult
from cfb_edge.store import connect, init_db

CHANNEL = "discord"
MAX_EMBEDS = 10


class NotifyError(RuntimeError):
    pass


@dataclass
class NotifyOutcome:
    sent: list[EdgeRow]
    skipped_duplicates: int
    reason: str | None = None


def line_key(row: EdgeRow) -> str:
    return "none" if row.dk_point is None else f"{row.dk_point:g}"


def already_sent(conn: sqlite3.Connection, row: EdgeRow) -> bool:
    found = conn.execute(
        """
        SELECT 1 FROM notifications
        WHERE channel = ? AND event_id = ? AND market = ? AND side = ?
          AND line_key = ? AND price = ?
        LIMIT 1
        """,
        (CHANNEL, row.event_id, row.market, row.side, line_key(row), row.dk_price),
    ).fetchone()
    return found is not None


def record_sent(conn: sqlite3.Connection, rows: Sequence[EdgeRow], run_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn.executemany(
        """
        INSERT OR IGNORE INTO notifications (
            sent_at_utc, channel, event_id, market, side, line_key, price, edge_pct, run_id
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        [
            (now, CHANNEL, r.event_id, r.market, r.side, line_key(r), r.dk_price,
             r.edge_pct, run_id)
            for r in rows
        ],
    )
    conn.commit()


def qualifying(rows: Sequence[EdgeRow], min_edge_pct: float) -> list[EdgeRow]:
    keep = [r for r in rows if r.is_bet and r.edge is not None and r.edge * 100 >= min_edge_pct]
    keep.sort(key=lambda r: -(r.edge or 0.0))
    return keep


def build_payload(cfg: Config, rows: Sequence[EdgeRow], min_edge_pct: float) -> dict:
    """A Discord webhook body: a one-line summary plus an embed per bet."""
    headline = (
        f"{len(rows)} DraftKings line(s) at {min_edge_pct:g}%+ edge"
        if len(rows) != 1
        else "1 DraftKings line at "
        f"{min_edge_pct:g}%+ edge"
    )
    embeds = []
    for row in rows[:MAX_EMBEDS]:
        fields = [
            {"name": "DK", "value": format_american(row.dk_price), "inline": True},
            {
                "name": "Sharp",
                "value": f"{format_american(row.sharp_price)} ({row.sharp_source})",
                "inline": True,
            },
            {"name": "Edge", "value": f"{row.edge_pct:.2f}%", "inline": True},
            {"name": "Fair", "value": f"{(row.fair_prob or 0) * 100:.1f}%", "inline": True},
            {
                "name": "EV / $100",
                "value": f"{row.ev_per_100:+.2f}" if row.ev_per_100 is not None else "-",
                "inline": True,
            },
            {
                "name": "Stake",
                "value": f"${row.stake:,.2f}" if row.stake is not None else "-",
                "inline": True,
            },
        ]
        description = f"{row.matchup}\n{kickoff_et(row.commence_time)} ET"
        if row.status == TRANSLATED and row.sharp_point is not None:
            description += f"\nsharp number {row.sharp_point:g}, priced via the half-point table"
        embeds.append({
            "title": f"{row.market_label}: {row.pick}",
            "description": description,
            "fields": fields,
        })
    payload = {"content": headline, "embeds": embeds}
    if cfg.discord_username:
        payload["username"] = cfg.discord_username
    if len(rows) > MAX_EMBEDS:
        payload["content"] += f" (showing the top {MAX_EMBEDS})"
    return payload


def post(webhook_url: str, payload: dict, timeout: float = 15.0) -> None:
    try:
        response = requests.post(webhook_url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise NotifyError(f"could not reach the Discord webhook: {exc}") from exc
    if response.status_code == 404:
        raise NotifyError("404 from Discord: the webhook URL is wrong or was deleted.")
    if response.status_code == 429:
        raise NotifyError("429 from Discord: rate limited; nothing was sent.")
    if not response.ok:
        raise NotifyError(f"{response.status_code} from Discord: {response.text[:200]}")


def notify_bets(
    cfg: Config, result: ScanResult, dry_run: bool = False, printer=print
) -> NotifyOutcome:
    """Announce qualifying lines, skipping anything already sent at this price."""
    rows = qualifying(result.rows, cfg.discord_min_edge)
    if not rows:
        return NotifyOutcome([], 0, f"nothing at {cfg.discord_min_edge:g}%+ edge to announce")

    conn = connect(cfg.db_path)
    try:
        init_db(conn)
        fresh = [row for row in rows if not already_sent(conn, row)]
        duplicates = len(rows) - len(fresh)
        if not fresh:
            return NotifyOutcome([], duplicates, "already announced every qualifying line")

        payload = build_payload(cfg, fresh, cfg.discord_min_edge)
        if dry_run:
            printer(f"\n[discord dry run] would send {len(fresh)} bet(s):")
            printer(json.dumps(payload, indent=2)[:4000])
            return NotifyOutcome(fresh, duplicates, "dry run; nothing sent")

        if not cfg.discord_webhook_url:
            return NotifyOutcome(
                [], duplicates,
                "no Discord webhook configured; set DISCORD_WEBHOOK_URL in .env "
                "or [discord] webhook_url in config.toml",
            )
        post(cfg.discord_webhook_url, payload)
        record_sent(conn, fresh, result.run_id)
        printer(f"discord: announced {len(fresh)} bet(s) at {cfg.discord_min_edge:g}%+ edge")
        return NotifyOutcome(fresh, duplicates)
    finally:
        conn.close()
