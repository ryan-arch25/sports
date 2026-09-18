"""Tests for Discord notifications."""

from __future__ import annotations

import pytest

import cfb_edge.notify as notify
from cfb_edge.edges import DIFFERENT_NUMBER, PRICED, TRANSLATED, EdgeRow
from cfb_edge.models import parse_commence_time
from cfb_edge.notify import (
    CHANNEL,
    NotifyError,
    build_payload,
    line_key,
    notify_bets,
    qualifying,
)
from cfb_edge.scan import ScanResult
from cfb_edge.store import connect, init_db

from test_api import FakeResponse


def row(**kwargs) -> EdgeRow:
    defaults = dict(
        event_id="evt", commence_time=parse_commence_time("2026-09-20T23:30:00Z"),
        home_team="Alabama Crimson Tide", away_team="Georgia Bulldogs", market="totals",
        side="Over", dk_point=51.5, dk_price=-110, dk_prob=0.5238,
        sharp_source="pinnacle", sharp_books="pinnacle", sharp_point=51.5,
        sharp_price=-105, sharp_hold=0.024, fair_prob=0.56, fair_american=-127,
        edge=0.036, ev_per_100=7.2, stake=40.0, status=PRICED,
    )
    defaults.update(kwargs)
    return EdgeRow(**defaults)


class FakeSnapshot:
    quota: dict = {}
    fetched_at = parse_commence_time("2026-09-18T14:05:00Z")

    class path:
        name = "snapshot.json"


def result(rows) -> ScanResult:
    return ScanResult(
        run_id="run1", started_at=FakeSnapshot.fetched_at, snapshot=FakeSnapshot(),
        source="cache", games=[], rows=rows, bets=rows, markets=("totals",), min_edge=1.0,
    )


@pytest.fixture
def notify_cfg(cfg, tmp_path):
    cfg.db_path = tmp_path / "log.sqlite"
    cfg.discord_webhook_url = "https://discord.test/webhook"
    cfg.discord_min_edge = 2.0
    return cfg


class TestQualifying:
    def test_only_lines_over_the_threshold(self):
        rows = [row(edge=0.036), row(side="Under", edge=0.005)]
        assert [r.side for r in qualifying(rows, 2.0)] == ["Over"]

    def test_best_edge_first(self):
        rows = [row(edge=0.021), row(side="Under", edge=0.044)]
        assert [r.side for r in qualifying(rows, 2.0)] == ["Under", "Over"]

    def test_unpriced_rows_are_never_announced(self):
        rows = [row(status=DIFFERENT_NUMBER, edge=None)]
        assert qualifying(rows, 2.0) == []

    def test_translated_rows_are_announced(self):
        rows = [row(status=TRANSLATED, edge=0.038)]
        assert len(qualifying(rows, 2.0)) == 1

    def test_line_key_covers_a_missing_number(self):
        assert line_key(row(dk_point=None)) == "none"
        assert line_key(row(dk_point=51.5)) == "51.5"


class TestPayload:
    def test_summary_and_one_embed_per_bet(self, notify_cfg):
        payload = build_payload(notify_cfg, [row(), row(side="Under")], 2.0)
        assert "2 DraftKings line(s) at 2%+ edge" in payload["content"]
        assert len(payload["embeds"]) == 2
        assert payload["username"] == "cfb-edge"

    def test_the_embed_carries_the_numbers_that_matter(self, notify_cfg):
        embed = build_payload(notify_cfg, [row()], 2.0)["embeds"][0]
        assert embed["title"] == "Total: Over 51.5"
        assert "Georgia Bulldogs @ Alabama Crimson Tide" in embed["description"]
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["DK"] == "-110"
        assert fields["Edge"] == "3.60%"
        assert fields["Stake"] == "$40.00"

    def test_a_translated_bet_says_which_number_the_sharp_was_on(self, notify_cfg):
        embed = build_payload(
            notify_cfg, [row(status=TRANSLATED, sharp_point=53.0)], 2.0
        )["embeds"][0]
        assert "sharp number 53" in embed["description"]

    def test_discord_embed_cap_is_respected(self, notify_cfg):
        rows = [row(side=f"Side {i}") for i in range(15)]
        payload = build_payload(notify_cfg, rows, 2.0)
        assert len(payload["embeds"]) == notify.MAX_EMBEDS
        assert "showing the top 10" in payload["content"]


class TestSending:
    def test_posts_once_and_records_it(self, notify_cfg, monkeypatch):
        posted = {}

        def fake_post(url, json=None, timeout=None):
            posted.update(url=url, body=json)
            return FakeResponse(204)

        monkeypatch.setattr(notify.requests, "post", fake_post)
        outcome = notify_bets(notify_cfg, result([row()]), printer=lambda *a: None)
        assert len(outcome.sent) == 1
        assert posted["url"] == "https://discord.test/webhook"

        conn = connect(notify_cfg.db_path)
        try:
            sent = conn.execute("SELECT * FROM notifications").fetchall()
        finally:
            conn.close()
        assert len(sent) == 1
        assert sent[0]["channel"] == CHANNEL
        assert sent[0]["line_key"] == "51.5"

    def test_the_same_price_is_never_announced_twice(self, notify_cfg, monkeypatch):
        calls = {"n": 0}

        def fake_post(url, json=None, timeout=None):
            calls["n"] += 1
            return FakeResponse(204)

        monkeypatch.setattr(notify.requests, "post", fake_post)
        notify_bets(notify_cfg, result([row()]), printer=lambda *a: None)
        outcome = notify_bets(notify_cfg, result([row()]), printer=lambda *a: None)
        assert calls["n"] == 1
        assert outcome.sent == []
        assert outcome.skipped_duplicates == 1

    def test_a_better_price_is_announced_again(self, notify_cfg, monkeypatch):
        calls = {"n": 0}

        def fake_post(url, json=None, timeout=None):
            calls["n"] += 1
            return FakeResponse(204)

        monkeypatch.setattr(notify.requests, "post", fake_post)
        notify_bets(notify_cfg, result([row()]), printer=lambda *a: None)
        notify_bets(notify_cfg, result([row(dk_price=-105)]), printer=lambda *a: None)
        assert calls["n"] == 2

    def test_nothing_qualifying_sends_nothing(self, notify_cfg, monkeypatch):
        monkeypatch.setattr(
            notify.requests, "post", lambda *a, **k: pytest.fail("should not post")
        )
        outcome = notify_bets(notify_cfg, result([row(edge=0.001)]))
        assert outcome.sent == []
        assert "nothing at 2%+" in outcome.reason

    def test_a_dry_run_sends_nothing_and_shows_the_payload(self, notify_cfg, monkeypatch):
        printed = []
        monkeypatch.setattr(
            notify.requests, "post", lambda *a, **k: pytest.fail("should not post")
        )
        outcome = notify_bets(
            notify_cfg, result([row()]), dry_run=True, printer=printed.append
        )
        assert outcome.reason == "dry run; nothing sent"
        assert any("would send 1 bet" in line for line in printed)

    def test_a_dry_run_does_not_mark_anything_as_sent(self, notify_cfg, monkeypatch):
        monkeypatch.setattr(notify.requests, "post", lambda *a, **k: FakeResponse(204))
        notify_bets(notify_cfg, result([row()]), dry_run=True, printer=lambda *a: None)
        conn = connect(notify_cfg.db_path)
        try:
            init_db(conn)
            assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
        finally:
            conn.close()

    def test_a_missing_webhook_is_reported_not_raised(self, notify_cfg):
        notify_cfg.discord_webhook_url = None
        outcome = notify_bets(notify_cfg, result([row()]))
        assert outcome.sent == []
        assert "no Discord webhook configured" in outcome.reason

    @pytest.mark.parametrize(
        "status,message", [(404, "webhook URL is wrong"), (429, "rate limited"), (500, "500")]
    )
    def test_http_errors_are_translated(self, notify_cfg, monkeypatch, status, message):
        monkeypatch.setattr(
            notify.requests, "post", lambda *a, **k: FakeResponse(status, text="nope")
        )
        with pytest.raises(NotifyError, match=message):
            notify_bets(notify_cfg, result([row()]), printer=lambda *a: None)

    def test_a_network_failure_is_translated(self, notify_cfg, monkeypatch):
        def fail(*args, **kwargs):
            raise notify.requests.RequestException("dns")

        monkeypatch.setattr(notify.requests, "post", fail)
        with pytest.raises(NotifyError, match="could not reach"):
            notify_bets(notify_cfg, result([row()]), printer=lambda *a: None)

    def test_a_failed_post_is_not_recorded_as_sent(self, notify_cfg, monkeypatch):
        monkeypatch.setattr(notify.requests, "post", lambda *a, **k: FakeResponse(500))
        with pytest.raises(NotifyError):
            notify_bets(notify_cfg, result([row()]), printer=lambda *a: None)
        conn = connect(notify_cfg.db_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
        finally:
            conn.close()
