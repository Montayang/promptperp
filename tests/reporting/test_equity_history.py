from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from promptperp.reporting.equity_history import EquityHistory

NOW = datetime(2025, 1, 2, 0, tzinfo=timezone.utc)


def make_history(path, deployment_id):
    return EquityHistory(path, deployment_id, initial_equity=Decimal("2000"))


def record(report, at=NOW, equity="2000"):
    report.record(
        at=at,
        equity=Decimal(equity),
        realized=Decimal("0"),
        commission=Decimal("0"),
        funding=Decimal("0"),
        gross=Decimal("0"),
        net=Decimal("0"),
        position_count=0,
    )


def test_samples_are_idempotent_and_deployment_scoped(tmp_path):
    report = make_history(tmp_path / "r.sqlite3", "one")
    record(report)
    record(report)
    record(report, NOW + timedelta(minutes=1), "2020")
    record(report, NOW + timedelta(minutes=2), "1980")
    text, rows = report.summary(NOW, NOW + timedelta(minutes=2))
    assert len(rows) == 3
    assert "-1.0000%" in text
    assert "1.9802%" in text
    other = make_history(report.path, "two")
    assert not other.samples(NOW, NOW + timedelta(days=1))
    html = report.html(NOW, NOW + timedelta(minutes=2))
    assert "<svg" in html and html.count("<polyline") == 2


def test_daily_time_success_and_restart_dedup(tmp_path):
    report = make_history(tmp_path / "r.sqlite3", "one")
    record(report)
    sent = []

    def notify(subject, body):
        sent.append((subject, body))

    assert not report.daily(NOW - timedelta(seconds=1), notify)
    assert report.daily(NOW, notify)
    assert not make_history(report.path, "one").daily(NOW, notify)
    assert len(sent) == 1


def test_stale_samples_and_uncertain_delivery(tmp_path):
    report = make_history(tmp_path / "r.sqlite3", "one")
    record(report)

    def fail(subject, body):
        raise TimeoutError

    assert not report.daily(NOW + timedelta(minutes=3), fail)
    with pytest.raises(TimeoutError):
        report.daily(NOW, fail)
    assert not report.daily(NOW, fail)
    with report.connect() as db:
        assert db.execute("SELECT state FROM deliveries").fetchone()[0] == "UNCERTAIN"


def test_gaps_are_not_drawn_as_continuous_returns(tmp_path):
    report = make_history(tmp_path / "r.sqlite3", "<private>")
    record(report)
    record(report, NOW + timedelta(hours=3))
    html = report.html(NOW, NOW + timedelta(days=1))
    assert "<polyline" not in html
    assert "&lt;private&gt;" in html
    with pytest.raises(ValueError):
        report.samples(NOW.replace(tzinfo=None), NOW)


def test_initial_equity_is_explicit_and_cannot_change_on_reopen(tmp_path):
    path = tmp_path / "r.sqlite3"
    make_history(path, "one")
    with pytest.raises(ValueError, match="policy differs"):
        EquityHistory(path, "one", initial_equity=Decimal("3000"))
    with pytest.raises(ValueError):
        EquityHistory(path, "bad", initial_equity=Decimal("0"))


def test_timezone_and_daily_hour_are_configurable(tmp_path):
    report = EquityHistory(
        tmp_path / "r.sqlite3",
        "one",
        initial_equity=Decimal("2000"),
        schedule_timezone="Europe/London",
        schedule_hour=9,
    )
    record(report, NOW + timedelta(hours=9))
    sent = []

    def notify(subject, body):
        sent.append(subject)

    assert not report.daily(NOW + timedelta(hours=8), notify)
    assert report.daily(NOW + timedelta(hours=9), notify)
    assert len(sent) == 1
    with pytest.raises(ValueError):
        report.daily(NOW.replace(tzinfo=None), notify)
