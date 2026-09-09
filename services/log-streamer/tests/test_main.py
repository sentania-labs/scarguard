from __future__ import annotations

import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

import main
import pytest


class FakePipeline:
    def __init__(self, redis_client: FakeRedis) -> None:
        self.redis_client = redis_client
        self.commands: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, command: str) -> Any:
        def queue_command(*args: Any) -> FakePipeline:
            self.commands.append((command, args))
            return self

        return queue_command

    def execute(self) -> list[Any]:
        return [getattr(self.redis_client, command)(*args) for command, args in self.commands]


class FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = defaultdict(list)
        self.published: list[tuple[str, str]] = []
        self.sorted_sets: dict[str, dict[str, float]] = defaultdict(dict)
        self.values: dict[str, str] = {}

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self.lists[key][start : end + 1]

    def pipeline(self, transaction: bool = False) -> FakePipeline:
        assert transaction is False
        return FakePipeline(self)

    def publish(self, channel: str, line: str) -> int:
        self.published.append((channel, line))
        return 1

    def lpush(self, key: str, line: str) -> int:
        self.lists[key].insert(0, line)
        return len(self.lists[key])

    def ltrim(self, key: str, start: int, end: int) -> bool:
        self.lists[key] = self.lists[key][start : end + 1]
        return True

    def zadd(self, key: str, values: dict[str, float]) -> int:
        self.sorted_sets[key].update(values)
        return len(values)

    def zremrangebyscore(self, key: str, minimum: float, maximum: float) -> int:
        removed = [
            member
            for member, score in self.sorted_sets[key].items()
            if minimum <= score <= maximum
        ]
        for member in removed:
            del self.sorted_sets[key][member]
        return len(removed)

    def eval(self, script: str, key_count: int, *args: Any) -> int:
        assert key_count == 2
        events_key = str(args[0])
        count_key = str(args[1])
        if script == main._HEALTH_UPDATE_SCRIPT:
            now = float(args[2])
            member = str(args[3])
            cutoff = float(args[4])
            self.zadd(events_key, {member: now})
        else:
            assert script == main._HEALTH_REFRESH_SCRIPT
            cutoff = float(args[2])
        self.zremrangebyscore(events_key, float("-inf"), cutoff)
        count = len(self.sorted_sets[events_key])
        if count:
            self.values[count_key] = str(count)
        else:
            self.values.pop(count_key, None)
        return count

    def close(self) -> None:
        pass


class FakeContainer:
    def __init__(
        self,
        service: str,
        container_id: str,
        history: list[str] | None = None,
        live: list[str] | None = None,
        hold_open: threading.Event | None = None,
    ) -> None:
        self.id = container_id
        self.short_id = container_id[:12]
        self.labels = {"com.docker.compose.service": service}
        self.history = history or []
        self.live = live or []
        self.hold_open = hold_open
        self.log_calls: list[dict[str, Any]] = []

    def logs(self, **kwargs: Any) -> bytes | Any:
        self.log_calls.append(kwargs)
        if not kwargs["stream"]:
            return "".join(self.history).encode()

        def live_stream() -> Any:
            yield from self.live
            if self.hold_open is not None:
                self.hold_open.wait(timeout=2)

        return live_stream()


class FakeContainers:
    def __init__(self, containers: list[FakeContainer]) -> None:
        self._containers = containers

    def list(self, **_kwargs: Any) -> list[FakeContainer]:
        return self._containers


class FakeDockerClient:
    def __init__(self, containers: list[FakeContainer]) -> None:
        self.containers = FakeContainers(containers)
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def clear_stop_event() -> Any:
    main._stop.clear()
    yield
    main._stop.clear()


def _event_identity(container_id: str, timestamp: str, text: str) -> str:
    return f"{container_id}:{timestamp}:{text}"


def test_reattach_backfills_gap_and_deduplicates_live_overlap() -> None:
    redis_client = FakeRedis()
    buffer_key = f"{main.BUFFER_PREFIX}web"
    identity_key = f"{main.IDENTITY_PREFIX}web"
    redis_client.lists[buffer_key] = ["before-2", "before-1"]
    redis_client.lists[identity_key] = [
        _event_identity("container-1", "2026-09-09T12:00:02Z", "before-2"),
        _event_identity("container-1", "2026-09-09T12:00:01Z", "before-1"),
    ]
    container = FakeContainer(
        "web",
        "container-1",
        history=[
            "2026-09-09T12:00:01Z before-1\n",
            "2026-09-09T12:00:02Z before-2\n",
            "2026-09-09T12:00:03Z missed-1\n",
            "2026-09-09T12:00:04Z missed-2\n",
        ],
        live=[
            "2026-09-09T12:00:04Z missed-2\n",
            "2026-09-09T12:00:05Z live-1\n",
        ],
    )
    results: list[main.TailResult] = []

    main.tail_container("web", container, results.append, lambda: redis_client)

    assert container.log_calls[0] == {
        "stream": False,
        "follow": False,
        "tail": main.BACKFILL_LINES,
        "timestamps": True,
    }
    assert container.log_calls[1]["stream"] is True
    assert container.log_calls[1]["follow"] is True
    assert container.log_calls[1]["tail"] == 0
    assert container.log_calls[1]["timestamps"] is True
    assert redis_client.published == [
        (f"{main.CHANNEL_PREFIX}web", "missed-1"),
        (f"{main.CHANNEL_PREFIX}web", "missed-2"),
        (f"{main.CHANNEL_PREFIX}web", "live-1"),
    ]
    assert redis_client.lists[buffer_key][:5] == [
        "live-1",
        "missed-2",
        "missed-1",
        "before-2",
        "before-1",
    ]
    assert len(redis_client.sorted_sets[main.HEALTH_EVENTS_KEY]) == 3
    assert redis_client.values[main.HEALTH_KEY] == "3"
    assert len(results) == 1


def test_new_container_repeating_old_line_is_not_dropped() -> None:
    redis_client = FakeRedis()
    redis_client.lists[f"{main.BUFFER_PREFIX}web"] = ["ready"]
    redis_client.lists[f"{main.IDENTITY_PREFIX}web"] = [
        _event_identity("old-container", "2026-09-09T12:00:01Z", "ready")
    ]
    container = FakeContainer(
        "web",
        "new-container",
        history=["2026-09-09T12:00:01Z ready\n"],
    )

    main.tail_container("web", container, lambda _result: None, lambda: redis_client)

    assert redis_client.published == [(f"{main.CHANNEL_PREFIX}web", "ready")]


def test_health_refresh_expires_lines_outside_five_minute_window(monkeypatch: Any) -> None:
    redis_client = FakeRedis()
    times = iter([0.0, 299.0])
    monkeypatch.setattr(main.time, "time", lambda: next(times))
    monkeypatch.setattr(main.time, "time_ns", lambda: 1)
    first = main.ParsedLogLine(text="first", identity="container:0:first")
    second = main.ParsedLogLine(text="second", identity="container:299:second")

    main._publish_line(redis_client, "channel", "buffer", "identities", "web", first)
    main._publish_line(redis_client, "channel", "buffer", "identities", "web", second)
    assert redis_client.values[main.HEALTH_KEY] == "2"

    assert main.refresh_health(lambda: redis_client, now=301.0) == 1
    assert redis_client.values[main.HEALTH_KEY] == "1"
    assert main.refresh_health(lambda: redis_client, now=600.0) == 0
    assert main.HEALTH_KEY not in redis_client.values


def test_repeated_quick_eof_reattachments_recreate_stale_client(caplog: Any) -> None:
    stale_container = FakeContainer("web", "stale-web")
    stable_release = threading.Event()
    recovered_container = FakeContainer(
        "web",
        "fresh-web",
        history=["2026-09-09T12:00:01Z gap-line\n"],
        live=["2026-09-09T12:00:01Z gap-line\n"],
        hold_open=stable_release,
    )
    stale_client = FakeDockerClient([stale_container])
    recovered_client = FakeDockerClient([recovered_container])
    clients = iter([stale_client, recovered_client])
    redis_client = FakeRedis()
    streamer = main.LogStreamer(
        client_factory=lambda: next(clients),  # type: ignore[arg-type]
        redis_factory=lambda: redis_client,  # type: ignore[arg-type]
    )
    settings = main.LogStreamerSettings(
        quick_eof_limit=3,
        quick_eof_threshold_seconds=10,
    )

    for _cycle in range(3):
        streamer.run_cycle(settings)
        stale_thread, _container_id = streamer.active["web"]
        stale_thread.join(timeout=1)
        assert not stale_thread.is_alive()

    streamer.run_cycle(settings)
    recovered_thread, _container_id = streamer.active["web"]
    for _attempt in range(100):
        if redis_client.published:
            break
        threading.Event().wait(0.01)

    assert stale_client.closed is True
    assert streamer.docker_client is recovered_client
    assert recovered_thread.is_alive()
    assert redis_client.published == [(f"{main.CHANNEL_PREFIX}web", "gap-line")]
    assert "Recreated Docker client" in caplog.text

    stable_release.set()
    recovered_thread.join(timeout=1)


def test_load_settings_uses_defaults_and_operator_values(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yml"
    assert main.load_settings(missing) == main.LogStreamerSettings()

    config_path = tmp_path / "scarguard.yml"
    config_path.write_text(
        "system:\n"
        "  log_streamer:\n"
        "    quick_eof_limit: 5\n"
        "    quick_eof_threshold_seconds: 25\n"
    )
    assert main.load_settings(config_path) == main.LogStreamerSettings(
        quick_eof_limit=5,
        quick_eof_threshold_seconds=25,
    )
