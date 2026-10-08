from __future__ import annotations

import pytest
from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.ai.services.core import AIServiceError
from pixiv_novel_sync.ai.retrieval import TFIDFRetriever
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def writing(tmp_path):
    path = tmp_path / "writing.db"
    db = Database(path)
    db.init_schema()
    pid = db.create_ai_writing_project({"name": "测试"})
    cid = db.create_ai_chapter({"project_id": pid, "chapter_number": 1, "content": "原文"})
    yield db, AIWritingService(path), pid, cid
    db.close()


def test_chapter_cas_rejects_stale_writer(writing):
    db, _, _, cid = writing
    revision = db.get_ai_chapter(cid)["chapter_revision"]
    db.update_ai_chapter(cid, {"content": "人工修改"}, expected_revision=revision)
    with pytest.raises(ValueError, match="版本冲突"):
        db.update_ai_chapter(cid, {"content": "旧快照"}, expected_revision=revision)
    assert db.get_ai_chapter(cid)["content"] == "人工修改"


def test_pipeline_close_terminalizes_metadata(writing):
    db, service, pid, cid = writing
    stream = service.stream_chapter_pipeline({"project_id": pid, "chapter_id": cid, "steps": ["detect"]})
    next(stream)
    stream.close()
    assert db.get_ai_chapter(cid)["metadata"]["pipeline"]["status"] == "cancelled"


def test_pipeline_rejects_foreign_chapter(writing):
    db, service, _, cid = writing
    other = db.create_ai_writing_project({"name": "其他"})
    with pytest.raises(AIServiceError, match="所属"):
        list(service.stream_chapter_pipeline({"project_id": other, "chapter_id": cid, "steps": ["detect"]}))
    assert "pipeline" not in db.get_ai_chapter(cid)["metadata"]


def test_json_braces_inside_strings(writing):
    _, service, _, _ = writing
    import json
    data = {"text": '右括号 } 和转义 "'}
    assert service._extract_json_object("说明 " + json.dumps(data) + " 尾声") == data


def test_reindex_invalidates_search(tmp_path):
    retriever = TFIDFRetriever(tmp_path / "db.sqlite")
    try:
        retriever.index_chapter(1, 1, "城堡大门")
        assert retriever.search(1, "城堡")
        retriever.index_chapter(1, 1, "森林小路")
        assert retriever.search(1, "城堡") == []
    finally:
        retriever.close()


def test_delete_project_clears_import_link(writing):
    db, _, pid, _ = writing
    sid = db.create_ai_chat_session({"title": "导入"})
    db.update_ai_chat_session(sid, {"imported_project_id": pid})
    db.delete_ai_writing_project(pid)
    assert db.get_ai_chat_session(sid)["imported_project_id"] is None


@pytest.mark.parametrize("step,method", [("continue", "stream_chapter_continue"), ("deai", "stream_rewrite"), ("polish_dialogue", "stream_polish")])
def test_pipeline_preserves_concurrent_content(writing, monkeypatch, step, method):
    from pixiv_novel_sync.ai.models import AIStreamChunk
    db, service, pid, cid = writing
    def generate(payload):
        db.update_ai_chapter(cid, {"content": "人工修改"})
        yield AIStreamChunk(type="delta", text="生成")
        yield AIStreamChunk(type="done", data={})
    monkeypatch.setattr(service, method, generate)
    chunks = list(service.stream_chapter_pipeline({"project_id": pid, "chapter_id": cid, "steps": [step]}))
    content = db.get_ai_chapter(cid)["content"]
    assert content == ("人工修改生成" if step == "continue" else "人工修改")
    if step != "continue":
        assert any((c.data or {}).get("event") == "step_failed" for c in chunks)


def test_unknown_state_rejected(writing):
    db, service, pid, _ = writing
    with pytest.raises(AIServiceError):
        service.update_project_state(pid, "arbitrary", "value")
    assert "arbitrary" not in db.get_all_project_states(pid)


def test_create_plan_rolls_back_partial_failure(writing, monkeypatch):
    db, service, pid, _ = writing
    original = Database.create_ai_chapter
    def create(self, payload):
        if payload["chapter_number"] == 3:
            raise RuntimeError("injected")
        return original(self, payload)
    monkeypatch.setattr(Database, "create_ai_chapter", create)
    with pytest.raises(RuntimeError):
        service.create_chapters_from_plan(pid, [{"chapter_number": 2}, {"chapter_number": 3}])
    assert [c["chapter_number"] for c in db.list_ai_chapters(pid)] == [1]


@pytest.mark.parametrize("filename,required", [
    ("chapters", 'expected_revision: currentChapter.value.chapter_revision'),
    ("chapters", 'async function reloadCurrentChapter()'),
    ("chapters", 'pipelineAgentIds[kind] || bestAgentId(kind)'),
    ("chapters", 'signal: pipelineAbortController.signal'),
    ("chapters", 'streamAutosaved'),
    ("pipeline_modal", '@click="cancelPipeline"'),
    ("notes", 'chapter_id: rawImportChapterId.value'),
    ("source_search", 'window.aiApi.request(config.url(this.query))'),
])
def test_frontend_writing_contract(filename, required):
    from pathlib import Path
    path = Path(__file__).parents[1] / "src/pixiv_novel_sync/templates" / ("dashboard_ai_" + filename + ".html")
    assert required in path.read_text(encoding="utf-8")


def test_plan_generation_saves_style_first():
    from pathlib import Path
    text = (Path(__file__).parents[1] / "src/pixiv_novel_sync/templates/dashboard_ai_project.html").read_text(encoding="utf-8")
    for name in ("generateLongformPlan", "generateDetailedOutlines"):
        body = text.split("async function " + name + "()", 1)[1].split("async function ", 1)[0]
        assert body.index("await saveProjectStyleControl()") < body.index("await routeStream(")



def test_continue_autosave_rereads_content(writing, monkeypatch):
    from types import SimpleNamespace
    from pixiv_novel_sync.ai.models import AIStreamChunk, AIAgentConfig
    db, service, pid, cid = writing
    chapter = db.get_ai_chapter(cid)
    built = dict(chapter_id=cid, project_id=pid, chapter=chapter, chapter_number=1,
                 existing_content="原文", original_context_chars=2, context_chars=100,
                 raw_full_context="原文", style_prompt="", novel_prompt="", plan_text="")
    agent = AIAgentConfig(id=1, name="续写", task_type="continue", provider_id=1, model="test", system_prompt="")
    context = SimpleNamespace(job_id="test", prompt_budget=SimpleNamespace(input_budget=10000))
    monkeypatch.setattr(service, "_load_agent_config", lambda *args: agent)
    monkeypatch.setattr(service, "_build_chapter_continue_inputs", lambda *args: built)
    monkeypatch.setattr(service, "_start_route_job", lambda *args, **kwargs: context)
    monkeypatch.setattr(service, "_finish_route_job", lambda *args, **kwargs: True)
    def generate(*args):
        db.update_ai_chapter(cid, {"content": "人工修改"})
        yield AIStreamChunk(type="delta", text="生成")
        return SimpleNamespace(output_text="生成", finish_state="succeeded")
    monkeypatch.setattr(service, "_stream_route", generate)
    chunks = list(service.stream_chapter_continue({"agent_id": 1, "project_id": pid, "chapter_id": cid}))
    assert chunks[-1].type == "done"
    assert db.get_ai_chapter(cid)["content"] == "人工修改生成"


def test_resolution_preserves_notes(writing):
    import json
    db, service, pid, cid = writing
    fid = db.create_ai_foreshadow({"project_id": pid, "description": "谜题", "notes": "人工备注"})
    service.import_foreshadow_resolution_output(pid, {"chapter_id": cid, "output_text": json.dumps({"resolved": [{"id": fid, "evidence": "证据"}]})})
    assert "人工备注" in db.get_ai_foreshadow(fid)["notes"]


def test_state_ignores_no_foreshadows(writing):
    db, service, pid, cid = writing
    service._parse_and_save_state(db, pid, db.get_ai_chapter(cid), "=== new_foreshadows ===\n无")
    assert db.list_ai_foreshadows(pid) == []


def test_chapter_list_omits_content(writing):
    _, service, pid, _ = writing
    assert "content" not in service.list_chapters(pid)[0]


def test_embedding_rejects_remote_plain_http():
    from pixiv_novel_sync.ai.retrieval import OpenAICompatibleEmbeddingClient
    with pytest.raises(ValueError):
        OpenAICompatibleEmbeddingClient("http://example.com/v1", "secret", "model")


def test_draft_read_ui_exists():
    from pathlib import Path
    text = (Path(__file__).parents[1] / "src/pixiv_novel_sync/templates/dashboard_ai_output_panel.html").read_text(encoding="utf-8")
    assert "/api/dashboard/ai/drafts?page=" in text
    assert "draft.content" in text



def test_cas_conflict_releases_transaction(writing):
    from pixiv_novel_sync.storage.ai.writing import ChapterRevisionConflict
    db, _, _, cid = writing
    db.update_ai_chapter(cid, {"content": "new"})
    with pytest.raises(ChapterRevisionConflict):
        db.update_ai_chapter(cid, {"content": "old"}, expected_revision=0)
    assert not db.conn.in_transaction


@pytest.mark.parametrize("name", ["openNewWizardSession", "updateChatSessionAgent", "renameWizardSession", "deleteWizardSession", "saveDistillProfile", "deleteStyleProfile", "deleteNovelProfile"])
def test_wizard_actions_handle_network_errors(name):
    from pathlib import Path
    text = (Path(__file__).parents[1] / "src/pixiv_novel_sync/templates/dashboard_wizard.html").read_text(encoding="utf-8")
    body = text.split("async function " + name + "(", 1)[1].split("async function ", 1)[0]
    assert "catch (e)" in body


def test_empty_foreshadows_load_once():
    from pathlib import Path
    text = (Path(__file__).parents[1] / "src/pixiv_novel_sync/templates/dashboard_ai_chapters.html").read_text(encoding="utf-8")
    assert "if (!foreshadowsLoaded)" in text



def test_search_inflight_cannot_restore_invalidated_cache(tmp_path, monkeypatch):
    from pixiv_novel_sync.ai import retrieval
    retriever = TFIDFRetriever(tmp_path / "writing.db")
    retriever.index_chapter(1, 1, "城堡大门")
    original = retrieval.math.sqrt
    indexed = False
    def sqrt(value):
        nonlocal indexed
        if not indexed:
            indexed = True
            retriever.index_chapter(1, 1, "森林小路")
        return original(value)
    monkeypatch.setattr(retrieval.math, "sqrt", sqrt)
    try:
        retriever.search(1, "城堡")
        assert retriever.search(1, "城堡") == []
    finally:
        retriever.close()


def test_batch_pipeline_close_closes_active_chapter(writing):
    db, service, pid, cid = writing
    stream = service.stream_chapters_pipeline({"project_id": pid, "chapter_ids": [cid], "steps": ["detect"]})
    while next(stream).type != "metadata":
        pass
    stream.close()
    assert db.get_ai_chapter(cid)["metadata"]["pipeline"]["status"] == "cancelled"



def test_continue_checks_chapter_ownership_before_context(writing):
    from pixiv_novel_sync.ai.models import AIAgentConfig
    db, service, _, cid = writing
    other = db.create_ai_writing_project({"name": "other"})
    agent = AIAgentConfig(id=1, name="writer", task_type="continue", provider_id=1, model="test", system_prompt="")
    with pytest.raises(AIServiceError, match="属于"):
        service._build_chapter_continue_inputs(db, {"project_id": other, "chapter_id": cid}, agent)


def test_delete_index_only_after_database_commit(writing, monkeypatch):
    from types import SimpleNamespace
    db, service, pid, _ = writing
    calls = []
    def delete_index(project_id):
        assert db.get_ai_writing_project(project_id) is None
        assert not db.conn.in_transaction
        calls.append(project_id)
    monkeypatch.setattr(service, "_get_retriever", lambda: SimpleNamespace(delete_project=delete_index))
    service.delete_writing_project(pid)
    assert calls == [pid]


def test_two_database_writers_only_one_cas_wins(writing):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from pixiv_novel_sync.storage.ai.writing import ChapterRevisionConflict
    db, service, _, cid = writing
    gate = Barrier(2)
    def write(text):
        connection = Database(service.db_path)
        try:
            revision = connection.get_ai_chapter(cid)["chapter_revision"]
            gate.wait(timeout=5)
            try:
                connection.update_ai_chapter(cid, {"content": text}, expected_revision=revision)
                return "saved"
            except ChapterRevisionConflict:
                return "conflict"
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(write, ["first", "second"]))
    assert sorted(outcomes) == ["conflict", "saved"]
    assert db.get_ai_chapter(cid)["chapter_revision"] == 1



@pytest.fixture
def writing_http(writing, monkeypatch):
    from flask import Flask
    from types import SimpleNamespace
    from pixiv_novel_sync import ai_web
    db, service, pid, cid = writing
    monkeypatch.setattr(ai_web, "AIWritingService", lambda path: service)
    app = Flask(__name__)
    app.secret_key = "writing-http-test"
    app.config["TESTING"] = True
    ai_web.register_ai_routes(app, SimpleNamespace(storage=SimpleNamespace(db_path=service.db_path)))
    return app.test_client(), db, service, pid, cid


@pytest.mark.parametrize("revision", [{}, {"expected_revision": None}, {"expected_revision": True}, {"expected_revision": "0"}, {"expected_revision": -1}, {"expected_revision": 0.0}])
def test_http_chapter_requires_integer_revision(writing_http, revision):
    client, db, _, _, cid = writing_http
    response = client.put(f"/api/dashboard/ai/chapters/{cid}", json={"content": "不能写入", **revision})
    assert response.status_code == 400
    assert response.json["ok"] is False
    assert db.get_ai_chapter(cid)["content"] == "原文"
    assert db.get_ai_chapter(cid)["chapter_revision"] == 0


def test_http_chapter_stale_revision_is_409(writing_http):
    client, db, _, _, cid = writing_http
    db.update_ai_chapter(cid, {"content": "最新内容"})
    response = client.put(f"/api/dashboard/ai/chapters/{cid}", json={"content": "旧快照", "expected_revision": 0})
    assert response.status_code == 409
    assert "版本冲突" in response.json["error"]
    assert db.get_ai_chapter(cid)["content"] == "最新内容"


def test_http_chapter_returns_current_revision(writing_http):
    client, db, _, _, cid = writing_http
    response = client.put(f"/api/dashboard/ai/chapters/{cid}", json={"content": "成功保存", "expected_revision": 0})
    assert response.status_code == 200
    assert response.json["data"]["chapter_revision"] == 1
    assert response.json["data"]["content"] == "成功保存"
    assert db.get_ai_chapter(cid)["chapter_revision"] == 1


def test_http_sse_service_error_preserves_actionable_message(writing_http, monkeypatch):
    from pixiv_novel_sync.ai.models import AIStreamChunk
    client, _, service, _, _ = writing_http
    closed = []
    def stream(payload):
        try:
            yield AIStreamChunk(type="metadata", data={"job_id": "test"})
            raise AIServiceError("章节版本冲突，请重新加载后再保存")
        finally:
            closed.append(True)
    monkeypatch.setattr(service, "stream_continue", stream)
    response = client.post("/api/dashboard/ai/continue/stream", json={})
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "event: error" in body
    assert "章节版本冲突，请重新加载后再保存" in body
    assert closed == [True]


def test_http_sse_disconnect_closes_pipeline(writing_http):
    client, db, _, pid, cid = writing_http
    response = client.post("/api/dashboard/ai/chapters/pipeline/stream", json={"project_id": pid, "chapter_id": cid, "steps": ["detect"]}, buffered=False)
    first = next(iter(response.response)).decode("utf-8")
    assert "event: metadata" in first
    response.close()
    assert db.get_ai_chapter(cid)["metadata"]["pipeline"]["status"] == "cancelled"



def test_retry_pipeline_preserves_unselected_results(writing, monkeypatch):
    db, service, pid, cid = writing
    db.patch_ai_chapter_metadata(cid, {"pipeline": {"id": "previous", "status": "partial", "steps": [
        {"name": "detect", "status": "done", "score": 88},
        {"name": "index", "status": "failed", "error": "old error"},
    ]}})
    monkeypatch.setattr(service, "index_chapter_for_retrieval", lambda *args: None)
    list(service.stream_chapter_pipeline({"project_id": pid, "chapter_id": cid, "steps": ["index"], "retry": True}))
    pipeline = db.get_ai_chapter(cid)["metadata"]["pipeline"]
    steps = {step["name"]: step for step in pipeline["steps"]}
    assert steps["detect"] == {"name": "detect", "status": "done", "score": 88}
    assert steps["index"]["status"] == "done"
    assert "error" not in steps["index"]
    assert pipeline["status"] == "succeeded"


def test_summary_index_cannot_overwrite_newer_writer(writing, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    db, first, pid, cid = writing
    second = AIWritingService(first.db_path)
    retriever = TFIDFRetriever(first.db_path)
    paused = Event()
    release = Event()
    def delayed_retriever():
        paused.set()
        assert release.wait(10), "测试调度超时"
        return retriever
    monkeypatch.setattr(first, "_get_retriever", delayed_retriever)
    monkeypatch.setattr(second, "_get_retriever", lambda: retriever)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(first.update_chapter, cid, {"summary": "旧索引城堡"})
            try:
                assert paused.wait(5)
                new = pool.submit(second.update_chapter, cid, {"summary": "新索引森林"})
                new.result(timeout=5)
            finally:
                release.set()
            old.result(timeout=5)
        assert db.get_ai_chapter(cid)["summary"] == "新索引森林"
        assert retriever.search(pid, "森林")[0].content == "新索引森林"
        assert retriever.search(pid, "城堡") == []
    finally:
        retriever.close()



def test_failed_index_can_retry_without_erasing_detect(writing, monkeypatch):
    db, service, pid, cid = writing
    def fail_index(*args):
        raise AIServiceError("索引失败")
    monkeypatch.setattr(service, "index_chapter_for_retrieval", fail_index)
    list(service.stream_chapter_pipeline({"project_id": pid, "chapter_id": cid, "steps": ["detect", "index"]}))
    before = db.get_ai_chapter(cid)["metadata"]["pipeline"]
    steps = {step["name"]: step for step in before["steps"]}
    assert steps["index"]["status"] == "failed"
    assert before["status"] == "partial"
    monkeypatch.setattr(service, "index_chapter_for_retrieval", lambda *args: None)
    list(service.stream_chapter_pipeline({"project_id": pid, "chapter_id": cid, "steps": ["index"], "retry": True}))
    after = db.get_ai_chapter(cid)["metadata"]["pipeline"]
    assert after["steps"][0] == steps["detect"]
    assert after["steps"][1]["status"] == "done"
    assert after["status"] == "succeeded"
    assert after["failed_steps"] == 0


def test_retry_keeps_unselected_failure_and_warning(writing, monkeypatch):
    db, service, pid, cid = writing
    warning = {"step": "summary", "message": "摘要失败"}
    db.patch_ai_chapter_metadata(cid, {"pipeline": {"steps": [{"name": "summary", "status": "failed"}, {"name": "index", "status": "failed"}], "warnings": [warning, {"step": "index", "message": "旧错误"}]}})
    monkeypatch.setattr(service, "index_chapter_for_retrieval", lambda *args: None)
    list(service.stream_chapter_pipeline({"project_id": pid, "chapter_id": cid, "steps": ["index"], "retry": True}))
    result = db.get_ai_chapter(cid)["metadata"]["pipeline"]
    assert result["failed_steps"] == 1
    assert result["status"] == "partial"
    assert result["warnings"] == [warning]


def test_index_snapshot_and_write_share_main_database_transaction(writing, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    db, first, pid, cid = writing
    second = AIWritingService(first.db_path)
    entered_index = Event()
    second_attempting = Event()
    release_index = Event()
    retriever = TFIDFRetriever(first.db_path)
    original_index = retriever.index_chapter
    original_update = Database.update_ai_chapter
    def update(self, chapter_id, payload, **kwargs):
        if payload.get("summary") == "最新森林":
            second_attempting.set()
        return original_update(self, chapter_id, payload, **kwargs)
    def index(project_id, chapter_number, summary, key_events):
        if summary == "旧城堡":
            entered_index.set()
            assert release_index.wait(10)
        return original_index(project_id, chapter_number, summary, key_events)
    monkeypatch.setattr(Database, "update_ai_chapter", update)
    monkeypatch.setattr(retriever, "index_chapter", index)
    monkeypatch.setattr(first, "_get_retriever", lambda: retriever)
    monkeypatch.setattr(second, "_get_retriever", lambda: retriever)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first.update_chapter, cid, {"summary": "旧城堡"})
            try:
                assert entered_index.wait(5)
                b = pool.submit(second.update_chapter, cid, {"summary": "最新森林"})
                assert second_attempting.wait(5)
                # 索引写入仍进行时，另一连接尚不能提交新版本。
                assert db.get_ai_chapter(cid)["summary"] == "旧城堡"
            finally:
                release_index.set()
            a.result(timeout=5)
            b.result(timeout=5)
        assert db.get_ai_chapter(cid)["summary"] == "最新森林"
        assert retriever.search(pid, "森林")[0].content == "最新森林"
    finally:
        retriever.close()
