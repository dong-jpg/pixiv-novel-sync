"""带版本前置条件的阅读进度接口；不依赖远程执行节点。"""
from __future__ import annotations

import json
import sqlite3

from flask import jsonify, request

from ..storage.reading_progress import ReadingProgressError, validate_progress_change, validate_progress_id


MESSAGES = {
    'progress_conflict': '其他设备更新了阅读进度，请选择要保留的位置。',
    'progress_idempotency_conflict': '保存请求的内容已改变，请重新确认阅读位置。',
    'progress_version_required': '阅读页已更新，请刷新后重试。',
    'novel_not_found': '小说不存在。',
    'progress_storage_unavailable': '暂时无法保存进度，本机记录会保留，请稍后重试。',
}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate_field')
        result[key] = value
    return result


def _body(reset):
    if request.content_length is not None and request.content_length > 4096:
        raise ReadingProgressError('progress_request_too_large', 413)
    raw = request.stream.read(4097)
    if len(raw) > 4096:
        raise ReadingProgressError('progress_request_too_large', 413)
    if not raw:
        raise ReadingProgressError('progress_version_required', 428)
    if not request.is_json:
        raise ReadingProgressError('invalid_progress_request')
    try:
        body = json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise ReadingProgressError('invalid_progress_request') from None
    required = {'base_version', 'mutation_id'} | (set() if reset else {'progress', 'status'})
    if type(body) is not dict or body.keys() - required:
        raise ReadingProgressError('invalid_progress_fields')
    if not {'base_version', 'mutation_id'} <= body.keys():
        raise ReadingProgressError('progress_version_required', 428)
    if not required <= body.keys():
        raise ReadingProgressError('invalid_progress_fields')
    if reset:
        body.update(progress=0, status='unread')
    validate_progress_change(body['progress'], body['status'], body['base_version'], body['mutation_id'], reset=reset)
    return body


def register_reading_progress_routes(app, database_factory):
    def response(payload, status=200):
        result = jsonify(payload)
        result.status_code = status
        result.headers['Cache-Control'] = 'private, no-store'
        return result

    def execute(novel_id, operation):
        db = None
        try:
            validate_progress_id(novel_id)
            # 校验小请求后再打开书库，错误/超限输入不触发数据库迁移。
            payload = _body(operation == 'reset') if operation != 'read' else None
            db = database_factory()
            if operation == 'read':
                return response(db.progress_snapshot(novel_id))
            result = db.compare_and_set_reading_progress(
                novel_id, payload['progress'], payload['status'], payload['base_version'],
                payload['mutation_id'], reset=operation == 'reset')
            return response(dict(result, success=True, ok=True))
        except ReadingProgressError as error:
            body = {'error': error.code, 'detail': MESSAGES.get(error.code, '阅读进度请求格式不正确。')}
            if error.record is not None:
                body['record'] = error.record
            return response(body, error.status)
        except (sqlite3.Error, OSError):
            return response({'error': 'progress_storage_unavailable', 'detail': MESSAGES['progress_storage_unavailable']}, 503)
        finally:
            if db is not None:
                db.close()

    @app.get('/api/dashboard/novels/<int:novel_id>/progress')
    def get_novel_progress(novel_id):
        return execute(novel_id, 'read')

    @app.post('/api/dashboard/novels/<int:novel_id>/progress')
    def update_novel_progress(novel_id):
        return execute(novel_id, 'save')

    @app.delete('/api/dashboard/novels/<int:novel_id>/progress')
    def delete_novel_progress(novel_id):
        return execute(novel_id, 'reset')
