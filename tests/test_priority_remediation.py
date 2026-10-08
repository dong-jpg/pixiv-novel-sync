from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from test_recommendations import make_settings
from pixiv_novel_sync.webapp import _ArchiveTrash, _sweep_stale_trash, _remove_archive_files_atomic


def test_failed_archive_restore_preserves_only_copy_and_survives_sweep(tmp_path: Path) -> None:
    trash = _ArchiveTrash(make_settings(tmp_path), [])
    source = tmp_path / 'public' / 'novel'
    source.mkdir(parents=True)
    (source / 'text.txt').write_text('only copy', encoding='utf-8')
    trash._move_to_trash(source)
    staged = trash._moves[0][1]
    assert staged.resolve().is_relative_to(tmp_path.resolve())
    with patch('pixiv_novel_sync.webapp.shutil.move', side_effect=PermissionError('locked')):
        trash.rollback()
    assert staged.is_dir()
    assert (staged / 'text.txt').read_text(encoding='utf-8') == 'only copy'
    assert len(trash._moves) == 1
    old = time.time() - 172800
    os.utime(trash._trash_root, (old, old))
    _sweep_stale_trash(trash._trash_root.parent)
    assert staged.exists()
    trash.rollback()
    assert (source / 'text.txt').read_text(encoding='utf-8') == 'only copy'
    assert not trash._trash_root.exists()


def test_archive_restore_does_not_overwrite_recreated_source(tmp_path: Path) -> None:
    trash = _ArchiveTrash(make_settings(tmp_path), [])
    source = tmp_path / 'public' / 'text.txt'
    source.parent.mkdir(parents=True)
    source.write_text('old', encoding='utf-8')
    trash._move_to_trash(source)
    staged = trash._moves[0][1]
    source.write_text('new', encoding='utf-8')
    trash.rollback()
    assert source.read_text(encoding='utf-8') == 'new'
    assert staged.read_text(encoding='utf-8') == 'old'


def test_partial_stage_failure_runs_rollback(tmp_path: Path, monkeypatch) -> None:
    called = []
    def stage(self):
        raise OSError('stage failed')
    monkeypatch.setattr(_ArchiveTrash, 'stage', stage)
    monkeypatch.setattr(_ArchiveTrash, 'rollback', lambda self: called.append('rollback'))
    with pytest.raises(OSError, match='stage failed'):
        _remove_archive_files_atomic(make_settings(tmp_path), [], lambda: called.append('delete'))
    assert called == ['rollback']


@pytest.mark.parametrize('mode', ['truncated', 'failed', 'cancelled'])
def test_scheduled_backup_propagates_terminal_state(tmp_path: Path, monkeypatch, mode) -> None:
    from pixiv_novel_sync.storage_db import Database
    from pixiv_novel_sync.jobs.quick_sync import run_scheduled_user_backup
    from pixiv_novel_sync.webapp import _task_log_status_for_stats
    settings = make_settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    db.conn.execute("INSERT INTO users(user_id, name, raw_json) VALUES (1, 'u', '{}')")
    db.close()
    def backup(*args, **kwargs):
        if mode == 'failed':
            raise RuntimeError('upstream failed')
        if mode == 'cancelled':
            raise InterruptedError('cancelled')
        return {'novels': 0, 'truncated': True, 'incomplete': True}
    monkeypatch.setattr('pixiv_novel_sync.jobs.quick_sync.job_services.run_user_backup_task', backup)
    result = run_scheduled_user_backup(settings)
    if mode == 'cancelled':
        assert result['stopped']
        assert result['failed_users'] == 0
    else:
        assert result['incomplete']
        assert result['aborted_reason']
        assert _task_log_status_for_stats(result) != 'succeeded'
        if mode == 'truncated':
            assert result['truncated']


def test_one_bad_recommendation_candidate_keeps_good_candidate(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    from pixiv_novel_sync.storage_db import Database
    from pixiv_novel_sync.recommendations import RecommendationService
    settings = make_settings(tmp_path)
    db = Database(settings.storage.db_path)
    try:
        db.init_schema()
        profile_id = db.create_preference_profile({'name':'profile', 'source_scope':{}, 'stats':{}, 'profile':{}})
        service = RecommendationService(db, settings, api=object())
        monkeypatch.setattr(service, '_search_novels', lambda *a: [SimpleNamespace(id=1), SimpleNamespace(id=2)])
        monkeypatch.setattr(service, '_page_delay', lambda *a: None)
        def candidate(api, novel, *args, **kwargs):
            if novel.id == 1:
                raise ValueError('bad candidate')
            return {'novel_id':2, 'title':'good', 'item_type':'novel'}
        monkeypatch.setattr(service, '_candidate_to_item', candidate)
        result = service.run(profile_id=profile_id, search_plan={'queries':[{'query':'test'}]})
        assert result['stats']['incomplete']
        assert result['stats']['query_errors'] == 0
        assert result['stats']['candidate_errors'] == 1
        assert [item['novel_id'] for item in db.list_recommendation_items()] == [2]
    finally:
        db.close()


@pytest.mark.parametrize('payload', [{}, {'name':'custom', 'description':'kept'}])
def test_preference_endpoint_preserves_omitted_fields(tmp_path: Path, payload) -> None:
    from flask import Flask
    from types import SimpleNamespace
    from pixiv_novel_sync.preference_web import register_preference_routes
    app = Flask(__name__)
    app.secret_key = "test-only-key"
    register_preference_routes(app, make_settings(tmp_path))
    captured = []
    def submit(spec, *args, **kwargs):
        captured.append(spec.params)
        return SimpleNamespace(job_id='test')
    app.config['submit_shared_web_job'] = submit
    response = app.test_client().post('/api/dashboard/preferences/profiles/analyze', json=payload)
    assert response.status_code == 200
    for key in ('name','description'):
        if key in payload:
            assert captured[0][key] == payload[key]
        else:
            assert key not in captured[0]


def test_preference_page_does_not_reset_name_on_analyze() -> None:
    text = Path('src/pixiv_novel_sync/templates/dashboard_preferences.html').read_text(encoding='utf-8')
    analyze = text.split('async function analyze()', 1)[1].split('async function', 1)[0]
    assert "name: '本地偏好画像'" not in analyze


def test_removed_legacy_routes_are_not_registered_in_source() -> None:
    import ast
    removed = {'/oauth/start', '/oauth/callback', '/oauth/sync-callback/<task_id>', '/api/dashboard/pending-deletions/count', '/api/dashboard/recommendations/runs'}
    paths = set()
    for name in ('webapp.py', 'preference_web.py'):
        tree = ast.parse(Path('src/pixiv_novel_sync', name).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {'route','get','post','add_url_rule'} and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    paths.add(arg.value)
    assert not (paths & removed)


def test_cron_uses_required_dependency_without_simple_fallback() -> None:
    import pixiv_novel_sync.settings as settings
    assert not hasattr(settings, '_simple_cron_next_run')


def test_removed_oauth_flow_does_not_leave_unreachable_manager_methods() -> None:
    from pixiv_novel_sync.oauth_helper import OAuthManager
    assert not hasattr(OAuthManager, 'find_task_by_state')
    assert not hasattr(OAuthManager, 'sync_state_from_callback_url')


def test_partial_cross_device_move_keeps_complete_staged_copy(tmp_path: Path, monkeypatch) -> None:
    import errno
    import shutil
    settings = make_settings(tmp_path)
    source = tmp_path / 'public' / 'novel'
    source.mkdir(parents=True)
    (source / 'a.txt').write_text('A', encoding='utf-8')
    (source / 'b.txt').write_text('B', encoding='utf-8')
    real_rmtree = shutil.rmtree
    observed = []
    deleted = []
    def stage(trash):
        observed.append(trash)
        trash._move_to_trash(source)
    def rmtree(path, *args, **kwargs):
        assert Path(path).resolve().is_relative_to(tmp_path.resolve())
        if Path(path) == source:
            (source / 'a.txt').unlink()
            raise PermissionError('b.txt locked after a.txt removed')
        return real_rmtree(path, *args, **kwargs)
    monkeypatch.setattr(_ArchiveTrash, 'stage', stage)
    with patch('os.rename', side_effect=OSError(errno.EXDEV, 'cross-device')):
        with patch('shutil.rmtree', side_effect=rmtree):
            with pytest.raises(PermissionError):
                _remove_archive_files_atomic(settings, [], lambda: deleted.append(True))
    trash = observed[0]
    assert not deleted
    assert not (source / 'a.txt').exists()
    assert (trash._trash_root / '0' / 'a.txt').read_text(encoding='utf-8') == 'A'
    assert (trash._trash_root / '0' / 'b.txt').read_text(encoding='utf-8') == 'B'
    assert trash._moves
