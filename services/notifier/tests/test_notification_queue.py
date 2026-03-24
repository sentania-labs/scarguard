import json
import threading
import time

from notification_queue import NotificationQueue, QueueEntry


class EnabledNotifier:
    def __init__(self):
        self.sent_events = []

    def send(self, event):
        self.sent_events.append(event)


class DisabledNotifier:
    def send(self, event):
        raise RuntimeError("should not be called")


def test_process_due_keeps_disabled_notifier_entries(tmp_path):
    queue_path = tmp_path / "notification_queue.json"
    queue = NotificationQueue(str(queue_path))
    now = time.time()

    enabled_event = {"id": "enabled"}
    disabled_event = {"id": "disabled"}

    queue._entries = [
        QueueEntry(
            event=enabled_event,
            notifier_type="EnabledNotifier",
            attempt=0,
            next_retry=now - 1,
            first_failed=now - 20,
        ),
        QueueEntry(
            event=disabled_event,
            notifier_type="DisabledNotifier",
            attempt=0,
            next_retry=now - 1,
            first_failed=now - 20,
        ),
    ]
    queue._save()

    enabled = EnabledNotifier()
    queue.process_due([enabled], threading.Lock())

    assert enabled.sent_events == [enabled_event]
    assert queue.depth == 1
    assert queue._entries[0].event == disabled_event
    assert queue._entries[0].notifier_type == "DisabledNotifier"

    persisted = json.loads(queue_path.read_text())
    assert len(persisted) == 1
    assert persisted[0]["event"] == disabled_event
    assert persisted[0]["notifier_type"] == "DisabledNotifier"
