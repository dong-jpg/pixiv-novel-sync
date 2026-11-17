from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from pixivpy3.utils import JsonDict

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus, JobType
from pixiv_novel_sync.jobs.runner import JobRunner
from pixiv_novel_sync.jobs.tasks import execute_task
from pixiv_novel_sync.models import NovelTextRecord, SourceRecord, UserRecord
from pixiv_novel_sync.preferences import PreferenceAnalyzer
from pixiv_novel_sync.recommendations import RecommendationService
from pixiv_novel_sync.settings import _coerce_float, _coerce_int, _coerce_optional_int
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService
from tests.test_preference_incremental import _add_novel
from tests.test_recommendations import make_settings


@pytest.mark.parametrize("old_min,new_min,expected", [(1000, 7000, 1), (7000, 1000, 2), (1000, 10000, 0)])
def test_preference_scope_changes_rebuild_already_analyzed_novels(tmp_path, old_min, new_min, expected):
    with Database(tmp_path / "scope.db") as db:
        db.init_schema()
        db.upsert_user(UserRecord(user_id=1, name="作者", account="a", raw_json="{}"))
        _add_novel(db, 1, 1, '["短篇"]', "短篇内容", text_length=6000)
        _add_novel(db, 2, 1, '["长篇"]', "长篇内容", text_length=8000)
        analyzer = PreferenceAnalyzer(db)
        analyzer.analyze_incremental(min_text_length=old_min, max_batches=10)
        result = analyzer.analyze_incremental(min_text_length=new_min, max_batches=10)
        profile = analyzer.rebuild_profile_from_accumulator()
        assert result["done"] is True
        assert result["processed_this_run"] == result["analyzed_total"] == expected
        assert profile["stats"]["novel_count"] == expected
        assert profile["source_scope"]["min_text_length"] == new_min
        full = analyzer.analyze_local({"min_text_length": new_min})
        assert profile["stats"]["total_chars"] == full["stats"]["total_chars"]
        assert {t["name"]: t["count"] for t in profile["stats"]["top_tags"]} == {
            t["name"]: t["count"] for t in full["stats"]["top_tags"]
        }


def test_empty_changed_scope_drops_old_ai_keywords(tmp_path):
    settings = make_settings(tmp_path)
    with Database(settings.storage.db_path) as db:
        db.init_schema()
        db.upsert_user(UserRecord(user_id=1, name="作者", account="a", raw_json="{}"))
        _add_novel(db, 1, 1, '["旧标签"]', "正文", text_length=6000)
        PreferenceAnalyzer(db).analyze_incremental(min_text_length=1000)
        db.create_preference_profile({
            "is_default": True, "stats": {"refined_keywords": ["旧词"]},
            "profile": {}, "source_scope": {"min_text_length": 1000},
        })
    result = execute_task("preference_analyze", settings, {"params": {"scope": {"min_text_length": 10000}}})
    assert result["stats"]["novel_count"] == 0
    assert result["profile"]["positive_preferences"]["keywords"] == []


@pytest.mark.parametrize("require_assets", [False, True])
def test_empty_text_never_counts_as_an_archived_series_chapter(tmp_path, require_assets):
    with Database(tmp_path / "archive.db") as db:
        db.init_schema()
        db.upsert_user(UserRecord(user_id=1, name="作者", account="a", raw_json="{}"))
        for novel_id, body in ((1, "有效正文"), (2, ""), (3, " \n\t")):
            _add_novel(db, novel_id, 1, "[]", body, series_id=9)
            db.upsert_novel_text(NovelTextRecord(novel_id, body, None, str(novel_id)))
        assert db.count_series_complete_novels(9, require_assets=require_assets) == 1
        assert db.get_existing_novel_ids([1, 2, 3], require_assets=require_assets) == {1}
        assert [db.novel_archive_complete(n, require_assets=require_assets) for n in (1, 2, 3)] == [True, False, False]


class _RecommendationApi:
    def search_novel(self, **kwargs):
        return SimpleNamespace(novels=[SimpleNamespace(
            id=42, title="候选", text_length=6000, caption="", tags=["冒险"],
            user=SimpleNamespace(id=9, name="作者"), total_bookmarks=10, series=None,
        )], next_url=None)

    def parse_qs(self, url):
        return None


def _recommendation_profile(db):
    return db.create_preference_profile({
        "name": "默认", "is_default": True,
        "profile": {"search_strategy": {"primary_tags": ["冒险"]}},
    })


@pytest.mark.parametrize("cancel_timing", ["before_publish", "inside_publish"])
def test_recommendation_publish_and_job_cancel_are_mutually_exclusive(tmp_path, monkeypatch, cancel_timing):
    settings = make_settings(tmp_path)
    with Database(settings.storage.db_path) as db:
        db.init_schema()
        _recommendation_profile(db)
    manager = JobManager()
    job = manager.submit(JobSpec(source=JobSource.WEB, task_types=["recommendation_run"], job_type=JobType.RECOMMENDATION_RUN))
    monkeypatch.setattr(RecommendationService, "_login_api", lambda self: _RecommendationApi())
    accepted = []
    if cancel_timing == "before_publish":
        monkeypatch.setattr(RecommendationService, "_page_delay", lambda *a: accepted.append(manager.request_cancel(job.job_id)))
    else:
        monkeypatch.setattr(RecommendationService, "_page_delay", lambda *a: None)
        original = Database.upsert_recommendation_item

        def upsert(db, data):
            accepted.append(manager.request_cancel(job.job_id))
            return original(db, data)

        monkeypatch.setattr(Database, "upsert_recommendation_item", upsert)
    JobRunner(manager, lambda task_type, context: execute_task(task_type, settings, context)).run(job.job_id)
    with Database(settings.storage.db_path) as db:
        run = db.list_recommendation_runs()[0]
        if cancel_timing == "before_publish":
            assert accepted == [True]
            assert job.status == JobStatus.CANCELLED
            assert run["status"] == "cancelled"
            assert db.list_recommendation_items() == []
        else:
            assert accepted == [False]
            assert job.status == JobStatus.SUCCEEDED
            assert run["status"] == "succeeded"
            assert [item["novel_id"] for item in db.list_recommendation_items()] == [42]


def test_recommendation_search_error_json_is_a_failed_run(tmp_path):
    class Api(_RecommendationApi):
        def search_novel(self, **kwargs):
            return JsonDict({"error": {"message": "Rate Limit"}})

    with Database(tmp_path / "errors.db") as db:
        db.init_schema()
        _recommendation_profile(db)
        service = RecommendationService(db, make_settings(tmp_path), api=Api())
        with pytest.raises(RuntimeError, match="全部失败"):
            service.run()
        run = db.list_recommendation_runs()[0]
        assert run["status"] == "failed"
        assert run["stats"]["query_errors"] == 1
        assert db.list_recommendation_items() == []


@pytest.mark.parametrize("failure", ["cancel", "error_json", "network"])
def test_recommendation_series_failures_are_not_zero_length(tmp_path, failure):
    class Api:
        def novel_series(self, **kwargs):
            if failure == "cancel":
                raise InterruptedError("stop")
            if failure == "network":
                raise RuntimeError("upstream unavailable")
            return JsonDict({"error": {"message": "authentication failed"}})

        def parse_qs(self, url):
            return None

    service = RecommendationService(None, make_settings(tmp_path))
    expected = InterruptedError if failure == "cancel" else RuntimeError
    with pytest.raises(expected):
        service._series_length(Api(), 9)


@pytest.mark.parametrize("operation", ["create", "update", "missing"])
def test_failed_profile_change_preserves_the_default(tmp_path, operation):
    with Database(tmp_path / "profiles.db") as db:
        db.init_schema()
        default_id = _recommendation_profile(db)
        other_id = db.create_preference_profile({"name": "备用"})
        if operation == "missing":
            with pytest.raises(ValueError):
                db.set_default_preference_profile(99999)
        else:
            invalid = {"is_default": True, "name": {"invalid": "name"}}
            with pytest.raises(sqlite3.Error):
                if operation == "create":
                    db.create_preference_profile(invalid)
                else:
                    db.update_preference_profile(other_id, invalid)
        assert db.get_default_preference_profile()["id"] == default_id
        assert sum(profile["is_default"] for profile in db.list_preference_profiles()) == 1


@pytest.mark.parametrize("muted", ["author", "tag"])
def test_muted_series_is_filtered_without_requesting_its_chapters(tmp_path, muted):
    service = RecommendationService(None, make_settings(tmp_path))
    candidate = _RecommendationApi().search_novel().novels[0]
    candidate.series = SimpleNamespace(id=9)

    class Api:
        def novel_series(self, *args, **kwargs):
            pytest.fail("屏蔽作品不应请求系列章节")

    state = {"muted_authors": {"9"} if muted == "author" else set(),
             "muted_tags": {"冒险"} if muted == "tag" else set()}
    assert service._candidate_to_item(Api(), candidate, {"query": "冒险"}, {}, {}, state, existing_items=[]) is None


@pytest.mark.parametrize("restrict", ["public", "private"])
def test_unbookmark_detection_only_compares_the_requested_scope(tmp_path, restrict):
    class Api:
        def user_bookmarks_novel(self, **kwargs):
            assert kwargs["restrict"] == restrict
            return SimpleNamespace(novels=[SimpleNamespace(id=1)], next_url=None)

        def parse_qs(self, url):
            return None

        def novel_detail(self, novel_id):
            pytest.fail("未同步的收藏范围不能被当作取消收藏来检查")

    with Database(tmp_path / "scope-detection.db") as db:
        db.init_schema()
        db.upsert_user(UserRecord(user_id=1, name="作者", account="a", raw_json="{}"))
        _add_novel(db, 1, 1, "[]", "正文")
        _add_novel(db, 2, 1, "[]", "正文")
        db.upsert_source(SourceRecord(1, f"bookmark_{restrict}", "1"))
        other = "private" if restrict == "public" else "public"
        db.upsert_source(SourceRecord(2, f"bookmark_{other}", "1"))
        service = BookmarkNovelSyncService(Api(), db, None, make_settings(tmp_path))
        result = service.detect_unbookmarked_novels(1, iter([restrict]))
        assert result["total_checked"] == result["new_pending"] == 0
        assert db.get_pending_deletion_count() == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "nan", "inf"])
def test_nonfinite_yaml_numbers_fall_back_instead_of_disabling_rate_limits(value):
    assert _coerce_float(value, 2.0) == 2.0
    assert _coerce_int(value, 30) == 30
    assert _coerce_optional_int(value) is None
