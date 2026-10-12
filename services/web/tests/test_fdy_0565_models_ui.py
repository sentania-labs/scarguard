"""FDY-0565 / SG-07: model uploads become candidates; promotion/rollback in the Models UI.

Drives the real FastAPI routes and Jinja template with CPU-only fixture
checkpoints and asserts on the rendered HTML and on the files on disk.
"""

from __future__ import annotations

import io
import os
import pickle
import re
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Any

import pytest

LEGACY = b"legacy live model - provenance unknown"


class _Evil:
    def __reduce__(self):  # noqa: ANN204 - pickle protocol hook
        return (os.system, ("echo pwned",))


def checkpoint(payload: object | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        data = payload if payload is not None else {"model": OrderedDict(w=[1.0])}
        # Fixed timestamp: the same payload must always give the same bytes.
        info = zipfile.ZipInfo("best/data.pkl", (2026, 1, 1, 0, 0, 0))
        archive.writestr(info, pickle.dumps(data, protocol=2))
    return buf.getvalue()


@pytest.fixture()
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from routes import models as models_mod

    live_dir = tmp_path / "models"
    live_dir.mkdir()
    (live_dir / "best.pt").write_bytes(LEGACY)  # detection.model_path in MOCK_CONFIG
    store = tmp_path / "store"
    monkeypatch.setattr(models_mod, "MODELS_DIR", live_dir)
    monkeypatch.setattr(models_mod, "MODEL_STORE_DIR", store)
    audits: list[dict[str, Any]] = []
    real_record = models_mod.audit.record_request

    def spy(request: Any, **kwargs: Any) -> None:
        audits.append(kwargs)
        real_record(request, **kwargs)

    monkeypatch.setattr(models_mod.audit, "record_request", spy)
    return {"live": live_dir, "store": store, "audits": audits}


def _upload(client: Any, name: str, content: bytes) -> Any:
    return client.post(
        "/models",
        files={"file": (name, content, "application/octet-stream")},
        follow_redirects=True,
    )


def _candidate_ids(html: str) -> list[str]:
    return re.findall(r'action="/models/candidates/([0-9a-f]{32})/promote"', html)


def test_upload_is_candidate_and_promotion_rollback_render(client: Any, dirs: dict) -> None:
    live = dirs["live"] / "best.pt"

    page = _upload(client, "best.pt", checkpoint())  # duplicate of the live name
    assert page.status_code == 200
    assert "Uploaded as a candidate" in page.text
    assert live.read_bytes() == LEGACY
    (candidate_id,) = _candidate_ids(page.text)
    assert 'name="target_name" value="best.pt"' in page.text
    row = page.text.split('data-name="best.pt"', 1)[1].split("</tr>", 1)[0]
    assert "active" in row and 'data-status="unresolved"' in row

    promoted = client.post(
        f"/models/candidates/{candidate_id}/promote",
        data={"target_name": "best.pt"},
        follow_redirects=True,
    )
    assert promoted.status_code == 200
    assert "Candidate promoted." in promoted.text
    assert live.read_bytes() == checkpoint()
    row = promoted.text.split('data-name="best.pt"', 1)[1].split("</tr>", 1)[0]
    assert 'data-status="recorded"' in row and candidate_id[:12] in row
    (rollback_id,) = re.findall(r'action="/models/rollback/([0-9a-f]{32})"', promoted.text)
    assert "<strong>promote</strong>" in promoted.text
    assert [a["action"] for a in dirs["audits"]] == ["model.candidate_upload", "model.promote"]

    restored = client.post(f"/models/rollback/{rollback_id}", follow_redirects=True)
    assert restored.status_code == 200
    assert "Rollback copy restored." in restored.text
    assert live.read_bytes() == LEGACY
    assert "<strong>rollback</strong>" in restored.text
    assert dirs["audits"][-1]["action"] == "model.rollback"
    # The live row attributes the bytes to the copy that was restored, not to
    # the snapshot the restore took of the promoted file it replaced.
    row = restored.text.split('data-name="best.pt"', 1)[1].split("</tr>", 1)[0]
    assert 'data-status="recorded"' in row and f"of copy <code>{rollback_id[:12]}" in row
    new_copies = set(re.findall(r'action="/models/rollback/([0-9a-f]{32})"', restored.text))
    (candidate_copy,) = new_copies - {rollback_id}
    assert candidate_copy[:12] not in row
    assert dirs["audits"][-1]["details"]["restored_rollback_id"] == rollback_id


def test_discard_ledger_failure_keeps_candidate_listed(
    client: Any, dirs: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routes import models as models_mod

    (candidate_id,) = _candidate_ids(_upload(client, "pond.pt", checkpoint()).text)
    real_store = models_mod._store

    def failing_store() -> Any:
        store = real_store()
        monkeypatch.setattr(store, "_append_history", _raise_unwritable, raising=False)
        return store

    monkeypatch.setattr(models_mod, "_store", failing_store)
    page = client.post(f"/models/candidates/{candidate_id}/discard")
    assert page.status_code == 400
    assert "Discard failed" in page.text
    assert _candidate_ids(page.text) == [candidate_id]
    assert (dirs["store"] / "candidates" / candidate_id / "manifest.json").is_file()
    assert not (dirs["store"] / "history.jsonl").exists()
    assert dirs["audits"][-1]["action"] == "model.candidate_upload"


def _raise_unwritable(_record: dict) -> None:
    raise OSError("history not writable")


def test_malicious_upload_rejected_and_not_stored(client: Any, dirs: dict) -> None:
    page = _upload(client, "best.pt", checkpoint({"x": _Evil()}))
    assert "Upload rejected" in page.text and "disallowed code" in page.text
    assert _candidate_ids(page.text) == []
    assert (dirs["live"] / "best.pt").read_bytes() == LEGACY
    assert not list((dirs["store"] / "candidates").iterdir())
    assert not list((dirs["store"] / ".staging").iterdir())
    assert dirs["audits"][-1]["action"] == "model.candidate_rejected"


def test_failed_promotion_shows_error_and_keeps_live(client: Any, dirs: dict) -> None:
    (candidate_id,) = _candidate_ids(_upload(client, "pond.pt", checkpoint()).text)
    page = client.post(
        f"/models/candidates/{candidate_id}/promote", data={"target_name": "../best.pt"}
    )
    assert page.status_code == 400
    assert "Promotion failed" in page.text
    assert (dirs["live"] / "best.pt").read_bytes() == LEGACY
    assert sorted(p.name for p in dirs["live"].iterdir()) == ["best.pt"]
    assert dirs["audits"][-1]["action"] == "model.promote_failed"


def test_discard_removes_candidate_only(client: Any, dirs: dict) -> None:
    (candidate_id,) = _candidate_ids(_upload(client, "pond.pt", checkpoint()).text)
    page = client.post(f"/models/candidates/{candidate_id}/discard", follow_redirects=True)
    assert "Candidate discarded." in page.text
    assert _candidate_ids(page.text) == []
    assert (dirs["live"] / "best.pt").read_bytes() == LEGACY


@pytest.fixture()
def role_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    import auth as auth_module

    cfg = {
        "system": {"armed": True, "auth": {"enabled": True}},
        "cameras": [],
        "detection": {"model_path": "/models/best.pt"},
        "redis": {"host": "localhost", "port": 6379},
    }
    users = {
        "admin-session": {"user_id": 1, "username": "pond-admin", "role": "admin", "disabled": 0},
        "viewer-session": {"user_id": 2, "username": "pond-viewer", "role": "viewer",
                           "disabled": 0},
    }
    monkeypatch.setattr("config_store.load", lambda: cfg)
    monkeypatch.setattr("config_store.load_cached", lambda **_kw: cfg)
    monkeypatch.setattr(auth_module, "validate_session", lambda _db, token: users.get(token))
    monkeypatch.setattr(auth_module, "users_exist", lambda _p: True)

    from fastapi.testclient import TestClient
    from main import app

    c = TestClient(app)
    c.headers["Accept"] = "text/html"
    c.get("/login", follow_redirects=False)
    c.headers["X-CSRF-Token"] = c.cookies.get("csrf_token", "")
    return c


def test_viewer_sees_no_controls_and_cannot_promote(role_client: Any, dirs: dict) -> None:
    role_client.cookies.set("session", "admin-session")
    (candidate_id,) = _candidate_ids(_upload(role_client, "best.pt", checkpoint()).text)
    assert dirs["audits"][-1]["action"] == "model.candidate_upload"

    role_client.cookies.set("session", "viewer-session")
    page = role_client.get("/models")
    assert page.status_code == 200
    assert candidate_id in page.text  # listed for review
    assert _candidate_ids(page.text) == []  # but no promote form
    assert "/discard" not in page.text
    denied = role_client.post(
        f"/models/candidates/{candidate_id}/promote",
        data={"target_name": "best.pt"},
        follow_redirects=False,
    )
    assert denied.status_code in (302, 403)
    assert (dirs["live"] / "best.pt").read_bytes() == LEGACY

    role_client.cookies.set("session", "admin-session")
    role_client.post(
        f"/models/candidates/{candidate_id}/promote", data={"target_name": "best.pt"}
    )
    page = role_client.get("/models")
    assert "by pond-admin" in page.text


def test_notice_text_cannot_be_injected_from_url(client: Any, dirs: dict) -> None:
    page = client.get("/models", params={"notice": "Promoted evil.pt"})
    assert "Promoted evil.pt" not in page.text
    page = client.get("/models", params={"uploaded": "evil.pt"})
    assert "Uploaded as a candidate" not in page.text


def test_training_job_result_points_at_candidate() -> None:
    from routes.training_jobs import _result_view

    view = _result_view({"train": {"candidate_id": "a" * 32, "candidate_name": "trained.pt"}})
    assert view["candidate_id"] == "a" * 32
    assert view["candidate_name"] == "trained.pt"
