import threading
from unittest.mock import MagicMock

import camera_health
import main
import numpy as np
import pytest
import yaml
from atomic_ref import AtomicRef
from camera_health import CameraHealthTracker


def test_load_config_rejects_zero_frame_skip(monkeypatch, tmp_path):
    config_path = tmp_path / "scarguard.yml"
    config_path.write_text(yaml.safe_dump({"detection": {"frame_skip": 0}}))
    monkeypatch.setattr(main, "CONFIG_PATH", str(config_path))

    with pytest.raises(ValueError, match="frame_skip"):
        main.load_config()


@pytest.mark.parametrize("value", [0, -1, 1.5, "2", True])
def test_frame_skip_validation_rejects_invalid_reload_values(value):
    with pytest.raises(ValueError, match="frame_skip"):
        main._validate_frame_skip(value)


def test_regression_stalled_and_exception(monkeypatch):
    stop_event = threading.Event()
    health_tracker = MagicMock()

    camera_cfg = {"name": "test_cam", "rtsp_url": "fake"}

    call_count = 0

    class StubStream:
        def __init__(self, *args, **kwargs):
            pass

        def read(self):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("unexpected failure")
            else:
                # A failed read records stalled frame progress without crashing.
                stop_event.set()
                return False, None

        def grab(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr(main, "RTSPStream", StubStream)
    monkeypatch.setattr(main, "RedisPublisher", MagicMock)

    detector = MagicMock()
    detector.model_path = "stub"

    frame_skip_ref = AtomicRef(1)
    armed_ref = AtomicRef(True)
    paused_ref = AtomicRef(False)
    zones_ref = AtomicRef([])
    rules_ref = AtomicRef([])
    det_rules_ref = AtomicRef([])
    conf_ref = AtomicRef(0.25)

    args = [
        camera_cfg,
        detector,
        set(),
        MagicMock(),
        {},
        frame_skip_ref,
        armed_ref,
        paused_ref,
        zones_ref,
        rules_ref,
        det_rules_ref,
        conf_ref,
        stop_event,
        None,
        None,
        health_tracker,
        None,
    ]

    monkeypatch.setattr(stop_event, "wait", lambda _timeout: stop_event.is_set())

    main.run_camera(*args)

    # Expect the worker to have crashed and been restarted
    assert call_count >= 2
    health_tracker.record_failure.assert_called_with("test_cam")


def test_run_camera_supports_keyword_invocation(monkeypatch):
    stop_event = threading.Event()
    worker = MagicMock(side_effect=lambda **_kwargs: stop_event.set())
    monkeypatch.setattr(main, "_camera_worker", worker)

    main.run_camera(
        camera_cfg={"name": "keyword_cam", "rtsp_url": "fake"},
        detector=MagicMock(),
        target_classes=None,
        event_processor=MagicMock(),
        redis_cfg={},
        frame_skip_ref=AtomicRef(1),
        armed_ref=AtomicRef(True),
        paused_ref=AtomicRef(False),
        exclusion_zones_ref=AtomicRef([]),
        action_rules_ref=AtomicRef([]),
        deterrent_rules_ref=AtomicRef([]),
        confidence_ref=AtomicRef(None),
        stop_event=stop_event,
    )

    worker.assert_called_once()


def test_run_camera_marks_terminal_failure_after_bounded_retries(monkeypatch):
    stop_event = threading.Event()
    worker = MagicMock(side_effect=RuntimeError("worker failure"))
    health_tracker = MagicMock()
    monkeypatch.setattr(main, "_camera_worker", worker)
    monkeypatch.setattr(stop_event, "wait", lambda _timeout: False)

    main.run_camera(
        camera_cfg={"name": "failed_cam", "rtsp_url": "fake"},
        detector=MagicMock(),
        target_classes=None,
        event_processor=MagicMock(),
        redis_cfg={},
        frame_skip_ref=AtomicRef(1),
        armed_ref=AtomicRef(True),
        paused_ref=AtomicRef(False),
        exclusion_zones_ref=AtomicRef([]),
        action_rules_ref=AtomicRef([]),
        deterrent_rules_ref=AtomicRef([]),
        confidence_ref=AtomicRef(None),
        stop_event=stop_event,
        health_tracker=health_tracker,
    )

    assert worker.call_count == 5
    health_tracker.record_terminal_failure.assert_called_once_with("failed_cam")


def test_detection_health_rejects_terminated_offline_worker():
    dead_thread = MagicMock()
    dead_thread.is_alive.return_value = False
    dead_state = MagicMock(thread=dead_thread)

    assert not main._detection_is_healthy(
        {"failed_cam": dead_state},
        {"failed_cam": {"state": "offline"}},
    )


def test_detection_health_requires_frame_progress():
    live_thread = MagicMock()
    live_thread.is_alive.return_value = True
    state = MagicMock(thread=live_thread)

    assert not main._detection_is_healthy({"starting_cam": state}, {})


def test_detection_health_rejects_stalled_real_tracker(monkeypatch):
    now = 100.0
    monkeypatch.setattr(camera_health.time, "monotonic", lambda: now)
    tracker = CameraHealthTracker(debounce_seconds=5)
    tracker.record_frame("stalled_cam")
    now += 11.0

    live_thread = MagicMock()
    live_thread.is_alive.return_value = True
    assert not main._detection_is_healthy(
        {"stalled_cam": MagicMock(thread=live_thread)},
        tracker.get_all_status(),
    )


def test_real_worker_stalled_frames_drive_health_unhealthy(monkeypatch):
    stop_event = threading.Event()
    now = 100.0
    monkeypatch.setattr(camera_health.time, "monotonic", lambda: now)
    tracker = CameraHealthTracker(debounce_seconds=5)

    class StubStream:
        reads = 0

        def __init__(self, **_kwargs):
            pass

        def read(self):
            nonlocal now
            self.reads += 1
            if self.reads == 1:
                return True, np.zeros((2, 2, 3), dtype=np.uint8)
            now += 11.0
            stop_event.set()
            return False, None

        def grab(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr(main, "RTSPStream", StubStream)
    monkeypatch.setattr(main, "RedisPublisher", MagicMock)
    detector = MagicMock(model_path="stub")
    detector.predict.return_value = []

    main._camera_worker(
        camera_cfg={"name": "stalled_cam", "rtsp_url": "fake"},
        detector=detector,
        target_classes=None,
        event_processor=MagicMock(),
        redis_cfg={},
        frame_skip_ref=AtomicRef(1),
        armed_ref=AtomicRef(True),
        paused_ref=AtomicRef(False),
        exclusion_zones_ref=AtomicRef([]),
        action_rules_ref=AtomicRef([]),
        deterrent_rules_ref=AtomicRef([]),
        confidence_ref=AtomicRef(None),
        stop_event=stop_event,
        health_tracker=tracker,
    )

    live_thread = MagicMock()
    live_thread.is_alive.return_value = True
    assert not main._detection_is_healthy(
        {"stalled_cam": MagicMock(thread=live_thread)},
        tracker.get_all_status(),
    )


def test_detection_health_isolates_failed_camera():
    dead_thread = MagicMock()
    dead_thread.is_alive.return_value = False
    live_thread = MagicMock()
    live_thread.is_alive.return_value = True

    assert main._detection_is_healthy(
        {
            "failed_cam": MagicMock(thread=dead_thread),
            "healthy_cam": MagicMock(thread=live_thread),
        },
        {
            "failed_cam": {"state": "offline"},
            "healthy_cam": {"state": "online"},
        },
    )


def test_frame_skip_zero(monkeypatch):
    stop_event = threading.Event()
    health_tracker = MagicMock()
    camera_cfg = {"name": "test_cam2", "rtsp_url": "fake"}

    class StubStream:
        def __init__(self, *args, **kwargs):
            pass

        def read(self):
            stop_event.set()
            return False, None

        def grab(self):
            stop_event.set()
            return False

        def release(self):
            pass

    monkeypatch.setattr(main, "RTSPStream", StubStream)
    monkeypatch.setattr(main, "RedisPublisher", MagicMock)

    detector = MagicMock()
    detector.model_path = "stub"

    frame_skip_ref = AtomicRef(0)  # zero frame skip!
    armed_ref = AtomicRef(True)
    paused_ref = AtomicRef(False)
    zones_ref = AtomicRef([])
    rules_ref = AtomicRef([])
    det_rules_ref = AtomicRef([])
    conf_ref = AtomicRef(0.25)

    args = [
        camera_cfg,
        detector,
        set(),
        MagicMock(),
        {},
        frame_skip_ref,
        armed_ref,
        paused_ref,
        zones_ref,
        rules_ref,
        det_rules_ref,
        conf_ref,
        stop_event,
        None,
        None,
        health_tracker,
        None,
    ]

    # Should not raise ZeroDivisionError
    main.run_camera(*args)
    # The worker completed without exception, meaning it handled frame_skip=0
