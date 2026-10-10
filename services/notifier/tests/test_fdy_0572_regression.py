"""FDY-0572 regression test (finding SG-33): a slow notification channel
cannot block the other channels or the Redis subscriber.

Every case drives the production code - ``EmailNotifier``, ``DiscordNotifier``,
``main.dispatch``, ``main.subscribe_loop``, ``ChannelDispatcher`` and the
on-disk retry queue - against local fixture servers from conftest (imported
relative to this directory so the file runs unchanged from the checkout and
from /app/tests in the notifier image). ``StalledServer`` is an SMTP relay
that accepts the TCP connection and never sends its banner, which is exactly
the relay that used to hold the subscriber for the whole socket timeout.

On the tree before this change ``channel_dispatcher`` does not exist and
``dispatch`` sends inline on the caller's thread, so the module fails to
import and every case fails.

Placeholder credentials are assembled at runtime from parts so that no
credential-shaped literal appears in the source.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import notification_queue
import pytest
from channel_dispatcher import ChannelDispatcher
from conftest import SAMPLE_EVENT, CertSet, FakeNet, HTTPFixture, SMTPFixture, make_certs
from discord import DiscordNotifier
from email_notifier import EmailNotifier
from main import build_notifiers, dispatch, subscribe_loop
from notification_queue import NotificationQueue

SMTP_HOST = "smtp.example"
SMTP_IP = "93.184.216.34"
DISCORD_HOST = "discord.example"
DISCORD_IP = "162.159.135.232"

CANARY = "canary-" + "fdy0572"
SMTP_PASS = "mail-" + CANARY


def _event(n: int) -> dict[str, Any]:
    return {**SAMPLE_EVENT, "confidence": 0.5 + n / 100, "seq": n}


def wait_until(pred: Callable[[], bool], timeout: float, step: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def unstall(stalled: StalledServer, dispatcher: ChannelDispatcher) -> None:
    """Drop the relay so every abandoned send ends before the test returns."""
    stalled.close()
    workers = [dispatcher.worker(name) for name in dispatcher.snapshot()]
    assert wait_until(lambda: all(w is None or not w.stalled for w in workers), 5)


# ── Fixture servers specific to this finding ────────────────────────────────


class StalledServer:
    """Accepts TCP connections and never says a word (an SMTP relay that hangs)."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self.connections: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            self.connections.append(conn)

    def close(self) -> None:
        """Drop every held connection - the stalled sends then fail at once."""
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=2)
        for conn in self.connections:
            try:
                conn.close()
            except OSError:
                pass


class DelayProxy:
    """Forwards to *target_port* after holding each connection for *delay* seconds."""

    def __init__(self, target_port: int, delay: float) -> None:
        self._target = ("127.0.0.1", target_port)
        self._delay = delay
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            threading.Thread(target=self._pipe, args=(conn,), daemon=True).start()

    def _pipe(self, client: socket.socket) -> None:
        time.sleep(self._delay)
        try:
            upstream = socket.create_connection(self._target, timeout=5)
        except OSError:
            client.close()
            return

        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
        pump(upstream, client)

    def close(self) -> None:
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=2)


@pytest.fixture()
def stalled_smtp(net: FakeNet) -> Iterator[StalledServer]:
    srv = StalledServer()
    net.dns[SMTP_HOST] = [SMTP_IP]
    net.routes[(SMTP_IP, 587)] = ("127.0.0.1", srv.port)
    yield srv
    srv.close()


@pytest.fixture()
def discord_server(net: FakeNet, http_server: HTTPFixture) -> HTTPFixture:
    net.dns[DISCORD_HOST] = [DISCORD_IP]
    net.routes[(DISCORD_IP, 80)] = ("127.0.0.1", http_server.port)
    return http_server


@pytest.fixture()
def smtp_certs(tmp_path: Path) -> CertSet:
    return make_certs(tmp_path / "certs", SMTP_HOST)


@pytest.fixture()
def working_smtp(net: FakeNet, smtp_certs: CertSet) -> Iterator[SMTPFixture]:
    srv = SMTPFixture(smtp_certs, starttls=True)
    net.dns[SMTP_HOST] = [SMTP_IP]
    net.routes[(SMTP_IP, 587)] = ("127.0.0.1", srv.port)
    yield srv
    srv.close()


def email_channel(ca_file: str | None = None, **overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "name": "email",
        "type": "email",
        "smtp_host": SMTP_HOST,
        "smtp_port": 587,
        "smtp_user": "guard@example.com",
        "smtp_pass": SMTP_PASS,
        "to_addresses": ["pond@example.com"],
        "include_snapshot": False,
    }
    if ca_file:
        cfg["smtp_ca_file"] = ca_file
    cfg.update(overrides)
    return cfg


def discord_channel() -> dict[str, Any]:
    return {
        "name": "discord",
        "type": "discord",
        "webhook_url": f"http://{DISCORD_HOST}/api/webhooks/pond/hook",
        "include_snapshot": False,
    }


def make_queue(tmp_path: Path, **kwargs: Any) -> tuple[NotificationQueue, Path]:
    path = tmp_path / "state" / "notification_queue.json"
    return NotificationQueue(str(path), **kwargs), path


def read_queue_file(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    assert isinstance(data, list)
    assert not path.with_name(path.name + ".tmp").exists(), "temporary save file left behind"
    return data


# ── Blocked SMTP while another channel completes ────────────────────────────


class TestStalledEmailDoesNotBlockOthers:
    def test_discord_delivers_and_email_hits_deadline(
        self, tmp_path: Path, stalled_smtp: StalledServer, discord_server: HTTPFixture,
    ) -> None:
        email = EmailNotifier(email_channel())
        discord = DiscordNotifier(discord_channel())
        queue, path = make_queue(tmp_path)
        dispatcher = ChannelDispatcher(queue, send_deadline=1.0, retry_interval=0.2, stop_grace=0.5)
        dispatcher.sync([email, discord])
        lock = threading.Lock()

        started = time.monotonic()
        dispatch(_event(1), [email, discord], lock, queue, dispatcher)
        assert time.monotonic() - started < 1.0, "dispatch must not wait on the SMTP relay"

        # Discord completes while the relay still holds the email connection open.
        assert wait_until(lambda: len(discord_server.requests) == 1, 3)
        assert wait_until(lambda: len(stalled_smtp.connections) == 1, 3)
        snap = dispatcher.snapshot()
        assert snap["discord"]["delivered"] == 1
        assert snap["email"]["delivered"] == 0
        assert b"Great Blue Heron" in discord_server.requests[0].body

        # The email attempt is bounded by the deadline: it is queued for retry
        # (an observable outcome) long before the 15 s socket timeout.
        assert wait_until(lambda: queue.depth_by_type().get("email") == 1, 3)
        assert wait_until(lambda: dispatcher.snapshot()["email"]["timed_out"] == 1, 3)
        snap = dispatcher.snapshot()
        assert snap["email"]["stalled"] is True
        assert snap["discord"]["stalled"] is False

        # A second event: Discord still flows, email is deferred without a new
        # connection being stacked on the stalled relay.
        dispatch(_event(2), [email, discord], lock, queue, dispatcher)
        assert wait_until(lambda: len(discord_server.requests) == 2, 3)
        assert wait_until(lambda: queue.depth_by_type().get("email") == 2, 3)
        assert wait_until(lambda: dispatcher.snapshot()["email"]["deferred"] == 1, 3)
        assert len(stalled_smtp.connections) == 1

        persisted = read_queue_file(path)
        assert [e["notifier_type"] for e in persisted] == ["email", "email"]
        assert [e["event"]["seq"] for e in persisted] == [1, 2]
        assert all(e["attempt"] == 0 for e in persisted)
        dispatcher.stop()
        unstall(stalled_smtp, dispatcher)

    def test_subscriber_keeps_consuming_while_smtp_stalls(
        self, tmp_path: Path, stalled_smtp: StalledServer, discord_server: HTTPFixture,
    ) -> None:
        email = EmailNotifier(email_channel())
        discord = DiscordNotifier(discord_channel())
        notifiers: list[Any] = [email, discord]
        queue, path = make_queue(tmp_path)
        dispatcher = ChannelDispatcher(queue, send_deadline=30, retry_interval=60, stop_grace=0.5)
        dispatcher.sync(notifiers)
        shutdown = threading.Event()
        consumed: list[float] = []
        total = 5

        class FakePubSub:
            def subscribe(self, *args: Any) -> None:
                pass

            def listen(self) -> Iterator[dict[str, Any]]:
                for n in range(total):
                    consumed.append(time.monotonic())
                    yield {
                        "type": "message",
                        "channel": "scarguard:detections",
                        "data": json.dumps(_event(n)),
                    }
                shutdown.wait(10)
                yield {"type": "message", "channel": "scarguard:detections", "data": "{}"}

            def unsubscribe(self) -> None:
                pass

            def close(self) -> None:
                pass

        class FakeRedis:
            def pubsub(self) -> FakePubSub:
                return FakePubSub()

            def close(self) -> None:
                pass

        def run() -> None:
            with patch("main.redis_lib.Redis", return_value=FakeRedis()), \
                 patch("event_signing.load_key_from_env", return_value=None):
                subscribe_loop({}, notifiers, threading.Lock(), shutdown, queue, None, dispatcher=dispatcher)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            # Every message is taken off the bus and every Discord alert is out
            # while the first email send is still hanging on the relay.
            assert wait_until(lambda: len(consumed) == total, 3)
            assert consumed[-1] - consumed[0] < 2.0
            assert wait_until(lambda: len(discord_server.requests) == total, 5)
            assert wait_until(lambda: len(stalled_smtp.connections) == 1, 3)
            email_worker = dispatcher.worker("email")
            assert email_worker is not None
            assert wait_until(lambda: email_worker.depth == total - 1, 3)
            assert queue.depth == 0
        finally:
            shutdown.set()
            thread.join(timeout=5)
        assert not thread.is_alive()

        # Shutdown with one send in flight and four waiting: all five are on disk.
        # A second stop() from another thread (main() after the subscriber
        # returns) must wait for the first drain rather than return early.
        second_stop_done: list[float] = []

        def second_stop() -> None:
            dispatcher.stop()
            second_stop_done.append(time.monotonic())

        started = time.monotonic()
        threading.Timer(0.05, second_stop).start()
        dispatcher.stop()
        first_done = time.monotonic()
        assert first_done - started < 2.0
        assert wait_until(lambda: bool(second_stop_done), 2)
        assert second_stop_done[0] >= first_done - 0.01
        persisted = read_queue_file(path)
        assert sorted(e["event"]["seq"] for e in persisted) == list(range(total))
        assert {e["notifier_type"] for e in persisted} == {"email"}
        assert dispatcher.snapshot()["email"]["persisted"] == total
        unstall(stalled_smtp, dispatcher)


# ── Bounded saturation ──────────────────────────────────────────────────────


class TestBoundedSaturation:
    def test_overflow_spills_to_a_bounded_retry_queue(
        self,
        tmp_path: Path,
        stalled_smtp: StalledServer,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(notification_queue, "_MAX_QUEUE_SIZE", 8)
        email = EmailNotifier(email_channel())
        queue, path = make_queue(tmp_path)
        dispatcher = ChannelDispatcher(
            queue, max_pending=3, send_deadline=30, retry_interval=60, stop_grace=0.5,
        )
        dispatcher.sync([email])
        lock = threading.Lock()

        caplog.set_level(logging.INFO)
        started = time.monotonic()
        for n in range(20):
            dispatch(_event(n), [email], lock, queue, dispatcher)
        # Well under the relay's 15 s socket timeout; the overflowed events each
        # cost one fsync'd rewrite of the retry file, nothing more.
        assert time.monotonic() - started < 3.0, "a saturated channel must not block the caller"

        snap = dispatcher.snapshot()
        assert snap["email"]["submitted"] == 20
        assert snap["email"]["pending"] <= 3
        # 20 events, at most one in flight and three waiting: the rest overflowed.
        assert snap["email"]["overflowed"] in (16, 17)
        assert wait_until(lambda: len(stalled_smtp.connections) == 1, 3)
        snap = dispatcher.snapshot()
        assert snap["email"]["overflowed"] + snap["email"]["pending"] + 1 == 20
        # The retry queue is itself bounded and drop-oldest, and says so.
        assert queue.depth == 8
        assert len(read_queue_file(path)) == 8
        assert "delivery queue full" in caplog.text
        assert "Queue full (8 items) - dropped oldest entry" in caplog.text
        dispatcher.stop()
        assert queue.depth == 8
        unstall(stalled_smtp, dispatcher)


# ── Finite deadline with a slow-but-working relay ───────────────────────────


class TestDeadline:
    def test_late_success_cancels_the_retry(
        self,
        tmp_path: Path,
        net: FakeNet,
        smtp_certs: CertSet,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        relay = SMTPFixture(smtp_certs, starttls=True)
        proxy = DelayProxy(relay.port, delay=1.2)
        net.dns[SMTP_HOST] = [SMTP_IP]
        net.routes[(SMTP_IP, 587)] = ("127.0.0.1", proxy.port)
        try:
            email = EmailNotifier(email_channel(str(smtp_certs.ca_pem)))
            queue, path = make_queue(tmp_path)
            dispatcher = ChannelDispatcher(queue, send_deadline=0.4, retry_interval=60, stop_grace=3)
            dispatcher.sync([email])
            caplog.set_level(logging.INFO)

            dispatch(_event(1), [email], threading.Lock(), queue, dispatcher)
            # Deadline passes first: queued for retry and visible on disk.
            assert wait_until(lambda: queue.depth == 1, 3)
            assert wait_until(lambda: dispatcher.snapshot()["email"]["timed_out"] == 1, 3)
            assert read_queue_file(path)[0]["notifier_type"] == "email"
            # The slow relay then accepts the mail: the retry is cancelled.
            assert wait_until(lambda: any(s.messages for s in relay.sessions), 10)
            assert wait_until(lambda: queue.depth == 0, 3)
            assert read_queue_file(path) == []
            assert wait_until(lambda: "retry cancelled" in caplog.text, 3)
            snap = dispatcher.snapshot()
            assert snap["email"]["delivered"] == 1
            assert snap["email"]["stalled"] is False
            assert relay.sessions[0].tls is True
            dispatcher.stop()
        finally:
            proxy.close()
            relay.close()


# ── Retry file: atomic writes under interruption ────────────────────────────


class TestRetryFilePersistence:
    def test_interrupted_save_leaves_previous_queue_readable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        email = EmailNotifier(email_channel())
        queue, path = make_queue(tmp_path)
        queue.enqueue(_event(1), email)
        before = path.read_bytes()
        assert [e["event"]["seq"] for e in read_queue_file(path)] == [1]

        # Interrupt the next save after the temporary file is written but
        # before it is renamed into place.
        real_fsync = os.fsync
        hits = {"n": 0}

        def failing_fsync(fd: int) -> None:
            hits["n"] += 1
            raise OSError("simulated power loss")

        monkeypatch.setattr(notification_queue.os, "fsync", failing_fsync)
        queue.enqueue(_event(2), email)
        assert hits["n"] == 1
        assert path.read_bytes() == before, "an interrupted save must not touch the queue file"
        assert not path.with_name(path.name + ".tmp").exists()
        monkeypatch.setattr(notification_queue.os, "fsync", real_fsync)

        # A restart reads the last complete save.
        reloaded = NotificationQueue(str(path))
        assert [e.event["seq"] for e in reloaded._entries] == [1]

        # The next successful save writes both entries atomically.
        seen_during_replace: list[list[int]] = []
        real_replace = os.replace

        def observed_replace(src: Any, dst: Any) -> None:
            seen_during_replace.append([e["event"]["seq"] for e in json.loads(Path(dst).read_text())])
            real_replace(src, dst)

        monkeypatch.setattr(notification_queue.os, "replace", observed_replace)
        queue.enqueue(_event(3), email)
        assert seen_during_replace == [[1]], "queue file still complete right up to the rename"
        assert [e["event"]["seq"] for e in read_queue_file(path)] == [1, 2, 3]

    def test_stale_temporary_file_is_ignored_and_removed(self, tmp_path: Path) -> None:
        email = EmailNotifier(email_channel())
        queue, path = make_queue(tmp_path)
        queue.enqueue(_event(1), email)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("[{\"truncated")
        reloaded = NotificationQueue(str(path))
        assert reloaded.depth == 1
        assert not tmp.exists()


# ── Shutdown with work in flight, then restart recovery ─────────────────────


class TestShutdownAndRestart:
    def test_pending_and_inflight_survive_restart(
        self, tmp_path: Path, net: FakeNet, smtp_certs: CertSet, caplog: pytest.LogCaptureFixture,
    ) -> None:
        stalled = StalledServer()
        net.dns[SMTP_HOST] = [SMTP_IP]
        net.routes[(SMTP_IP, 587)] = ("127.0.0.1", stalled.port)
        email = EmailNotifier(email_channel(str(smtp_certs.ca_pem)))
        queue, path = make_queue(tmp_path)
        dispatcher = ChannelDispatcher(queue, max_pending=10, send_deadline=30, retry_interval=60, stop_grace=0.5)
        dispatcher.sync([email])
        lock = threading.Lock()
        caplog.set_level(logging.INFO)
        try:
            for n in range(3):
                dispatch(_event(n), [email], lock, queue, dispatcher)
            worker = dispatcher.worker("email")
            assert worker is not None
            assert wait_until(lambda: len(stalled.connections) == 1 and worker.depth == 2, 3)

            # Shutdown: bounded by the grace, and everything is on disk afterwards,
            # including the send that is still hanging on the relay.
            started = time.monotonic()
            dispatcher.stop()
            assert time.monotonic() - started < 2.0
            on_disk = read_queue_file(path)
            assert sorted(e["event"]["seq"] for e in on_disk) == [0, 1, 2]
            assert "persisted 2 pending notification(s)" in caplog.text
            assert "shutdown reached while a delivery attempt was still running" in caplog.text

            # An event arriving after the stop is persisted too, never attempted.
            dispatch(_event(3), [email], lock, queue, dispatcher)
            assert sorted(e["event"]["seq"] for e in read_queue_file(path)) == [0, 1, 2, 3]
            assert len(stalled.connections) == 1
        finally:
            # The hanging send now fails (relay goes away) - no second retry entry.
            stalled.close()
        assert wait_until(lambda: not worker.stalled and worker.snapshot()["failed"] == 1, 5)
        assert queue.depth == 4

        # Restart 40 s later against a healthy relay: the new process loads the
        # file and the email worker delivers every entry over STARTTLS.
        relay = SMTPFixture(smtp_certs, starttls=True)
        net.routes[(SMTP_IP, 587)] = ("127.0.0.1", relay.port)
        try:
            later = time.time() + 40
            restarted_queue = NotificationQueue(str(path), clock=lambda: later)
            assert restarted_queue.depth == 4
            assert restarted_queue.depth_by_type() == {"email": 4}
            email_after = EmailNotifier(email_channel(str(smtp_certs.ca_pem)))
            restarted = ChannelDispatcher(restarted_queue, retry_interval=0.2, stop_grace=2)
            restarted.sync([email_after])
            assert wait_until(lambda: restarted_queue.depth == 0, 15)
            delivered = [m for s in relay.sessions for m in s.messages]
            assert len(delivered) == 4
            assert all(b"Great Blue Heron" in m for m in delivered)
            assert all(s.tls and s.auth_over_tls for s in relay.sessions if s.messages)
            assert read_queue_file(path) == []
            assert restarted.snapshot()["email"]["retried"] == 4
            assert restarted.snapshot()["email"]["delivered"] == 4
            restarted.stop()
        finally:
            relay.close()


# ── Healthy flows and configuration are unchanged ───────────────────────────


class TestHealthyFlows:
    def test_email_and_discord_deliver_from_channel_config(
        self, tmp_path: Path, working_smtp: SMTPFixture, discord_server: HTTPFixture, smtp_certs: CertSet,
    ) -> None:
        notifiers = build_notifiers({
            "channels": [email_channel(str(smtp_certs.ca_pem)), discord_channel()],
        })
        assert [type(n).__name__ for n in notifiers] == ["EmailNotifier", "DiscordNotifier"]
        queue, path = make_queue(tmp_path)
        dispatcher = ChannelDispatcher(queue, retry_interval=60, stop_grace=2)
        dispatcher.sync(notifiers)

        dispatch(_event(1), notifiers, threading.Lock(), queue, dispatcher)
        assert wait_until(lambda: any(s.messages for s in working_smtp.sessions), 10)
        assert wait_until(lambda: len(discord_server.requests) == 1, 5)
        session = next(s for s in working_smtp.sessions if s.messages)
        assert session.tls is True
        assert session.auth == ("guard@example.com", SMTP_PASS) and session.auth_over_tls
        assert b"Subject: ScarGuard: Great Blue Heron detected" in session.messages[0]
        assert json.loads(discord_server.requests[0].body)["content"].startswith("**Great Blue Heron")
        assert queue.depth == 0
        assert not path.exists()
        snap = dispatcher.snapshot()
        assert snap["email"]["delivered"] == 1 and snap["discord"]["delivered"] == 1
        assert snap["email"]["failed"] == snap["email"]["timed_out"] == 0
        dispatcher.stop()

    def test_rules_still_route_to_named_channels_only(
        self, tmp_path: Path, working_smtp: SMTPFixture, discord_server: HTTPFixture, smtp_certs: CertSet,
    ) -> None:
        notifiers = build_notifiers({
            "channels": [email_channel(str(smtp_certs.ca_pem)), discord_channel()],
        })
        queue, _ = make_queue(tmp_path)
        dispatcher = ChannelDispatcher(queue, retry_interval=60, stop_grace=2)
        dispatcher.sync(notifiers)
        dispatch({**_event(1), "actions_triggered": ["discord"]}, notifiers, threading.Lock(), queue, dispatcher)
        assert wait_until(lambda: len(discord_server.requests) == 1, 5)
        dispatch({**_event(2), "actions_triggered": None}, notifiers, threading.Lock(), queue, dispatcher)
        time.sleep(0.3)
        assert dispatcher.snapshot()["email"]["submitted"] == 0
        assert dispatcher.snapshot()["discord"]["submitted"] == 1
        assert not any(s.messages for s in working_smtp.sessions)
        dispatcher.stop()

    def test_config_reload_swaps_sender_and_retires_removed_channel(
        self, tmp_path: Path, stalled_smtp: StalledServer, discord_server: HTTPFixture,
    ) -> None:
        email = EmailNotifier(email_channel())
        discord = DiscordNotifier(discord_channel())
        queue, path = make_queue(tmp_path)
        dispatcher = ChannelDispatcher(queue, max_pending=10, send_deadline=30, retry_interval=60, stop_grace=0.5)
        dispatcher.sync([email, discord])
        lock = threading.Lock()
        for n in range(3):
            dispatch(_event(n), [email], lock, queue, dispatcher)
        worker = dispatcher.worker("email")
        assert worker is not None
        assert wait_until(lambda: worker.depth == 2, 3)

        # Reload without the email channel: its waiting events are persisted,
        # the Discord worker keeps running with the new sender object.
        discord_after = DiscordNotifier(discord_channel())
        dispatcher.sync([discord_after])
        assert dispatcher.worker("email") is None
        assert sorted(e["event"]["seq"] for e in read_queue_file(path)) == [1, 2]
        discord_worker = dispatcher.worker("discord")
        assert discord_worker is not None and discord_worker.notifier is discord_after
        dispatch(_event(9), [discord_after], lock, queue, dispatcher)
        assert wait_until(lambda: len(discord_server.requests) == 1, 3)
        # A dispatch still holding the pre-reload snapshot cannot resurrect the
        # removed channel or re-point the live one: the event goes to the retry
        # queue and the Discord worker keeps the reloaded sender.
        dispatch(_event(7), [email, discord], lock, queue, dispatcher)
        assert dispatcher.worker("email") is None
        assert discord_worker.notifier is discord_after
        assert wait_until(lambda: len(discord_server.requests) == 2, 3)
        assert sorted(e["event"]["seq"] for e in read_queue_file(path)) == [1, 2, 7]
        dispatcher.stop()
        # The retired channel's in-flight send is persisted at stop as well.
        assert sorted(e["event"]["seq"] for e in read_queue_file(path)) == [0, 1, 2, 7]
        unstall(stalled_smtp, dispatcher)
        assert wait_until(lambda: not worker.stalled, 5)
