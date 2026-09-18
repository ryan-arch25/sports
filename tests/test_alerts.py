"""Tests for the dashboard's Discord alerts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cfb_edge.alerts import (
    CHANNEL,
    EDGE,
    SUMMARY,
    Alerter,
    chunk,
    edge_messages,
    et_date,
    format_line,
    line_id,
    qualifying,
    summary_due,
    summary_messages,
)
from cfb_edge.edges import evaluate_games
from cfb_edge.models import Outcome
from cfb_edge.store import connect

from conftest import make_game

# 9:30am ET on a Saturday, inside the summary window.
MORNING = datetime(2026, 9, 19, 13, 30, tzinfo=timezone.utc)
KICKOFF = "2026-09-19T20:00:00Z"  # 4pm ET the same day


@pytest.fixture
def alert_cfg(cfg, tmp_path):
    cfg.db_path = tmp_path / "alerts.sqlite"
    cfg.discord_webhook_url = "https://discord.example/hook"
    return cfg


class Recorder:
    """Stands in for the webhook, so nothing leaves the test."""

    def __init__(self):
        self.sent: list[str] = []

    def __call__(self, url, content, username):
        self.sent.append(content)


def rows_with_edge(cfg, dk_price=120, sharp_price=-110, event_id="evt", commence=KICKOFF):
    """DK long on the home side against a pick'em sharp line."""
    game = make_game(
        {
            "draftkings": {"h2h": [Outcome("Home Team", dk_price),
                                   Outcome("Away Team", -300)]},
            "pinnacle": {"h2h": [Outcome("Home Team", sharp_price),
                                 Outcome("Away Team", sharp_price)]},
        },
        event_id=event_id,
        commence_time=commence,
    )
    return evaluate_games([game], cfg, ("h2h",))


def alerter(cfg):
    sender = Recorder()
    return Alerter(cfg, sender=sender), sender


class TestSelection:
    def test_only_rows_at_or_above_the_threshold(self, cfg):
        rows = rows_with_edge(cfg)
        assert qualifying(rows, 1.5)
        assert not qualifying(rows, 99.0)

    def test_the_best_edge_comes_first(self, cfg):
        rows = rows_with_edge(cfg, dk_price=150) + rows_with_edge(
            cfg, dk_price=115, event_id="other"
        )
        ranked = qualifying(rows, 0.5)
        assert ranked[0].edge_pct >= ranked[-1].edge_pct

    def test_the_line_id_ignores_the_number_and_the_price(self, cfg):
        """A line that moves is the same thing to tell people about."""
        a = rows_with_edge(cfg, dk_price=120)[0]
        b = rows_with_edge(cfg, dk_price=180)[0]
        assert line_id(a) == line_id(b)


class TestWithoutAWebhook:
    def test_nothing_is_sent_and_nothing_complains(self, cfg, tmp_path):
        cfg.db_path = tmp_path / "alerts.sqlite"
        cfg.discord_webhook_url = None
        alert, sender = alerter(cfg)
        outcome = alert.run(rows_with_edge(cfg), now=MORNING)
        assert sender.sent == []
        assert not outcome.sent_anything
        assert "DISCORD_WEBHOOK_URL" in outcome.reason

    def test_no_database_is_created_either(self, cfg, tmp_path):
        cfg.db_path = tmp_path / "alerts.sqlite"
        cfg.discord_webhook_url = None
        alert, _ = alerter(cfg)
        alert.run(rows_with_edge(cfg), now=MORNING)
        assert not cfg.db_path.exists()

    def test_the_previous_scan_is_still_tracked(self, cfg, tmp_path):
        """Turning the webhook on mid-slate must not dump the whole board."""
        cfg.db_path = tmp_path / "alerts.sqlite"
        cfg.discord_webhook_url = None
        alert, sender = alerter(cfg)
        rows = rows_with_edge(cfg)
        alert.run(rows, now=MORNING)
        alert.cfg.discord_webhook_url = "https://discord.example/hook"
        alert.run(rows, now=MORNING)
        assert not any("new edge" in m for m in sender.sent)


class TestNewEdges:
    def test_a_new_edge_is_announced(self, alert_cfg):
        alert, sender = alerter(alert_cfg)
        alert.run(rows_with_edge(alert_cfg), now=MORNING)
        assert any("new edge" in m for m in sender.sent)

    def test_the_same_edge_is_not_announced_twice(self, alert_cfg):
        alert, sender = alerter(alert_cfg)
        rows = rows_with_edge(alert_cfg)
        alert.run(rows, now=MORNING)
        before = len(sender.sent)
        alert.run(rows, now=MORNING + timedelta(minutes=30))
        assert len(sender.sent) == before

    def test_a_line_that_flickers_is_announced_once(self, alert_cfg):
        """Above, below, above again is not news the second time."""
        alert, sender = alerter(alert_cfg)
        good = rows_with_edge(alert_cfg)
        alert.run(good, now=MORNING)
        alert.run(rows_with_edge(alert_cfg, dk_price=-200), now=MORNING + timedelta(hours=1))
        alert.run(good, now=MORNING + timedelta(hours=2))
        assert sum(1 for m in sender.sent if "new edge" in m) == 1

    def test_the_day_dedupe_survives_a_restart(self, alert_cfg):
        """A redeployed container has an empty memory but the same database."""
        first, sent_first = alerter(alert_cfg)
        first.run(rows_with_edge(alert_cfg), now=MORNING)
        second, sent_second = alerter(alert_cfg)
        second.run(rows_with_edge(alert_cfg), now=MORNING + timedelta(hours=1))
        assert any("new edge" in m for m in sent_first.sent)
        assert not any("new edge" in m for m in sent_second.sent)

    def test_a_new_day_announces_it_again(self, alert_cfg):
        alert, sender = alerter(alert_cfg)
        rows = rows_with_edge(alert_cfg)
        alert.run(rows, now=MORNING)
        alert._previous = set()  # as a fresh scan on the next day would be
        alert.run(rows, now=MORNING + timedelta(days=1))
        assert sum(1 for m in sender.sent if "new edge" in m) == 2

    def test_an_edge_below_the_threshold_says_nothing(self, alert_cfg):
        alert, sender = alerter(alert_cfg)
        alert_cfg.discord_alert_min_edge = 25.0
        alert.run(rows_with_edge(alert_cfg), now=MORNING)
        assert not any("new edge" in m for m in sender.sent)

    def test_what_was_written_down(self, alert_cfg):
        alert, _ = alerter(alert_cfg)
        alert.run(rows_with_edge(alert_cfg), now=MORNING)
        conn = connect(alert_cfg.db_path)
        try:
            rows = conn.execute(
                "SELECT channel, alert_date_et, kind, event_id FROM alerts WHERE kind = ?",
                (EDGE,),
            ).fetchall()
        finally:
            conn.close()
        assert rows and rows[0][0] == CHANNEL
        assert rows[0][1] == "2026-09-19"


class TestMorningSummary:
    def test_it_goes_out_inside_the_window(self, alert_cfg):
        alert, sender = alerter(alert_cfg)
        alert.run(rows_with_edge(alert_cfg), now=MORNING)
        assert any("Good morning" in m for m in sender.sent)

    def test_it_names_the_count_and_the_threshold(self, alert_cfg):
        alert, sender = alerter(alert_cfg)
        alert.run(rows_with_edge(alert_cfg), now=MORNING)
        summary = next(m for m in sender.sent if "Good morning" in m)
        assert "above 1% today" in summary

    def test_only_once_a_day(self, alert_cfg):
        alert, sender = alerter(alert_cfg)
        rows = rows_with_edge(alert_cfg)
        alert.run(rows, now=MORNING)
        alert.run(rows, now=MORNING + timedelta(minutes=30))
        assert sum(1 for m in sender.sent if "Good morning" in m) == 1

    def test_not_before_nine(self, alert_cfg):
        alert, sender = alerter(alert_cfg)
        eight_am_et = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
        alert.run(rows_with_edge(alert_cfg), now=eight_am_et)
        assert not any("Good morning" in m for m in sender.sent)

    def test_not_in_the_evening(self, alert_cfg):
        """A container that starts at 11pm must not summarize a slate already played."""
        alert, sender = alerter(alert_cfg)
        eleven_pm_et = datetime(2026, 9, 20, 3, 0, tzinfo=timezone.utc)
        alert.run(rows_with_edge(alert_cfg), now=eleven_pm_et)
        assert not any("Good morning" in m for m in sender.sent)

    def test_no_games_today_means_no_summary(self, alert_cfg):
        alert, sender = alerter(alert_cfg)
        next_week = rows_with_edge(alert_cfg, commence="2026-09-26T20:00:00Z")
        alert.run(next_week, now=MORNING)
        assert not any("Good morning" in m for m in sender.sent)

    def test_a_quiet_morning_still_reports_zero(self, alert_cfg):
        """Games today, nothing above 1%: the group still wants to know."""
        alert, sender = alerter(alert_cfg)
        flat = rows_with_edge(alert_cfg, dk_price=-115)
        alert.run(flat, now=MORNING)
        summary = next(m for m in sender.sent if "Good morning" in m)
        assert "0 edges above 1% today" in summary

    def test_it_counts_only_todays_games(self, alert_cfg):
        alert, sender = alerter(alert_cfg)
        rows = rows_with_edge(alert_cfg) + rows_with_edge(
            alert_cfg, event_id="later", commence="2026-09-26T20:00:00Z"
        )
        alert.run(rows, now=MORNING)
        summary = next(m for m in sender.sent if "Good morning" in m)
        assert "1 edge above 1% today" in summary

    @pytest.mark.parametrize(
        "hour_utc,due",
        [(12, False), (13, True), (15, True), (16, False)],  # 8, 9, 11am, noon ET
    )
    def test_the_window(self, alert_cfg, hour_utc, due):
        moment = datetime(2026, 9, 19, hour_utc, 5, tzinfo=timezone.utc)
        assert summary_due(alert_cfg, moment) is due


class TestMessages:
    def test_a_bet_is_one_line(self, cfg):
        text = format_line(rows_with_edge(cfg)[0])
        assert "\n" not in text

    def test_a_line_carries_what_you_need_to_place_it(self, cfg):
        text = format_line(rows_with_edge(cfg)[0])
        for part in ["Home Team ML", "Away Team @ Home Team", "DK +120", "pinnacle", "ET"]:
            assert part in text

    def test_no_embeds(self, cfg):
        """Compact means content only; the payload has nothing else in it."""
        messages = edge_messages(qualifying(rows_with_edge(cfg), 1.5), 1.5)
        assert all(isinstance(m, str) for m in messages)

    def test_one_edge_is_singular(self, cfg):
        one = qualifying(rows_with_edge(cfg), 1.5)
        assert len(one) == 1
        assert "1 new edge at" in edge_messages(one, 1.5)[0]

    def test_the_summary_lists_the_top_five(self, cfg):
        rows = [rows_with_edge(cfg, dk_price=200 - i, event_id=f"e{i}")[0] for i in range(9)]
        text = "\n".join(summary_messages(rows, 1.0))
        assert "5. " in text and "6. " not in text
        assert "and 4 more" in text

    def test_a_long_slate_is_split_not_truncated(self, cfg):
        rows = [rows_with_edge(cfg, event_id=f"e{i}")[0] for i in range(60)]
        messages = edge_messages(rows, 1.5)
        assert len(messages) > 1
        assert all(len(m) <= 1900 for m in messages)
        # every bet survives the split
        assert sum(m.count("Home Team ML") for m in messages) == 60

    def test_chunking_keeps_the_header_on_each_part(self):
        parts = chunk("**head**", ["x" * 300 for _ in range(20)], limit=1000)
        assert len(parts) > 1
        assert all(part.startswith("**head**") for part in parts)

    def test_a_single_line_too_long_for_a_message_is_still_sent(self):
        parts = chunk("**head**", ["y" * 5000], limit=100)
        assert parts and all(len(p) <= 100 for p in parts)


class TestEtDate:
    def test_a_late_kickoff_belongs_to_the_eastern_day(self):
        assert et_date(datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)) == "2026-09-19"
