import threading
import time
from unittest.mock import MagicMock

import main
from atomic_ref import AtomicRef


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
                # Stall then stop
                time.sleep(0.1)
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
        camera_cfg, detector, set(), MagicMock(), {}, frame_skip_ref, armed_ref,
        paused_ref, zones_ref, rules_ref, det_rules_ref, conf_ref,
        stop_event, None, None, health_tracker, None
    ]

    # We run the supervisor
    main.run_camera(*args)

    # Expect the worker to have crashed and been restarted
    assert call_count >= 2
    health_tracker.record_failure.assert_called_with("test_cam")

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
        camera_cfg, detector, set(), MagicMock(), {}, frame_skip_ref, armed_ref,
        paused_ref, zones_ref, rules_ref, det_rules_ref, conf_ref,
        stop_event, None, None, health_tracker, None
    ]

    # Should not raise ZeroDivisionError
    main.run_camera(*args)
    # The worker completed without exception, meaning it handled frame_skip=0

