"""Event feedback persists as one bounded write, including exportable boxes."""

import io
import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

import db
import pytest
from fastapi.testclient import TestClient
from httpx import Response


@pytest.fixture
def event_db(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    path = tmp_path / 'events.db'
    monkeypatch.setattr(db, 'DB_PATH', str(path))
    snapshot = tmp_path / 'sample.jpg'
    snapshot.write_bytes(b'fixture image')
    with sqlite3.connect(path) as conn:
        conn.execute('''CREATE TABLE detection_events (
            id INTEGER PRIMARY KEY, timestamp TEXT, class_name TEXT, confidence REAL,
            camera_name TEXT, snapshot_path TEXT, actions_triggered TEXT,
            bbox TEXT, frame_size TEXT, feedback TEXT, corrected_class TEXT, corrected_bbox TEXT
        )''')
        for event_id in (1, 2, 3):
            conn.execute('''INSERT INTO detection_events VALUES
                (?, '2026-09-21T14:00:00+00:00', 'plant', .8, 'pond', ?, '[]',
                 '[10,20,30,40]', '[800,500]', NULL, NULL, NULL)''', (event_id, str(snapshot)))
    db.ensure_training_tables()
    # The generic smoke client mocks reads; restore real event reads here.
    import importlib
    real_db = importlib.util.spec_from_file_location('feedback_test_db', Path(db.__file__))
    module = importlib.util.module_from_spec(real_db)
    real_db.loader.exec_module(module)
    module.DB_PATH = str(path)
    monkeypatch.setattr(db, 'get_events', module.get_events)
    monkeypatch.setattr(db, 'count_events', module.count_events)
    return client


def batch(client: TestClient, ids: list[Any], feedback: str = 'wrong_class', corrected_class: str = 'heron') -> Response:
    return client.post('/events/feedback/batch', json={
        'event_ids': ids, 'feedback': feedback, 'corrected_class': corrected_class,
    })


def test_bulk_corrects_only_selection_and_preserves_boxes(event_db: TestClient) -> None:
    db.update_feedback(1, 'wrong_class', 'duck', '[400,100,600,400]')
    assert batch(event_db, [1, 2]).json() == {'updated': 2}
    assert db.get_event(1)['corrected_bbox'] == '[400,100,600,400]'
    assert db.get_event(2)['corrected_class'] == 'heron'
    assert db.get_event(3)['feedback'] is None


@pytest.mark.parametrize('feedback', ['correct', 'false_positive'])
def test_bulk_clears_obsolete_corrections(event_db: TestClient, feedback: str) -> None:
    db.update_feedback(1, 'wrong_class', 'duck', '[400,100,600,400]')
    assert batch(event_db, [1], feedback).status_code == 200
    row = db.get_event(1)
    assert row['feedback'] == feedback
    assert row['corrected_class'] is None
    assert row['corrected_bbox'] is None


def test_missing_event_rejects_entire_batch(event_db: TestClient) -> None:
    assert batch(event_db, [1, 999]).status_code == 409
    assert db.get_event(1)['feedback'] is None


@pytest.mark.parametrize('ids', [[], [1, 1], [True], ['1'], [0], list(range(1, 52))])
def test_invalid_selection_is_rejected(event_db: TestClient, ids: list[Any]) -> None:
    assert batch(event_db, ids).status_code == 422
    assert db.get_event(1)['feedback'] is None


@pytest.mark.parametrize('feedback,cls', [('unknown', ''), ('wrong_class', '  '), ('wrong_class', 'x' * 101)])
def test_invalid_feedback_is_rejected(event_db: TestClient, feedback: str, cls: str) -> None:
    assert batch(event_db, [1], feedback, cls).status_code == 422
    assert db.get_event(1)['feedback'] is None


def test_box_and_class_persist_reload_and_export(event_db: TestClient) -> None:
    response = event_db.post('/events/1/feedback', data={
        'feedback': 'wrong_class', 'corrected_class': ' heron ',
        'corrected_bbox': '[400,100,600,400]',
    })
    assert response.status_code == 200
    row = db.get_event(1)
    assert row['corrected_class'] == 'heron'
    assert json.loads(row['corrected_bbox']) == [400, 100, 600, 400]
    page = event_db.get('/events').text
    assert 'data-corrected-bbox="[400, 100, 600, 400]"' in page
    exported = event_db.get('/admin/training/export')
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as zf:
        assert zf.read('dataset/labels/train/1.txt') == b'0 0.625000 0.500000 0.250000 0.600000\n'
        assert b'heron' in zf.read('dataset/data.yaml')


@pytest.mark.parametrize('bbox', ['nope', '[1,2,3]', '[true,1,50,50]', '[0,0,NaN,50]', '[-1,0,50,50]', '[0,0,900,50]', '[40,20,30,40]'])
def test_bad_box_does_not_save_either_part(event_db: TestClient, bbox: str) -> None:
    response = event_db.post('/events/1/feedback', data={
        'feedback': 'wrong_class', 'corrected_class': 'heron', 'corrected_bbox': bbox,
    })
    assert response.status_code == 422
    assert db.get_event(1)['feedback'] is None


def test_single_label_edit_preserves_existing_box(event_db: TestClient) -> None:
    db.update_feedback(1, 'wrong_class', 'duck', '[400,100,600,400]')
    response = event_db.post('/events/1/feedback', data={'feedback': 'wrong_class', 'corrected_class': 'heron'})
    assert response.status_code == 200
    assert db.get_event(1)['corrected_bbox'] == '[400,100,600,400]'


def test_batch_requires_csrf(event_db: TestClient) -> None:
    del event_db.headers['X-CSRF-Token']
    assert batch(event_db, [1]).status_code == 403
    assert db.get_event(1)['feedback'] is None


def test_rate_limit_still_enforced_with_isolated_state(client: TestClient) -> None:
    for _ in range(30):
        assert client.post('/arm').status_code == 200
    result = client.post('/arm')
    assert result.status_code == 429
    assert result.headers['retry-after'] == '60'
