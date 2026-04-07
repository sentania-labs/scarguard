"""Tests for v0.12.7 role/authz additions in services/web/src/auth.py.

Covers:
- ``role`` column backfill migration from legacy ``is_admin``
- ``create_user(role=...)`` writes both columns in sync
- ``update_user_role`` keeps is_admin in sync
- ``count_active_admins`` matches reality
- session + API token validation includes role
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile

import pytest


@pytest.fixture
def fresh_auth():
    """Yield the auth module bound to a brand-new temp DB."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # init_db will create it
    os.environ["AUTH_DB_PATH"] = path
    import auth  # noqa: WPS433

    importlib.reload(auth)
    auth.init_db()
    yield auth
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


@pytest.fixture
def legacy_auth():
    """Yield the auth module bound to a temp DB that mimics a pre-v0.12.7 schema.

    The users table exists but has NO ``role`` column — migration fills it.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Pre-seed with legacy schema and rows
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            disabled INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO users (username, password_hash, is_admin, created_at) VALUES
          ('legacy_admin', 'x', 1, '2025-01-01T00:00:00+00:00'),
          ('legacy_user',  'x', 0, '2025-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    os.environ["AUTH_DB_PATH"] = path
    import auth  # noqa: WPS433

    importlib.reload(auth)
    auth.init_db()
    yield auth
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


class TestRoleMigration:
    def test_fresh_db_has_role_column(self, fresh_auth):
        db = fresh_auth.get_db()
        try:
            cols = {r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()}
        finally:
            db.close()
        assert "role" in cols
        assert "is_admin" in cols  # legacy column still present

    def test_legacy_db_backfills_role_from_is_admin(self, legacy_auth):
        db = legacy_auth.get_db()
        try:
            rows = list(db.execute(
                "SELECT username, is_admin, role FROM users ORDER BY id"
            ))
        finally:
            db.close()
        assert rows[0]["username"] == "legacy_admin"
        assert rows[0]["role"] == "admin"
        assert rows[1]["username"] == "legacy_user"
        assert rows[1]["role"] == "user"

    def test_migration_is_idempotent(self, legacy_auth):
        legacy_auth.init_db()
        legacy_auth.init_db()
        db = legacy_auth.get_db()
        try:
            rows = list(db.execute("SELECT username, role FROM users ORDER BY id"))
        finally:
            db.close()
        assert rows[0]["role"] == "admin"
        assert rows[1]["role"] == "user"


class TestCreateUser:
    def test_create_admin_default(self, fresh_auth):
        db = fresh_auth.get_db()
        try:
            uid = fresh_auth.create_user(db, "scott", "password123")
            user = fresh_auth.get_user_by_id(db, uid)
        finally:
            db.close()
        assert user["role"] == "admin"
        assert user["is_admin"] == 1

    def test_create_user_via_legacy_is_admin_false(self, fresh_auth):
        db = fresh_auth.get_db()
        try:
            uid = fresh_auth.create_user(db, "alice", "password123", is_admin=False)
            user = fresh_auth.get_user_by_id(db, uid)
        finally:
            db.close()
        assert user["role"] == "user"
        assert user["is_admin"] == 0

    def test_create_viewer_via_role_kwarg(self, fresh_auth):
        db = fresh_auth.get_db()
        try:
            uid = fresh_auth.create_user(db, "bob", "password123", role="viewer")
            user = fresh_auth.get_user_by_id(db, uid)
        finally:
            db.close()
        assert user["role"] == "viewer"
        assert user["is_admin"] == 0  # viewer is NOT admin for legacy compat

    def test_explicit_role_admin_sets_legacy_flag(self, fresh_auth):
        db = fresh_auth.get_db()
        try:
            uid = fresh_auth.create_user(db, "scott", "password123", role="admin")
            user = fresh_auth.get_user_by_id(db, uid)
        finally:
            db.close()
        assert user["role"] == "admin"
        assert user["is_admin"] == 1

    def test_invalid_role_rejected(self, fresh_auth):
        db = fresh_auth.get_db()
        try:
            with pytest.raises(ValueError, match="invalid role"):
                fresh_auth.create_user(db, "evil", "password123", role="superuser")
        finally:
            db.close()


class TestUpdateRole:
    def test_update_role_keeps_is_admin_in_sync(self, fresh_auth):
        db = fresh_auth.get_db()
        try:
            uid = fresh_auth.create_user(db, "alice", "password123", role="user")
            fresh_auth.update_user_role(db, uid, "admin")
            u = fresh_auth.get_user_by_id(db, uid)
            assert u["role"] == "admin"
            assert u["is_admin"] == 1

            fresh_auth.update_user_role(db, uid, "viewer")
            u = fresh_auth.get_user_by_id(db, uid)
            assert u["role"] == "viewer"
            assert u["is_admin"] == 0

            fresh_auth.update_user_role(db, uid, "user")
            u = fresh_auth.get_user_by_id(db, uid)
            assert u["role"] == "user"
            assert u["is_admin"] == 0
        finally:
            db.close()

    def test_update_role_rejects_invalid(self, fresh_auth):
        db = fresh_auth.get_db()
        try:
            uid = fresh_auth.create_user(db, "alice", "password123", role="user")
            with pytest.raises(ValueError, match="invalid role"):
                fresh_auth.update_user_role(db, uid, "root")
        finally:
            db.close()


class TestCountActiveAdmins:
    def test_counts_only_active_admins(self, fresh_auth):
        db = fresh_auth.get_db()
        try:
            a = fresh_auth.create_user(db, "admin1", "password123", role="admin")
            fresh_auth.create_user(db, "admin2", "password123", role="admin")
            fresh_auth.create_user(db, "viewer", "password123", role="viewer")
            fresh_auth.create_user(db, "user", "password123", role="user")

            assert fresh_auth.count_active_admins(db) == 2

            # Disabling an admin drops the count
            fresh_auth.set_user_disabled(db, a, True)
            assert fresh_auth.count_active_admins(db) == 1

            # Non-admins don't count
            fresh_auth.create_user(db, "viewer2", "password123", role="viewer")
            assert fresh_auth.count_active_admins(db) == 1
        finally:
            db.close()


class TestAtomicLastAdminGuards:
    """The last-admin guards in auth.py use atomic UPDATE ... WHERE COUNT>1
    SQL predicates to avoid a race where two concurrent demotes both see
    count=2 and both proceed.  These tests exercise the single-threaded
    happy and refusal paths; true concurrency testing is out of scope."""

    def _seed_admins(self, auth_mod, *, count: int):
        db = auth_mod.get_db()
        ids = []
        for i in range(count):
            ids.append(auth_mod.create_user(db, f"admin{i}", "password123", role="admin"))
        db.close()
        return ids

    def test_try_demote_admin_refuses_last(self, fresh_auth):
        [admin_id] = self._seed_admins(fresh_auth, count=1)
        db = fresh_auth.get_db()
        try:
            ok = fresh_auth.try_demote_admin(db, admin_id, "viewer")
        finally:
            db.close()
        assert ok is False
        # Still an admin
        db = fresh_auth.get_db()
        try:
            assert fresh_auth.get_user_by_id(db, admin_id)["role"] == "admin"
        finally:
            db.close()

    def test_try_demote_admin_allows_when_multiple(self, fresh_auth):
        ids = self._seed_admins(fresh_auth, count=2)
        db = fresh_auth.get_db()
        try:
            ok = fresh_auth.try_demote_admin(db, ids[0], "viewer")
        finally:
            db.close()
        assert ok is True
        db = fresh_auth.get_db()
        try:
            assert fresh_auth.get_user_by_id(db, ids[0])["role"] == "viewer"
            # Other admin unchanged
            assert fresh_auth.get_user_by_id(db, ids[1])["role"] == "admin"
            # Count down to 1
            assert fresh_auth.count_active_admins(db) == 1
        finally:
            db.close()

    def test_try_demote_admin_on_non_admin_is_normal_update(self, fresh_auth):
        [admin_id] = self._seed_admins(fresh_auth, count=1)
        db = fresh_auth.get_db()
        try:
            viewer_id = fresh_auth.create_user(db, "v", "password123", role="viewer")
            # Last admin intact, and the target isn't admin, so this should just work
            ok = fresh_auth.try_demote_admin(db, viewer_id, "user")
            assert ok is True
            assert fresh_auth.get_user_by_id(db, viewer_id)["role"] == "user"
            # Promoting admin to admin is also a no-op success path
            ok = fresh_auth.try_demote_admin(db, admin_id, "admin")
            assert ok is True
        finally:
            db.close()

    def test_try_disable_admin_refuses_last(self, fresh_auth):
        [admin_id] = self._seed_admins(fresh_auth, count=1)
        db = fresh_auth.get_db()
        try:
            ok = fresh_auth.try_disable_admin(db, admin_id, True)
        finally:
            db.close()
        assert ok is False

    def test_try_disable_admin_allows_when_multiple(self, fresh_auth):
        ids = self._seed_admins(fresh_auth, count=2)
        db = fresh_auth.get_db()
        try:
            ok = fresh_auth.try_disable_admin(db, ids[0], True)
        finally:
            db.close()
        assert ok is True
        db = fresh_auth.get_db()
        try:
            assert fresh_auth.count_active_admins(db) == 1
        finally:
            db.close()

    def test_try_disable_admin_enable_always_works(self, fresh_auth):
        [admin_id] = self._seed_admins(fresh_auth, count=1)
        db = fresh_auth.get_db()
        try:
            fresh_auth.set_user_disabled(db, admin_id, True)
            # Re-enabling should always succeed (no danger of lockout)
            ok = fresh_auth.try_disable_admin(db, admin_id, False)
        finally:
            db.close()
        assert ok is True

    def test_try_delete_admin_refuses_last(self, fresh_auth):
        [admin_id] = self._seed_admins(fresh_auth, count=1)
        db = fresh_auth.get_db()
        try:
            ok = fresh_auth.try_delete_admin(db, admin_id)
        finally:
            db.close()
        assert ok is False

    def test_try_delete_admin_allows_when_multiple(self, fresh_auth):
        ids = self._seed_admins(fresh_auth, count=2)
        db = fresh_auth.get_db()
        try:
            ok = fresh_auth.try_delete_admin(db, ids[0])
        finally:
            db.close()
        assert ok is True
        db = fresh_auth.get_db()
        try:
            assert fresh_auth.get_user_by_id(db, ids[0]) is None
            assert fresh_auth.count_active_admins(db) == 1
        finally:
            db.close()

    def test_try_delete_non_admin_always_works(self, fresh_auth):
        [admin_id] = self._seed_admins(fresh_auth, count=1)
        db = fresh_auth.get_db()
        try:
            viewer_id = fresh_auth.create_user(db, "v", "password123", role="viewer")
            ok = fresh_auth.try_delete_admin(db, viewer_id)
        finally:
            db.close()
        assert ok is True


class TestSessionIncludesRole:
    def test_session_carries_role(self, fresh_auth):
        db = fresh_auth.get_db()
        try:
            uid = fresh_auth.create_user(db, "bob", "password123", role="viewer")
            token = fresh_auth.create_session(db, uid)
            user = fresh_auth.validate_session(db, token)
        finally:
            db.close()
        assert user is not None
        assert user["role"] == "viewer"
        assert user["is_admin"] == 0

    def test_api_token_inherits_role(self, fresh_auth):
        db = fresh_auth.get_db()
        try:
            uid = fresh_auth.create_user(db, "bob", "password123", role="viewer")
            raw = fresh_auth.create_api_token(db, uid, "test-token")
            result = fresh_auth.validate_api_token(db, raw)
        finally:
            db.close()
        assert result is not None
        assert result["role"] == "viewer"
