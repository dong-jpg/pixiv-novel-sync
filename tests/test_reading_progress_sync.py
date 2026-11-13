from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
from threading import Barrier
import uuid

import pytest

from pixiv_novel_sync.storage_db import Database


def seed(db):
    db.init_schema()
    db.conn.execute("INSERT INTO users(user_id,name,raw_json) VALUES(1,'Fixture','{}')")
    db.conn.execute("""INSERT INTO novels(novel_id,title,user_id,visible,restrict_value,
        x_restrict,text_length,total_bookmarks,total_views,tags_json,raw_json,meta_hash)
        VALUES(42,'Synthetic',1,1,'public',0,10,0,0,'[]','{}','fixture')""")


@pytest.fixture
def progress_db(tmp_path):
    db = Database(tmp_path / 'progress.db')
    seed(db)
    yield db
    db.close()


def write(db, percent, version, key=None, **kwargs):
    return db.compare_and_set_reading_progress(
        42, percent, 'reading', version, key or str(uuid.uuid4()), **kwargs
    )


def test_legacy_rows_get_a_stable_version_on_repeated_migration(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as connection:
        connection.execute('''CREATE TABLE reading_progress(novel_id INTEGER PRIMARY KEY,
            progress INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'unread',
            last_read_at TEXT,created_at TEXT,updated_at TEXT)''')
        connection.execute("INSERT INTO reading_progress VALUES(42,67,'reading','old','old','old')")
    db = Database(path)
    try:
        db.init_schema()
        first = db.get_reading_progress(42)
        db.init_schema()
        assert db.get_reading_progress(42) == first
        assert first['version'] == 1 and first['progress'] == 67 and first['is_reset'] == 0
    finally:
        db.close()


def test_two_devices_cannot_overwrite_the_same_version(progress_db):
    from pixiv_novel_sync.storage.reading_progress import ReadingProgressError

    first = write(progress_db, 70, 0)
    assert first['record']['version'] == first['applied_version'] == 1
    with pytest.raises(ReadingProgressError) as conflict:
        write(progress_db, 15, 0)
    assert conflict.value.code == 'progress_conflict'
    assert conflict.value.record['progress'] == 70
    # 明确选择回退/重读时可以用当前版本保存更低的进度。
    assert write(progress_db, 15, 1)['record']['progress'] == 15


def test_response_loss_receipt_does_not_hide_a_later_device_update(progress_db):
    from pixiv_novel_sync.storage.reading_progress import ReadingProgressError

    key = str(uuid.uuid4())
    write(progress_db, 30, 0, key)
    write(progress_db, 80, 1)
    replay = write(progress_db, 30, 0, key)
    assert replay['replayed'] is True and replay['applied_version'] == 1
    assert replay['record']['version'] == 2 and replay['record']['progress'] == 80
    with pytest.raises(ReadingProgressError, match='progress_idempotency_conflict'):
        write(progress_db, 40, 0, key)


def test_reset_tombstone_rejects_old_device_and_old_delete(progress_db):
    from pixiv_novel_sync.storage.reading_progress import ReadingProgressError

    write(progress_db, 90, 0)
    reset = progress_db.compare_and_set_reading_progress(42, 0, 'unread', 1, str(uuid.uuid4()), reset=True)
    assert reset['record']['version'] == 2 and reset['record']['is_reset'] is True
    with pytest.raises(ReadingProgressError, match='progress_conflict'):
        write(progress_db, 99, 1)
    with pytest.raises(ReadingProgressError, match='progress_conflict'):
        progress_db.compare_and_set_reading_progress(42, 0, 'unread', 1, str(uuid.uuid4()), reset=True)
    assert progress_db.progress_snapshot(42)['progress'] == 0


def test_reset_without_previous_progress_also_creates_a_version(progress_db):
    assert progress_db.progress_snapshot(42)['version'] == 0
    result = progress_db.compare_and_set_reading_progress(42, 0, 'unread', 0, str(uuid.uuid4()), reset=True)
    assert result['record']['version'] == 1 and result['record']['is_reset'] is True


def test_receipts_are_bounded_and_lost_old_receipt_cannot_reapply(progress_db):
    from pixiv_novel_sync.storage.reading_progress import ReadingProgressError

    first_key = str(uuid.uuid4())
    for version in range(40):
        write(progress_db, version, version, first_key if version == 0 else None)
    assert progress_db.conn.execute('SELECT count(*) FROM reading_progress_mutations').fetchone()[0] == 32
    with pytest.raises(ReadingProgressError, match='progress_conflict'):
        write(progress_db, 0, 0, first_key)
    progress_db.delete_novel(42)
    assert progress_db.conn.execute('SELECT count(*) FROM reading_progress_mutations').fetchone()[0] == 0


def test_independent_connections_race_for_one_version(progress_db):
    from pixiv_novel_sync.storage.reading_progress import ReadingProgressError

    barrier = Barrier(2)

    def attempt(percent):
        db = Database(progress_db.path)
        try:
            barrier.wait(timeout=5)
            return write(db, percent, 0)['record']['version']
        except ReadingProgressError as error:
            return error.code
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(attempt, [30, 70]))
    assert sorted(map(str, outputs)) == ['1', 'progress_conflict']


def test_no_ghost_progress_for_missing_novel(progress_db):
    from pixiv_novel_sync.storage.reading_progress import ReadingProgressError

    with pytest.raises(ReadingProgressError, match='novel_not_found'):
        progress_db.compare_and_set_reading_progress(404, 10, 'reading', 0, str(uuid.uuid4()))
    assert progress_db.conn.execute('SELECT count(*) FROM reading_progress').fetchone()[0] == 0


@pytest.fixture
def web(tmp_path, monkeypatch):
    from pixiv_novel_sync.webapp import create_app

    monkeypatch.setenv('DASHBOARD_TOKEN', 'progress-test-password')
    monkeypatch.setenv('PIXIV_FLASK_SECRET', 'progress-test-flask-secret')
    monkeypatch.setenv('PIXIV_REFRESH_TOKEN', 'progress-test-refresh')
    monkeypatch.delenv('PIXIV_REMOTE_ENABLED', raising=False)
    env = tmp_path / 'empty.env'
    env.write_text('')
    db = Database(Path(os.environ['PIXIV_DB_PATH']))
    seed(db)
    db.close()
    app = create_app(config_path=str(tmp_path / 'missing.yaml'), env_path=str(env), start_scheduler=False)
    app.config.update(TESTING=True, RAW_HTTP=True)
    client = app.test_client()
    assert client.post('/api/auth/login', data={'token': 'progress-test-password'}).status_code == 302
    csrf = client.get('/api/csrf-token').get_json()['csrf_token']
    return client, {'X-CSRF-Token': csrf}


def test_http_versions_csrf_and_no_cache(web):
    client, headers = web
    path = '/api/dashboard/novels/42/progress'
    response = client.get(path)
    assert response.get_json()['version'] == 0
    assert 'no-store' in response.headers['Cache-Control']
    body = {'progress': 50, 'status': 'reading', 'base_version': 0, 'mutation_id': str(uuid.uuid4())}
    assert client.post(path, json=body).status_code == 403
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200 and response.get_json()['record']['version'] == 1
    body['mutation_id'] = str(uuid.uuid4())
    stale = client.post(path, json=body, headers=headers)
    assert stale.status_code == 409 and stale.get_json()['record']['progress'] == 50
    assert client.post(path, json={'progress': 5, 'status': 'reading'}, headers=headers).status_code == 428
    assert client.delete(path, headers=headers).status_code == 428
    reset = client.delete(path, json={'base_version': 1, 'mutation_id': str(uuid.uuid4())}, headers=headers)
    assert reset.status_code == 200 and reset.get_json()['record']['is_reset'] is True
    assert client.get('/api/dashboard/novels/404/progress').status_code == 404


@pytest.mark.parametrize('field,value', [('progress', True), ('progress', -1), ('progress', 101),
    ('progress', 1.2), ('status', {}), ('status', 'unknown'), ('base_version', True),
    ('base_version', -1), ('mutation_id', 'not-a-uuid'), ('extra', 'command')])
def test_http_rejects_invalid_fields_without_write(web, field, value):
    client, headers = web
    body = {'progress': 40, 'status': 'reading', 'base_version': 0, 'mutation_id': str(uuid.uuid4())}
    body[field] = value
    path = '/api/dashboard/novels/42/progress'
    assert client.post(path, json=body, headers=headers).status_code == 400
    assert client.get(path).get_json()['version'] == 0


def test_http_bounded_duplicate_json_and_reset_fields(web):
    client, headers = web
    path = '/api/dashboard/novels/42/progress'
    response = client.post(path, data=b' ' * 4097, headers=dict(headers, **{'Content-Type': 'application/json'}))
    assert response.status_code == 413
    response = client.post(path, data='{"progress":1,"progress":2}', headers=dict(headers, **{'Content-Type': 'application/json'}))
    assert response.status_code == 400
    body = {'base_version': 0, 'mutation_id': str(uuid.uuid4()), 'progress': 30}
    assert client.delete(path, json=body, headers=headers).status_code == 400
