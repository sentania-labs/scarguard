"""Regression test for FDY-0563: per-service Redis ACL access control.

This test spins up a disposable Redis instance, creates ACL users with
per-service permissions, and verifies that:

1. Each service can only perform the operations it needs.
2. The web service cannot publish detection events (prevents forged events).
3. The log-streamer cannot erase safety state (pause/detention state).
4. Healthy pub/sub flows still work when using the correct ACL user.
5. Legacy admin-password auth still works (backwards compatibility).

The test uses a real Redis subprocess so the Redis server itself validates
the ACL boundaries - not mocks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import pytest

# ---------------------------------------------------------------------------
# Fixture: a disposable Redis instance with ACL support.
# ---------------------------------------------------------------------------

redis_process: subprocess.Popen | None = None
redis_password: str = ""


def _start_redis() -> str:
    """Start a standalone Redis 7 server in a temp directory.

    Returns the admin password for the default user.
    """
    global redis_process, redis_password

    # Generate admin password
    redis_password = uuid.uuid4().hex

    tmpdir = tempfile.mkdtemp(prefix="sg-redis-test-")

    # Use `redis-server` from PATH (the installed redis package)
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "redis", "server",
            "--aclfile", os.path.join(tmpdir, "scarguard-acl.txt"),
            "--requirepass", redis_password,
            "--maxmemory", "64mb",
            "--maxmemory-policy", "allkeys-lru",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait for server to be ready
    wait_ok = False
    for _ in range(50):
        time.sleep(0.1)
        try:
            import redis as redis_lib
            client = redis_lib.Redis(password=redis_password, decode_responses=True)
            if client.ping():
                wait_ok = True
                client.close()
                break
        except Exception:
            pass

    if not wait_ok:
        proc.kill()
        proc.wait()
        raise RuntimeError("Redis server did not start in time")

    redis_process = proc
    return tmpdir


def _stop_redis() -> None:
    global redis_process
    if redis_process is not None:
        redis_process.terminate()
        try:
            redis_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            redis_process.kill()
            redis_process.wait()
        redis_process = None


def _write_acl_file(tmpdir: str, passwords: dict[str, str]) -> str:
    """Write the ACL file and return its path."""
    acl_path = os.path.join(tmpdir, "scarguard-acl.txt")
    lines: list[str] = [
        "# Scarguard FDY-0563 ACL",
        f"user default on >{redis_password} ~* +@all",
        "",
    ]
    for username, cmds, keys, channels in [
        (
            "detector",
            ["PUBLISH", "GET", "SET", "SUBSCRIBE", "PING"],
            ["scarguard:detector:state"],
            ["scarguard:detections", "scarguard:detector:command"],
        ),
        (
            "notifier",
            ["SUBSCRIBE", "GET", "PING"],
            ["scarguard:health"],
            ["scarguard:detections", "scarguard:health"],
        ),
        (
            "deterrent",
            ["SUBSCRIBE", "PUBLISH", "GET", "SET", "DEL", "EVAL", "PING"],
            ["scarguard:off-watchdog:lease:*"],
            ["scarguard:detections", "scarguard:actuations", "scarguard:metrics:drops"],
        ),
        (
            "web",
            ["SUBSCRIBE", "PUBLISH", "GET", "INCR", "PING"],
            ["scarguard:rl:*", "scarguard:logs:*"],
            ["scarguard:detections"],
        ),
        (
            "log-streamer",
            ["PUBLISH", "LPUSH", "LRANGE", "LTRIM", "DEL", "PING"],
            ["scarguard:logs:buffer:*"],
            ["scarguard:logs:*"],
        ),
        (
            "backup",
            ["SUBSCRIBE", "PUBLISH", "PING"],
            [],
            ["scarguard:backup:trigger"],
        ),
    ]:
        pw = passwords.get(username, redis_password)
        cmd_str = " ".join(cmds)
        key_str = " ".join(f"&{k}" for k in keys)
        ch_str = " ".join(f"&{k}" for k in channels)
        lines.append(f"user {username} on {cmd_str} {key_str} {ch_str}")
        lines.append(f"user {username} on >{pw}")
        lines.append("")

    acl_content = "\n".join(lines)
    with open(acl_path, "w") as f:
        f.write(acl_content)
    os.chmod(acl_path, 0o600)
    return acl_path


@pytest.fixture(scope="module")
def redis_env():
    """Start a Redis instance with Scarguard ACL file.

    Yields: (tmpdir, admin_password, passwords_map)
    """
    tmpdir = _start_redis()
    from shared.acl_init import generate_user_passwords

    passwords = generate_user_passwords(redis_password)
    _write_acl_file(tmpdir, passwords)

    # Reload ACL so Redis picks up the file
    import redis as redis_lib
    admin = redis_lib.Redis(password=redis_password, decode_responses=True)
    admin.config_set("aclfile", os.path.join(tmpdir, "scarguard-acl.txt"))
    admin.acl_reload()
    admin.close()

    yield tmpdir, redis_password, passwords

    _stop_redis()


# ---------------------------------------------------------------------------
# Helper: create a Redis client with a specific user/password.
# ---------------------------------------------------------------------------

def _make_redis_client(user: str | None, password: str, host: str = "localhost", port: int = 6379):
    """Create a Redis client optionally using ACL user auth."""
    import redis as redis_lib
    kwargs: dict = {"host": host, "port": port, "decode_responses": True}
    if user:
        kwargs["username"] = user
    kwargs["password"] = password
    return redis_lib.Redis(**kwargs)


# ---------------------------------------------------------------------------
# AC2 tests: adversarial failure cases exercised against real code/artifacts.
# ---------------------------------------------------------------------------

class TestWebCannotForgeDetectionEvents:
    """AC2: The web service must not be able to publish detection events.

    In the original architecture the web service had the same Redis password
    as the detector, so any attacker who compromised the web container could
    publish a fake detection event and trigger notifications / deterrents.
    """

    def test_web_cannot_publish_detection_event(self, redis_env):
        _, admin_pw, passwords = redis_env
        web_client = _make_redis_client("web", passwords["web"])
        try:
            with pytest.raises(Exception):
                web_client.publish("scarguard:detections", json.dumps({"class_name": "fake_heron"}))
        except Exception as exc:
            # Redis ACL denial should raise an error (not success)
            assert "NOPERM" in str(exc) or "operation not permitted" in str(exc).lower() or "denied" in str(exc).lower(), (
                f"Expected ACL error but got: {exc}"
            )

    def test_web_can_subscribe_to_detections(self, redis_env):
        """Web should be allowed to SUBSCRIBE to scarguard:detections (SSE stream)."""
        _, admin_pw, passwords = redis_env
        web_client = _make_redis_client("web", passwords["web"])
        # This should NOT raise - the web ACL includes SUBSCRIBE for detections channel
        try:
            pubsub = web_client.pubsub()
            pubsub.subscribe("scarguard:detections")
            pubsub.unsubscribe("scarguard:detections")
            pubsub.close()
            web_client.close()
        except Exception as exc:
            pytest.fail(f"Web service should be allowed to subscribe to detections, but got: {exc}")


class TestLogStreamerCannotEraseSafetyState:
    """AC2: Log-streamer must not be able to touch safety state keys.

    SG-02 / SG-03: The log-streamer publishes log lines and manages buffers.
    Without ACLs it could accidentally (or maliciously) delete detector state,
    pause state, or activation leases.
    """

    def test_log_streamer_cannot_delete_detector_state(self, redis_env):
        """Log-streamer should not be able to delete detector:state keys."""
        _, admin_pw, passwords = redis_env
        ls_client = _make_redis_client("log-streamer", passwords["log-streamer"])
        # Set a detector state key (simulating detector writing it)
        import redis as redis_lib
        admin = redis_lib.Redis(password=admin_pw, decode_responses=True)
        admin.set("scarguard:detector:state", json.dumps({"state": "running"}))
        admin.close()

        # Log-streamer should NOT be able to delete it
        try:
            with pytest.raises(Exception):
                ls_client.delete("scarguard:detector:state")
        except Exception as exc:
            assert "NOPERM" in str(exc) or "denied" in str(exc).lower(), (
                f"Expected ACL error deleting detector:state, got: {exc}"
            )

    def test_log_streamer_cannot_delete_lease_keys(self, redis_env):
        """Log-streamer should not be able to delete off-watchdog:lease keys."""
        _, admin_pw, passwords = redis_env
        ls_client = _make_redis_client("log-streamer", passwords["log-streamer"])
        try:
            with pytest.raises(Exception):
                ls_client.delete("scarguard:off-watchdog:lease:sprinkler")
        except Exception as exc:
            assert "NOPERM" in str(exc) or "denied" in str(exc).lower(), (
                f"Expected ACL error deleting lease, got: {exc}"
            )

    def test_log_streamer_can_publish_logs(self, redis_env):
        """Log-streamer should be able to publish to scarguard:logs:*."""
        _, admin_pw, passwords = redis_env
        ls_client = _make_redis_client("log-streamer", passwords["log-streamer"])
        try:
            ls_client.publish("scarguard:logs:web", "INFO: health check ok")
            ls_client.close()
        except Exception as exc:
            pytest.fail(f"Log-streamer should be allowed to publish logs, got: {exc}")


class TestNotifierCannotPublishEvents:
    """AC2: The notifier is a pure subscriber - it should not be able to publish."""

    def test_notifier_cannot_publish(self, redis_env):
        """Notifier should not have PUBLISH capability."""
        _, admin_pw, passwords = redis_env
        notifier_client = _make_redis_client("notifier", passwords["notifier"])
        try:
            with pytest.raises(Exception):
                notifier_client.publish("scarguard:detections", "forbidden")
        except Exception as exc:
            assert "NOPERM" in str(exc) or "denied" in str(exc).lower(), (
                f"Expected ACL error, got: {exc}"
            )


class TestDetectorCannotSubscribeToBackupChannel:
    """AC2: The detector should not need to subscribe to backup channels."""

    def test_web_cannot_trigger_backup(self, redis_env):
        """Web service should not have SUBSCRIBE access to backup trigger channel."""
        _, admin_pw, passwords = redis_env
        web_client = _make_redis_client("web", passwords["web"])
        try:
            # Web should have SUBSCRIBE capability but not to backup channels
            # Actually the web ACL may include SUBSCRIBE to detections - that's fine.
            # But web should not be able to publish to backup:trigger.
            with pytest.raises(Exception):
                web_client.publish("scarguard:backup:trigger", json.dumps({"type": "manual"}))
        except Exception as exc:
            assert "NOPERM" in str(exc) or "denied" in str(exc).lower(), (
                f"Expected ACL error publishing to backup:trigger, got: {exc}"
            )


# ---------------------------------------------------------------------------
# Healthy flow tests: pub/sub and reconnect still work with ACL users.
# ---------------------------------------------------------------------------

class TestHealthyPubSubFlows:
    """AC2: Existing healthy flows must remain functional with per-service ACLs."""

    def test_detector_can_publish_and_notifier_can_receive(self, redis_env):
        """Detector publishes to scarguard:detections; notifier receives it."""
        _, admin_pw, passwords = redis_env

        detector_client = _make_redis_client("detector", passwords["detector"])
        notifier_client = _make_redis_client("notifier", passwords["notifier"])

        test_event = json.dumps({
            "class_name": "great_blue_heron",
            "confidence": 0.95,
            "camera_name": "pond-north",
            "timestamp": "2026-01-15T10:30:00Z",
            "snapshot_path": "/data/snapshots/img.jpg",
        })

        try:
            detector_client.publish("scarguard:detections", test_event)
            received: list[str] = []

            pubsub = notifier_client.pubsub()
            pubsub.subscribe("scarguard:detections")
            message = pubsub.get_message(ignore_subscribe_messages=True, timeout=2.0)
            if message and message["type"] == "message":
                received.append(message["data"])

            pubsub.unsubscribe("scarguard:detections")
            pubsub.close()
            detector_client.close()
            notifier_client.close()

            assert len(received) == 1, f"Notifier should have received the event, got: {received}"
            assert json.loads(received[0])["class_name"] == "great_blue_heron"
        except Exception as exc:
            # Clean up on failure
            try:
                notifier_client.close()
                detector_client.close()
            except Exception:
                pass
            pytest.fail(f"Healthy pub/sub flow failed: {exc}")

    def test_admin_password_still_works(self, redis_env):
        """Legacy admin-password auth must still work (backwards compatibility)."""
        _, admin_pw, _ = redis_env
        admin_client = _make_redis_client(None, admin_pw)
        try:
            assert admin_client.ping() is True
            admin_client.set("test:key", "test:value")
            assert admin_client.get("test:key") == "test:value"
        finally:
            admin_client.close()


class TestSharedRedisClientFactory:
    """Test the updated shared/redis_client.py with redis_user parameter."""

    def test_make_sync_client_with_user(self, redis_env):
        """make_sync_client accepts redis_user parameter and connects."""
        from shared.redis_client import make_sync_client

        _, admin_pw, passwords = redis_env

        # With redis_user - should use ACL user auth
        client = make_sync_client(
            redis_cfg={"host": "localhost", "port": 6379},
            redis_user="detector",
            socket_connect_timeout=2,
        )
        try:
            assert client.ping() is True
            client.set("test:user_auth", "works")
            assert client.get("test:user_auth") == "works"
        finally:
            client.close()

    def test_make_sync_client_without_user(self, redis_env):
        """make_sync_client without redis_user uses legacy admin auth."""
        from shared.redis_client import make_sync_client

        _, admin_pw, _ = redis_env

        # No redis_user - legacy admin mode
        os.environ["REDIS_PASSWORD"] = admin_pw
        try:
            client = make_sync_client(
                redis_cfg={"host": "localhost", "port": 6379},
                socket_connect_timeout=2,
            )
            try:
                assert client.ping() is True
            finally:
                client.close()
        finally:
            del os.environ["REDIS_PASSWORD"]


class TestReconnectLoopWithACL:
    """Test the shared reconnect_loop with ACL user auth."""

    def test_reconnect_loop_subscribes_with_user(self, redis_env):
        """reconnect_loop works with redis_user parameter."""
        from shared.redis_client import reconnect_loop

        _, admin_pw, passwords = redis_env
        detector_client = _make_redis_client("detector", passwords["detector"])

        handler_calls: list[tuple[str, dict]] = []

        def handler(channel: str, payload: dict) -> None:
            handler_calls.append((channel, payload))

        shutdown_event = threading.Event()

        # Start reconnect_loop in a thread
        t = threading.Thread(
            target=reconnect_loop,
            args=({"host": "localhost", "port": 6379}, ["scarguard:detections"], handler, shutdown_event),
            kwargs={"redis_user": "detector"},
        )
        t.daemon = True
        t.start()

        # Give it time to connect
        time.sleep(1)

        # Publish a message
        try:
            detector_client.publish("scarguard:detections", json.dumps({"test": True}))
            time.sleep(0.5)
            assert len(handler_calls) == 1, f"Expected 1 handler call, got {len(handler_calls)}"
            assert handler_calls[0][1] == {"test": True}
        finally:
            shutdown_event.set()
            t.join(timeout=5)
            detector_client.close()


class TestBackupCannotBeForgedByLogStreamer:
    """AC2: Log-streamer should not be able to trigger backups."""

    def test_log_streamer_cannot_publish_backup_trigger(self, redis_env):
        """Log-streamer must not be able to publish to backup:trigger."""
        _, admin_pw, passwords = redis_env
        ls_client = _make_redis_client("log-streamer", passwords["log-streamer"])
        try:
            with pytest.raises(Exception):
                ls_client.publish("scarguard:backup:trigger", json.dumps({"type": "manual"}))
        except Exception as exc:
            assert "NOPERM" in str(exc) or "denied" in str(exc).lower(), (
                f"Expected ACL error, got: {exc}"
            )


class TestBackupSubscriber:
    """Test that the backup service can subscribe to its trigger channel."""

    def test_backup_can_subscribe_to_trigger(self, redis_env):
        """Backup service should be able to subscribe to scarguard:backup:trigger."""
        _, admin_pw, passwords = redis_env
        backup_client = _make_redis_client("backup", passwords["backup"])
        try:
            pubsub = backup_client.pubsub()
            pubsub.subscribe("scarguard:backup:trigger")
            pubsub.unsubscribe("scarguard:backup:trigger")
            pubsub.close()
            backup_client.close()
        except Exception as exc:
            pytest.fail(f"Backup service should be able to subscribe to trigger, got: {exc}")


# ---------------------------------------------------------------------------
# AC1 test: per-service access replaces shared credential.
# ---------------------------------------------------------------------------

class TestPerServiceAccess:
    """AC1: Verify each service has its own Redis credential and permissions."""

    def test_web_cannot_access_detector_keys(self, redis_env):
        """Web should not be able to modify detector state keys."""
        _, admin_pw, passwords = redis_env
        web_client = _make_redis_client("web", passwords["web"])
        try:
            with pytest.raises(Exception):
                web_client.set("scarguard:detector:state", "corrupted")
        except Exception as exc:
            assert "NOPERM" in str(exc) or "denied" in str(exc).lower(), (
                f"Expected ACL error, got: {exc}"
            )

    def test_web_cannot_access_lease_keys(self, redis_env):
        """Web should not be able to access off-watchdog lease keys."""
        _, admin_pw, passwords = redis_env
        web_client = _make_redis_client("web", passwords["web"])
        try:
            with pytest.raises(Exception):
                web_client.get("scarguard:off-watchdog:lease:sprinkler")
        except Exception as exc:
            assert "NOPERM" in str(exc) or "denied" in str(exc).lower(), (
                f"Expected ACL error, got: {exc}"
            )

    def test_web_can_read_rate_limit_keys(self, redis_env):
        """Web should be able to read/write rate limit keys."""
        _, admin_pw, passwords = redis_env
        web_client = _make_redis_client("web", passwords["web"])
        try:
            web_client.incr("scarguard:rl:test-fire:user:1")
            web_client.ttl("scarguard:rl:test-fire:user:1")
            web_client.close()
        except Exception as exc:
            pytest.fail(f"Web should be able to access rate limit keys, got: {exc}")

    def test_deterrent_can_manage_leases(self, redis_env):
        """Deterrent should be able to manage activation lease keys."""
        _, admin_pw, passwords = redis_env
        det_client = _make_redis_client("deterrent", passwords["deterrent"])
        try:
            det_client.set("scarguard:off-watchdog:lease:sprinkler", "test-value")
            val = det_client.get("scarguard:off-watchdog:lease:sprinkler")
            assert val == "test-value"
            det_client.delete("scarguard:off-watchdog:lease:sprinkler")
        finally:
            det_client.close()
