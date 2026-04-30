"""Tests for CameraHealthTracker, focused on the v1.14.4 recovery alert
+ flap-suppression behaviour added for #131.
"""

import pytest
from camera_health import CameraHealthTracker


class FakeClock:
    """Controllable replacement for time.monotonic() in tests."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr("camera_health.time.monotonic", c)
    return c


def _alert_types(alerts: list[dict]) -> list[str]:
    return [a["type"] for a in alerts]


def test_clean_outage_emits_offline_then_recovery(clock):
    """Camera offline >threshold fires offline alert. Stays online >threshold
    after reconnect → recovery alert with both durations populated.
    """
    t = CameraHealthTracker(alert_threshold_seconds=10, debounce_seconds=2)

    # t=0: first frame → online
    t.record_frame("cam-a")
    assert t.check_alerts() == []

    # t=5: stream fails. Debounce (2s) is exceeded vs last_frame_at=0,
    # so offline_since gets set immediately.
    clock.advance(5)
    t.record_failure("cam-a")

    # t=20: 15s offline >= 10s threshold → offline alert
    clock.advance(15)
    alerts = t.check_alerts()
    assert _alert_types(alerts) == ["camera_offline"]
    assert alerts[0]["camera_name"] == "cam-a"
    assert alerts[0]["offline_seconds"] == 15.0

    # t=22: reconnect
    clock.advance(2)
    t.record_frame("cam-a")

    # t=25: only 3s online, below threshold → no recovery alert yet
    clock.advance(3)
    assert t.check_alerts() == []

    # t=33: 11s online >= 10s threshold → recovery alert
    clock.advance(8)
    alerts = t.check_alerts()
    assert _alert_types(alerts) == ["camera_recovered"]
    rec = alerts[0]
    assert rec["camera_name"] == "cam-a"
    assert rec["offline_seconds"] == 17.0  # 22 - 5
    assert rec["online_seconds"] == 11.0  # 33 - 22
    assert rec["reconnect_count"] == 1

    # No duplicate recovery alert on subsequent checks
    clock.advance(60)
    assert t.check_alerts() == []


def test_short_flap_below_threshold_emits_no_alerts(clock):
    """A camera that bounces offline-then-online faster than the alert
    threshold must NOT page the user with offline OR recovery alerts.
    Flap suppression is the core promise of #131.
    """
    t = CameraHealthTracker(alert_threshold_seconds=10, debounce_seconds=2)

    t.record_frame("cam-a")  # online at t=0

    # Three short outages, each well below the 10s alert threshold
    for _ in range(3):
        clock.advance(5)
        t.record_failure("cam-a")  # debounce satisfied → offline_since set
        clock.advance(3)
        t.record_frame("cam-a")  # back online quickly

    # check_alerts at any point should produce nothing
    assert t.check_alerts() == []
    clock.advance(100)
    assert t.check_alerts() == []


def test_first_frame_startup_emits_no_alerts(clock):
    """A camera that comes up cleanly at startup (never been offline) must
    not produce a recovery alert just because it's been online a long time.
    """
    t = CameraHealthTracker(alert_threshold_seconds=10, debounce_seconds=2)

    t.record_frame("cam-a")
    clock.advance(60)  # well past threshold

    assert t.check_alerts() == []


def test_post_recovery_outage_cycles_cleanly(clock):
    """After a full offline → recovery cycle, the next outage must produce
    a fresh offline alert (not stuck in outage_alerted=True).
    """
    t = CameraHealthTracker(alert_threshold_seconds=10, debounce_seconds=2)

    # First outage cycle
    t.record_frame("cam-a")
    clock.advance(5)
    t.record_failure("cam-a")
    clock.advance(15)
    assert _alert_types(t.check_alerts()) == ["camera_offline"]

    clock.advance(2)
    t.record_frame("cam-a")  # reconnect
    clock.advance(11)
    assert _alert_types(t.check_alerts()) == ["camera_recovered"]

    # Second outage cycle — must fire its own offline alert
    clock.advance(5)
    t.record_failure("cam-a")
    clock.advance(15)
    alerts = t.check_alerts()
    assert _alert_types(alerts) == ["camera_offline"]
    assert alerts[0]["reconnect_count"] == 1  # carries from prior cycle


def test_brief_mid_outage_recovery_does_not_emit_premature_recovery(clock):
    """Camera offline >threshold (offline alert fires) → reconnects briefly
    (below stable-uptime threshold) → fails again. The brief reconnect
    must NOT emit a recovery alert; the camera is still flapping.
    """
    t = CameraHealthTracker(alert_threshold_seconds=10, debounce_seconds=2)

    t.record_frame("cam-a")
    clock.advance(5)
    t.record_failure("cam-a")
    clock.advance(15)
    assert _alert_types(t.check_alerts()) == ["camera_offline"]

    # Brief 4s reconnect (below 10s threshold)
    clock.advance(2)
    t.record_frame("cam-a")
    clock.advance(4)
    assert t.check_alerts() == []  # no recovery yet — not stable

    # Goes offline again before stability
    t.record_failure("cam-a")
    clock.advance(15)
    # Per the design, this is treated as a new continuous outage and fires
    # a fresh offline alert (alert_sent was reset on reconnect).
    alerts = t.check_alerts()
    assert _alert_types(alerts) == ["camera_offline"]
