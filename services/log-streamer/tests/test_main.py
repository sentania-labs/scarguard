from __future__ import annotations

import json
import threading
from collections import defaultdict
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

    def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
    ) -> bool:
        if ex is not None:
            assert ex == main.HEALTH_STATUS_TTL_SECONDS
        self.values[key] = value
        return True

    def delete(self, key: str) -> int:
        removed = int(self.values.pop(key, None) is not None)
        removed += int(self.lists.pop(key, None) is not None)
        return removed

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
        hold_started: threading.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        self.id = container_id
        self.short_id = container_id[:12]
        self.labels = {"com.docker.compose.service": service}
        self.history = history or []
        self.live = live or []
        self.hold_open = hold_open
        self.hold_started = hold_started
        self.error = error
        self.log_calls: list[dict[str, Any]] = []

    def logs(self, **kwargs: Any) -> Any:
        self.log_calls.append(kwargs)

        def log_stream() -> Any:
            yield from self.history
            yield from self.live
            if self.error is not None:
                raise self.error
            if self.hold_open is not None:
                if self.hold_started is not None:
                    self.hold_started.set()
                self.hold_open.wait(timeout=2)

        return log_stream()


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


def _buffer_entry(container_id: str, timestamp: str, text: str) -> str:
    return main.BufferedLogEntry(
        envelope="scarguard-log-buffer-v1",
        text=text,
        identity=_event_identity(container_id, timestamp, text),
    ).model_dump_json()


def _buffer_texts(redis_client: FakeRedis, buffer_key: str) -> list[str]:
    return [
        main.BufferedLogEntry.model_validate_json(entry).text
        for entry in redis_client.lists[buffer_key]
    ]


def _tail_container(
    service: str,
    container: FakeContainer,
    result_callback: Any,
    redis_factory: Any,
) -> None:
    generation_guard = main.TailGeneration()
    generation = generation_guard.advance()
    main.tail_container(
        service,
        container,
        result_callback,
        generation_guard,
        generation,
        redis_factory,
    )


def test_reattach_backfills_gap_and_deduplicates_live_overlap(caplog: Any) -> None:
    redis_client = FakeRedis()
    buffer_key = f"{main.BUFFER_PREFIX}web"
    redis_client.lists[buffer_key] = [
        _buffer_entry("container-1", "2026-09-09T12:00:02Z", "before-2"),
        _buffer_entry("container-1", "2026-09-09T12:00:01Z", "before-1"),
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

    _tail_container("web", container, results.append, lambda: redis_client)

    assert container.log_calls == [
        {
            "stream": True,
            "follow": True,
            "tail": main.BACKFILL_LINES,
            "timestamps": True,
        }
    ]
    assert redis_client.published == [
        (f"{main.CHANNEL_PREFIX}web", "missed-1"),
        (f"{main.CHANNEL_PREFIX}web", "missed-2"),
        (f"{main.CHANNEL_PREFIX}web", "live-1"),
    ]
    assert _buffer_texts(redis_client, buffer_key)[:5] == [
        "live-1",
        "missed-2",
        "missed-1",
        "before-2",
        "before-1",
    ]
    assert json.loads(redis_client.lists[buffer_key][0])[
        "__scarguard_log_buffer__"
    ] == "scarguard-log-buffer-v1"
    assert len(redis_client.sorted_sets[main.HEALTH_EVENTS_KEY]) == 3
    assert redis_client.values[main.HEALTH_KEY] == "3"
    assert len(results) == 1
    assert "pre-upgrade buffer has no identities" not in caplog.text


def test_new_container_repeating_old_line_is_not_dropped() -> None:
    redis_client = FakeRedis()
    redis_client.lists[f"{main.BUFFER_PREFIX}web"] = [
        _buffer_entry("old-container", "2026-09-09T12:00:01Z", "ready")
    ]
    container = FakeContainer(
        "web",
        "new-container",
        history=["2026-09-09T12:00:01Z ready\n"],
    )

    _tail_container("web", container, lambda _result: None, lambda: redis_client)

    assert redis_client.published == [(f"{main.CHANNEL_PREFIX}web", "ready")]


def test_legacy_buffer_backfill_is_seeded_without_republishing(
    monkeypatch: Any,
    caplog: Any,
) -> None:
    redis_client = FakeRedis()
    buffer_key = f"{main.BUFFER_PREFIX}web"
    redis_client.lists[buffer_key] = ["legacy-2", "legacy-1"]
    cutoff = main._timestamp_seconds("2026-09-09T12:00:04Z")
    assert cutoff is not None
    monkeypatch.setattr(main.time, "time", lambda: cutoff)
    caplog.set_level("INFO")
    container = FakeContainer(
        "web",
        "container-1",
        history=[
            "2026-09-09T12:00:01Z legacy-1\n",
            "2026-09-09T12:00:02Z legacy-2\n",
        ],
        live=["2026-09-09T12:00:05Z current\n"],
    )

    _tail_container("web", container, lambda _result: None, lambda: redis_client)
    _tail_container("web", container, lambda _result: None, lambda: redis_client)

    assert redis_client.published == [(f"{main.CHANNEL_PREFIX}web", "current")]
    assert _buffer_texts(redis_client, buffer_key) == [
        "current",
        "legacy-2",
        "legacy-1",
    ]
    assert caplog.text.count("pre-upgrade buffer has no identities") == 1


def test_legacy_migration_remains_pending_after_attachment_failure(
    monkeypatch: Any,
    caplog: Any,
) -> None:
    redis_client = FakeRedis()
    buffer_key = f"{main.BUFFER_PREFIX}web"
    redis_client.lists[buffer_key] = ["legacy"]
    cutoff = main._timestamp_seconds("2026-09-09T12:00:04Z")
    assert cutoff is not None
    monkeypatch.setattr(main.time, "time", lambda: cutoff)
    caplog.set_level("INFO")
    failed = FakeContainer(
        "web",
        "container-1",
        error=RuntimeError("attach failed"),
    )

    _tail_container("web", failed, lambda _result: None, lambda: redis_client)

    assert redis_client.lists[buffer_key] == ["legacy"]

    recovered = FakeContainer(
        "web",
        "container-1",
        history=["2026-09-09T12:00:01Z legacy\n"],
        live=["2026-09-09T12:00:05Z current\n"],
    )
    _tail_container("web", recovered, lambda _result: None, lambda: redis_client)

    assert redis_client.published == [(f"{main.CHANNEL_PREFIX}web", "current")]
    assert _buffer_texts(redis_client, buffer_key) == ["current", "legacy"]
    assert caplog.text.count("pre-upgrade buffer has no identities") == 1


def test_legacy_migration_failure_retries_first_live_line(monkeypatch: Any) -> None:
    class FailingPipeline(FakePipeline):
        def execute(self) -> list[Any]:
            raise main.redislib.RedisError("temporary migration failure")

    class FlakyMigrationRedis(FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.migration_failures = 1

        def pipeline(self, transaction: bool = False) -> FakePipeline:
            if transaction and self.migration_failures:
                self.migration_failures -= 1
                return FailingPipeline(self)
            return super().pipeline(transaction)

    redis_client = FlakyMigrationRedis()
    buffer_key = f"{main.BUFFER_PREFIX}web"
    redis_client.lists[buffer_key] = ["legacy"]
    cutoff = main._timestamp_seconds("2026-09-09T12:00:04Z")
    assert cutoff is not None
    monkeypatch.setattr(main.time, "time", lambda: cutoff)
    container = FakeContainer(
        "web",
        "container-1",
        history=["2026-09-09T12:00:01Z legacy\n"],
        live=[
            "2026-09-09T12:00:05Z first-live\n",
            "2026-09-09T12:00:06Z later-live\n",
        ],
    )
    results: list[main.TailResult] = []

    _tail_container("web", container, results.append, lambda: redis_client)

    assert results[0].failed is True
    assert redis_client.published == []
    assert redis_client.lists[buffer_key] == ["legacy"]

    _tail_container("web", container, results.append, lambda: redis_client)

    assert results[1].failed is False
    assert redis_client.published == [
        (f"{main.CHANNEL_PREFIX}web", "first-live"),
        (f"{main.CHANNEL_PREFIX}web", "later-live"),
    ]
    assert _buffer_texts(redis_client, buffer_key) == [
        "later-live",
        "first-live",
        "legacy",
    ]


def test_legacy_migration_preserves_unobserved_history(monkeypatch: Any) -> None:
    redis_client = FakeRedis()
    buffer_key = f"{main.BUFFER_PREFIX}web"
    redis_client.lists[buffer_key] = ["seen", "older-unseen"]
    cutoff = main._timestamp_seconds("2026-09-09T12:00:04Z")
    assert cutoff is not None
    monkeypatch.setattr(main.time, "time", lambda: cutoff)
    container = FakeContainer(
        "web",
        "container-1",
        history=["2026-09-09T12:00:01Z seen\n"],
    )

    _tail_container("web", container, lambda _result: None, lambda: redis_client)

    assert _buffer_texts(redis_client, buffer_key) == ["seen", "older-unseen"]
    entries = [
        main.BufferedLogEntry.model_validate_json(entry)
        for entry in redis_client.lists[buffer_key]
    ]
    assert entries[0].identity == _event_identity(
        "container-1", "2026-09-09T12:00:01Z", "seen"
    )
    assert entries[1].identity.startswith("legacy-buffer:")


def test_legacy_application_json_is_not_treated_as_buffer_envelope(
    monkeypatch: Any,
) -> None:
    redis_client = FakeRedis()
    buffer_key = f"{main.BUFFER_PREFIX}web"
    application_json = '{"v":1,"text":"app text","identity":"app identity"}'
    redis_client.lists[buffer_key] = [application_json]
    cutoff = main._timestamp_seconds("2026-09-09T12:00:04Z")
    assert cutoff is not None
    monkeypatch.setattr(main.time, "time", lambda: cutoff)
    container = FakeContainer(
        "web",
        "container-1",
        history=[f"2026-09-09T12:00:01Z {application_json}\n"],
    )

    _tail_container("web", container, lambda _result: None, lambda: redis_client)

    assert redis_client.published == []
    assert _buffer_texts(redis_client, buffer_key) == [application_json]


def test_legacy_migration_matches_repeated_text_in_newest_window(
    monkeypatch: Any,
) -> None:
    redis_client = FakeRedis()
    buffer_key = f"{main.BUFFER_PREFIX}web"
    fillers = [f"filler-{index}" for index in range(main.BACKFILL_LINES)]
    redis_client.lists[buffer_key] = ["repeated", *fillers, "repeated"]
    cutoff = main._timestamp_seconds("2026-09-09T12:00:04Z")
    assert cutoff is not None
    monkeypatch.setattr(main.time, "time", lambda: cutoff)
    container = FakeContainer(
        "web",
        "container-1",
        history=["2026-09-09T12:00:01Z repeated\n"],
    )

    _tail_container("web", container, lambda _result: None, lambda: redis_client)

    entries = [
        main.BufferedLogEntry.model_validate_json(entry)
        for entry in redis_client.lists[buffer_key]
    ]
    assert entries[0].identity == _event_identity(
        "container-1", "2026-09-09T12:00:01Z", "repeated"
    )
    assert entries[1].text == "filler-0"
    assert entries[-1].text == "repeated"
    assert entries[-1].identity.startswith("legacy-buffer:")


def test_coupled_buffer_deduplicates_without_separate_identity_key(
    caplog: Any,
) -> None:
    redis_client = FakeRedis()
    buffer_key = f"{main.BUFFER_PREFIX}web"
    redis_client.lists[buffer_key] = [
        _buffer_entry("container-1", "2026-09-09T12:00:01Z", "before")
    ]
    container = FakeContainer(
        "web",
        "container-1",
        history=[
            "2026-09-09T12:00:01Z before\n",
            "2026-09-09T12:00:02Z missed\n",
        ],
    )

    _tail_container("web", container, lambda _result: None, lambda: redis_client)

    assert redis_client.published == [(f"{main.CHANNEL_PREFIX}web", "missed")]
    assert "pre-upgrade buffer has no identities" not in caplog.text


def test_quick_eof_timing_excludes_redis_preparation(monkeypatch: Any) -> None:
    clock = [0.0]

    class SlowRedis(FakeRedis):
        def lrange(self, key: str, start: int, end: int) -> list[str]:
            clock[0] += main.QUICK_EOF_THRESHOLD_SECONDS + 1
            return super().lrange(key, start, end)

    redis_client = SlowRedis()
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    results: list[main.TailResult] = []

    _tail_container(
        "web",
        FakeContainer("web", "container-1"),
        results.append,
        lambda: redis_client,
    )

    assert results[0].elapsed_seconds == 0


def test_transient_redis_read_failure_retries_without_republishing() -> None:
    class FlakyRedis(FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.fail_reads = 1

        def lrange(self, key: str, start: int, end: int) -> list[str]:
            if self.fail_reads:
                self.fail_reads -= 1
                raise main.redislib.RedisError("temporary read failure")
            return super().lrange(key, start, end)

    redis_client = FlakyRedis()
    buffer_key = f"{main.BUFFER_PREFIX}web"
    redis_client.lists[buffer_key] = [
        _buffer_entry("container-1", "2026-09-09T12:00:01Z", "existing")
    ]
    container = FakeContainer(
        "web",
        "container-1",
        history=["2026-09-09T12:00:01Z existing\n"],
    )
    streamer = main.LogStreamer(
        client_factory=lambda: FakeDockerClient([container]),  # type: ignore[arg-type]
        redis_factory=lambda: redis_client,  # type: ignore[arg-type]
    )

    streamer.run_cycle()
    failed_thread, _container_id = streamer.active["web"]
    failed_thread.join(timeout=1)
    assert not failed_thread.is_alive()
    assert container.log_calls == []
    assert redis_client.published == []

    streamer.run_cycle()
    recovered_thread, _container_id = streamer.active["web"]
    recovered_thread.join(timeout=1)

    assert not recovered_thread.is_alive()
    assert len(container.log_calls) == 1
    assert redis_client.published == []
    assert _buffer_texts(redis_client, buffer_key) == ["existing"]
    assert "web" in streamer.attachment_failures
    assert "web" not in streamer.quick_eof_counts


def test_deduplication_window_stays_bounded() -> None:
    redis_client = FakeRedis()
    repeated = "2026-09-09T12:00:00Z repeated\n"
    unique = [
        f"2026-09-09T12:00:{second:02d}Z line-{second}\n"
        for second in range(1, main.BACKFILL_LINES + 1)
    ]
    container = FakeContainer(
        "web",
        "container-1",
        live=[repeated, *unique, repeated],
    )

    _tail_container("web", container, lambda _result: None, lambda: redis_client)

    repeated_publications = [
        line for _channel, line in redis_client.published if line == "repeated"
    ]
    assert repeated_publications == ["repeated", "repeated"]


def test_health_refresh_expires_lines_outside_five_minute_window(monkeypatch: Any) -> None:
    redis_client = FakeRedis()
    times = iter([0.0, 299.0])
    monkeypatch.setattr(main.time, "time", lambda: next(times))
    monkeypatch.setattr(main.time, "time_ns", lambda: 1)
    first = main.ParsedLogLine(text="first", identity="container:0:first", timestamp="0")
    second = main.ParsedLogLine(
        text="second",
        identity="container:299:second",
        timestamp="299",
    )

    main._publish_line(redis_client, "channel", "buffer", "web", first)
    main._publish_line(redis_client, "channel", "buffer", "web", second)
    assert redis_client.values[main.HEALTH_KEY] == "2"

    assert main.refresh_health(lambda: redis_client, now=301.0) == 1
    assert redis_client.values[main.HEALTH_KEY] == "1"
    assert main.refresh_health(lambda: redis_client, now=600.0) == 0
    assert main.HEALTH_KEY not in redis_client.values


def test_replacement_tail_preserves_new_lines_after_delayed_old_eof(
    monkeypatch: Any,
) -> None:
    redis_client = FakeRedis()
    buffer_key = f"{main.BUFFER_PREFIX}web"
    redis_client.lists[buffer_key] = ["legacy"]
    cutoff = main._timestamp_seconds("2026-09-09T12:00:04Z")
    assert cutoff is not None
    monkeypatch.setattr(main.time, "time", lambda: cutoff)

    old_release = threading.Event()
    old_holding = threading.Event()
    old_container = FakeContainer(
        "web",
        "old-container",
        history=["2026-09-09T12:00:01Z legacy\n"],
        hold_open=old_release,
        hold_started=old_holding,
    )
    docker_client = FakeDockerClient([old_container])
    streamer = main.LogStreamer(
        client_factory=lambda: docker_client,  # type: ignore[arg-type]
        redis_factory=lambda: redis_client,  # type: ignore[arg-type]
    )

    streamer.run_cycle()
    old_thread, _container_id = streamer.active["web"]
    assert old_holding.wait(timeout=1)

    new_container = FakeContainer(
        "web",
        "new-container",
        live=["2026-09-09T12:00:05Z new-line\n"],
    )
    docker_client.containers._containers = [new_container]
    streamer.run_cycle()
    new_thread, _container_id = streamer.active["web"]
    new_thread.join(timeout=1)
    assert not new_thread.is_alive()

    old_release.set()
    old_thread.join(timeout=1)
    assert not old_thread.is_alive()
    assert _buffer_texts(redis_client, buffer_key) == ["new-line", "legacy"]
    assert redis_client.published == [(f"{main.CHANNEL_PREFIX}web", "new-line")]
    assert streamer.tail_results.qsize() == 1


def test_replacement_discards_queued_old_tail_result() -> None:
    redis_client = FakeRedis()
    old_container = FakeContainer(
        "web",
        "old-container",
        error=RuntimeError("old attachment failed"),
    )
    docker_client = FakeDockerClient([old_container])
    streamer = main.LogStreamer(
        client_factory=lambda: docker_client,  # type: ignore[arg-type]
        redis_factory=lambda: redis_client,  # type: ignore[arg-type]
    )

    streamer.run_cycle()
    old_thread, _container_id = streamer.active["web"]
    old_thread.join(timeout=1)
    assert not old_thread.is_alive()
    assert streamer.tail_results.qsize() == 1

    new_release = threading.Event()
    new_container = FakeContainer(
        "web",
        "new-container",
        hold_open=new_release,
    )
    docker_client.containers._containers = [new_container]
    streamer.run_cycle()

    assert "web" not in streamer.attachment_failures
    assert "web" not in streamer.quick_eof_counts
    new_release.set()
    new_thread, _container_id = streamer.active["web"]
    new_thread.join(timeout=1)


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
    for _cycle in range(3):
        streamer.run_cycle()
        stale_thread, _container_id = streamer.active["web"]
        stale_thread.join(timeout=1)
        assert not stale_thread.is_alive()

    streamer.run_cycle()
    recovered_thread, _container_id = streamer.active["web"]
    for _attempt in range(100):
        if redis_client.published:
            break
        threading.Event().wait(0.01)

    assert stale_client.closed is True
    assert streamer.docker_client is recovered_client
    assert recovered_thread.is_alive()
    assert redis_client.published == [(f"{main.CHANNEL_PREFIX}web", "gap-line")]
    assert redis_client.values[main.HEALTH_STATUS_KEY] == "failed"
    assert "Recreated Docker client" in caplog.text

    streamer.active_started["web"] -= main.QUICK_EOF_THRESHOLD_SECONDS
    streamer.run_cycle()
    assert redis_client.values[main.HEALTH_STATUS_KEY] == "ok"

    stable_release.set()
    recovered_thread.join(timeout=1)


def test_stable_stream_clears_quick_eof_count_before_replacement() -> None:
    quick_container = FakeContainer("web", "container-1")
    docker_client = FakeDockerClient([quick_container])
    client_factory_calls = 0

    def client_factory() -> FakeDockerClient:
        nonlocal client_factory_calls
        client_factory_calls += 1
        return docker_client

    redis_client = FakeRedis()
    streamer = main.LogStreamer(
        client_factory=client_factory,  # type: ignore[arg-type]
        redis_factory=lambda: redis_client,  # type: ignore[arg-type]
    )

    streamer.run_cycle()
    first_thread, _container_id = streamer.active["web"]
    first_thread.join(timeout=1)
    streamer.run_cycle()
    second_thread, _container_id = streamer.active["web"]
    second_thread.join(timeout=1)

    stable_release = threading.Event()
    stable_container = FakeContainer(
        "web",
        "container-1",
        hold_open=stable_release,
    )
    docker_client.containers._containers = [stable_container]
    streamer.run_cycle()
    stable_thread, _container_id = streamer.active["web"]
    assert stable_thread.is_alive()
    assert streamer.quick_eof_counts["web"] == 2

    streamer.active_started["web"] -= main.QUICK_EOF_THRESHOLD_SECONDS
    streamer.run_cycle()
    assert "web" not in streamer.quick_eof_counts

    replacement = FakeContainer("web", "container-2")
    docker_client.containers._containers = [replacement]
    streamer.run_cycle()
    replacement_thread, _container_id = streamer.active["web"]
    replacement_thread.join(timeout=1)
    stable_release.set()
    stable_thread.join(timeout=1)
    streamer.run_cycle()

    assert streamer.quick_eof_counts["web"] == 1
    assert client_factory_calls == 1
    assert docker_client.closed is False


def test_attachment_failure_marks_manager_unhealthy() -> None:
    failing_container = FakeContainer(
        "web",
        "container-1",
        error=RuntimeError("attach failed"),
    )
    redis_client = FakeRedis()
    streamer = main.LogStreamer(
        client_factory=lambda: FakeDockerClient([failing_container]),  # type: ignore[arg-type]
        redis_factory=lambda: redis_client,  # type: ignore[arg-type]
    )

    streamer.run_cycle()
    failed_thread, _container_id = streamer.active["web"]
    failed_thread.join(timeout=1)
    streamer.run_cycle()

    assert redis_client.values[main.HEALTH_STATUS_KEY] == "failed"
