import json
from pathlib import Path

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobType
from pixiv_novel_sync.jobs.runner import JobRunner
from pixiv_novel_sync.work_analysis.executor import WorkAnalysisExecutor
from work_analysis_runtime_helpers import make_runtime_environment


def test_full_work_result_does_not_copy_source_or_reports_into_generic_jobs(tmp_path, monkeypatch):
    marker = "PRIVATE_SYNTHETIC_WHOLE_WORK_SENTINEL_4420"
    report_marker = "PRIVATE_SYNTHETIC_REPORT_SENTINEL_9173"
    env = make_runtime_environment(tmp_path, monkeypatch, texts=[marker + " first chapter", "second chapter tail"])
    try:
        original_stream = env.provider.stream_generate

        def report_stream(*args, **kwargs):
            for chunk in original_stream(*args, **kwargs):
                if chunk.type == "delta":
                    result = json.loads(chunk.text)
                    result["summary"] = report_marker
                    chunk.text = json.dumps(result)
                yield chunk

        monkeypatch.setattr(env.provider, "stream_generate", report_stream)
        manager = JobManager()
        job = manager.submit(JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": env.run_id}))
        executor = WorkAnalysisExecutor(env.db_factory, lambda: env.service)
        JobRunner(manager, lambda task, context: executor.run(env.run_id, context)).run(job.job_id)
        report = env.db.get_work_analysis_report(env.run_id)
        assert report["coverage"]["complete"] and report["synthesis_complete"]
        assert report["artifact"]["summary"] == report_marker
        assert job.spec.params == {"run_id": env.run_id}
        generic = [dict(row) for row in env.db.conn.execute("SELECT * FROM ai_jobs")]
        attempts = [dict(row) for row in env.db.conn.execute("SELECT * FROM ai_job_model_attempts")]
        assert generic and attempts
        serialized = json.dumps(generic + attempts, ensure_ascii=False)
        assert marker not in serialized and report_marker not in serialized
        assert all(row["output_text"] is None and row["output_json"] is None for row in generic)
        shared = json.dumps({"progress": job.progress, "stats": job.stats,
                             "logs": [entry.message for entry in job.logs]}, ensure_ascii=False)
        assert marker not in shared and report_marker not in shared
        ledger = env.db.conn.execute(
            "SELECT entry_kind,request_ordinal,planned_tokens FROM work_analysis_requests ORDER BY child_job_id,request_ordinal"
        ).fetchall()
        assert any(row["entry_kind"] == "attachment" and row["request_ordinal"] == 0
                   and row["planned_tokens"] is None for row in ledger)
        actual = [row for row in ledger if row["entry_kind"] == "request"]
        assert report["usage_summary"]["network_request_count"] == len(actual) == len(env.provider.sent_units)
        assert all(row["planned_tokens"] > 0 for row in actual)
    finally:
        env.close()


def test_old_continue_is_not_the_new_business_resume_path():
    root = Path(__file__).resolve().parents[1]
    controller = (root / "src/pixiv_novel_sync/static/js/work_analysis.js").read_text(encoding="utf-8")
    assert "/ai/jobs/" not in controller
    assert "work-analyses" in controller
