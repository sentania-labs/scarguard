"""Opt-in real-browser regression checks: SCARGUARD_BROWSER_TESTS=1 pytest this file.

Requires Playwright and Chromium. Runs an isolated local web app, never the pond.
"""

import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('SCARGUARD_BROWSER_TESTS') != '1', reason='opt-in browser suite')


@pytest.fixture
def browser_app(tmp_path: Path) -> Iterator[tuple[Any, str, dict[str, str], Path]]:
    import auth
    import yaml
    from playwright.sync_api import sync_playwright

    src = Path(__file__).resolve().parents[1] / 'src'
    root = src.parents[2]
    snapshots = tmp_path / 'snapshots'
    snapshots.mkdir()
    (snapshots / 'frame.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="800" height="500"><rect width="800" height="500" fill="#334f58"/><ellipse cx="560" cy="270" rx="60" ry="30" fill="#a8b9c2"/></svg>')
    config = {
        'system': {'armed': False, 'timezone': 'America/Chicago', 'auth': {'enabled': True}},
        'cameras': [{'name': 'pond', 'rtsp_url': 'rtsp://example.invalid/fixture', 'enabled': False}],
        'detection': {'target_classes': ['heron', 'duck', 'plant']},
        'redis': {'host': '127.0.0.1', 'port': 1},
        'notifications': {'channels': [{'name': 'example', 'type': 'email', 'enabled': False}]},
    }
    (tmp_path / 'config.yml').write_text(yaml.safe_dump(config))
    auth_path = str(tmp_path / 'auth.db')
    auth.init_db(auth_path)
    with auth.get_db(auth_path) as conn:
        sessions = {role: auth.create_session(conn, auth.create_user(conn, role, secrets.token_urlsafe(24), role=role)) for role in ('admin', 'viewer')}
    with sqlite3.connect(tmp_path / 'events.db') as conn:
        conn.execute('''CREATE TABLE detection_events (id INTEGER PRIMARY KEY, timestamp TEXT,
            class_name TEXT, confidence REAL, camera_name TEXT, snapshot_path TEXT,
            actions_triggered TEXT, bbox TEXT, frame_size TEXT, feedback TEXT,
            corrected_class TEXT, corrected_bbox TEXT)''')
        for i in range(1, 57):
            conn.execute('''INSERT INTO detection_events VALUES (?, '2026-09-21T14:00:00+00:00',
                'plant', .8, 'pond', ?, '[]', '[10,20,250,350]', '[800,500]', NULL, NULL, NULL)''',
                (i, str(snapshots / 'frame.svg')))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'PYTHONPATH': f'{src}:{root / "shared"}',
           'CONFIG_PATH': str(tmp_path / 'config.yml'), 'DB_PATH': str(tmp_path / 'events.db'),
           'AUTH_DB_PATH': auth_path, 'SNAPSHOT_DIR': str(snapshots),
           'MODELS_DIR': str(tmp_path / 'models'), 'CSRF_SECRET_PATH': str(tmp_path / 'csrf'),
           'SECRET_KEY_PATH': str(tmp_path / 'secret'), 'BOOTSTRAP_TOKEN_PATH': str(tmp_path / 'bootstrap')}
    with (tmp_path / 'web.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', str(port), '--lifespan', 'off'], env=env, stdout=log, stderr=log)
        try:
            for _ in range(100):
                with socket.socket() as probe:
                    if probe.connect_ex(('127.0.0.1', port)) == 0:
                        break
                if process.poll() is not None:
                    pytest.fail((tmp_path / 'web.log').read_text())
                time.sleep(.05)
            with sync_playwright() as playwright:
                executable = os.environ.get('SCARGUARD_CHROME')
                browser = playwright.chromium.launch(executable_path=executable, headless=True, args=['--no-sandbox'])
                context = browser.new_context(viewport={'width': 1440, 'height': 1050})
                base = f'http://127.0.0.1:{port}'
                context.add_cookies([{'name': 'session', 'value': sessions['admin'], 'url': base}])
                page = context.new_page()
                # Deterministic live arrivals without an external Redis service.
                page.add_init_script('''window.EventSource = class {
                    constructor(url) { this.url=url; this.listeners={}; if(url==='/events/stream') window.eventFeed=this; }
                    addEventListener(name, fn) { this.listeners[name]=fn; }
                    close() {}
                };''')
                yield page, base, sessions, tmp_path
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_bulk_box_and_readonly_browser(browser_app: tuple[Any, str, dict[str, str], Path]) -> None:
    page, base, sessions, tmp_path = browser_app
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(base + '/events')
    assert page.locator('.event-select').count() == 50
    page.locator('#select-all-events').click()
    assert page.locator('.event-select:checked').count() == 50
    page.locator('#select-no-events').click()
    assert page.locator('.event-select:checked').count() == 0
    page.locator('.event-select').nth(0).check()
    page.locator('.event-select').nth(2).check()
    page.locator('[data-bulk-feedback="wrong_class"]').click()
    page.locator('#bulk-corrected-class').fill('heron')
    page.locator('#apply-feedback').click()
    page.get_by_text('2 events updated.', exact=True).wait_for()
    page.locator('#event-row-56 .badge-wrong').wait_for()
    page.reload()
    assert page.locator('#event-row-56 .badge-wrong').inner_text() == 'Wrong: Heron'
    assert page.locator('#event-row-55 .badge-wrong').count() == 0
    page.locator('#event-row-56 .snapshot-link').click()
    page.get_by_role('button', name='Redraw box', exact=True).click()
    rect = page.locator('dialog img').bounding_box()
    page.mouse.move(rect['x'] + rect['width'] * .82, rect['y'] + rect['height'] * .85)
    page.mouse.down()
    page.mouse.move(rect['x'] + rect['width'] * .59, rect['y'] + rect['height'] * .25, steps=8)
    page.mouse.up()
    assert page.locator('dialog').is_visible()
    assert 'not saved' in page.locator('.event-correction-status').inner_text()
    page.locator('dialog input').fill('duck')
    page.route('**/events/56/feedback', lambda route: route.fulfill(status=503, content_type='application/json', body='{"detail":"Save unavailable"}'))
    page.get_by_role('button', name='Save correction', exact=True).click()
    page.get_by_text('Save unavailable', exact=True).wait_for()
    assert page.locator('.snapshot-overlay__corrected-bbox').is_visible()
    assert page.locator('dialog input').input_value() == 'duck'
    page.unroute('**/events/56/feedback')
    page.get_by_role('button', name='Save correction', exact=True).click()
    page.get_by_text('Saved. You can close this image or continue reviewing.', exact=True).wait_for()
    page.screenshot(path=str(tmp_path / 'saved-correction.png'))
    page.get_by_role('button', name='Close', exact=True).click()
    page.reload()
    page.locator('#event-row-56 .snapshot-link').click()
    assert page.locator('dialog input').input_value() == 'duck'
    assert page.locator('.snapshot-overlay__corrected-bbox').is_visible()
    page.get_by_role('button', name='Close', exact=True).click()
    page.locator('#event-row-55 button[title="Correct detection"]').click()
    page.locator('#event-row-55 .badge-correct').wait_for()
    page.locator('#event-row-54 .event-select').check()
    assert page.locator('#select-no-events').is_enabled()
    page.locator('#select-no-events').click()
    for role in ['admin', 'viewer']:
        page.context.add_cookies([{'name': 'session', 'value': sessions[role], 'url': base}])
        page.goto(base + '/config')
        page.wait_for_selector('.camera-card', state='attached')
        if not page.locator('#expert-mode-toggle').is_checked():
            page.locator('label').filter(has=page.locator('#expert-mode-toggle')).click()
        page.locator('button[data-subtab="notifications"]').click()
        visible = page.locator('[data-requires-admin]').evaluate_all('(els)=>els.filter(e=>e.getClientRects().length>0).length')
        assert (visible == 0) if role == 'viewer' else (visible > 0)
        if role == 'viewer':
            page.locator('button[data-subtab="advanced"]').click()
            assert not page.locator('#cert-upload-btn').is_visible()
            assert page.locator('#expert-mode-toggle').is_enabled()
    assert not errors


def test_live_refresh_does_not_erase_other_row_editor(browser_app: tuple[Any, str, dict[str, str], Path]) -> None:
    page, base, _, _ = browser_app
    page.goto(base + '/events')
    for event_id in (56, 55):
        page.locator(f'#event-row-{event_id} button[title="Wrong class"]').click()
        page.locator(f'#event-row-{event_id} input[name="corrected_class"]').fill('heron')
    page.locator('#event-row-56 .wrong-class-picker button').click()
    page.locator('#event-row-56 .badge-wrong').wait_for()
    page.locator('h1').click()
    page.evaluate("window.eventFeed.listeners.detection({data:'{}'})")
    page.wait_for_timeout(800)
    assert page.locator('#event-row-55 input[name="corrected_class"]').input_value() == 'heron'
    assert page.locator('#event-row-55 .wrong-class-picker').is_visible()


def test_batch_refresh_waits_for_an_older_read(browser_app: tuple[Any, str, dict[str, str], Path]) -> None:
    page, base, _, _ = browser_app
    page.goto(base + '/events')
    intercepted = []
    page.route('**/events', lambda route: intercepted.append(route))
    page.locator('h1').click()
    page.evaluate("window.eventFeed.listeners.detection({data:'{}'})")
    page.wait_for_timeout(800)
    assert len(intercepted) == 1
    old_response = intercepted[0].fetch()
    page.locator('#event-row-56 .event-select').check()
    page.locator('[data-bulk-feedback="correct"]').click()
    page.locator('#apply-feedback').click()
    page.get_by_text('1 events updated.', exact=True).wait_for()
    intercepted[0].fulfill(response=old_response)
    page.wait_for_timeout(100)
    assert len(intercepted) == 2
    intercepted[1].continue_()
    page.locator('#event-row-56 .badge-correct').wait_for()
    assert page.locator('#event-row-56 .badge-correct').inner_text() == 'Correct'
