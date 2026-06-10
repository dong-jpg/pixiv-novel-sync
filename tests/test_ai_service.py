

def test_pipeline_continue_appends_to_existing_content(tmp_path):
    """回归测试 P0: pipeline 的 continue 步骤必须拼接章节已有正文,不能覆盖。
    旧bug: sub_db.update_ai_chapter(chapter_id, {"content": step_output}) 直接覆盖。
    修复后: 先读 existing_content,保存 existing_content + generated。"""
    from pixiv_novel_sync.ai.service import AIWritingService
    db_path = tmp_path / "test.db"
    service = AIWritingService(db_path)
    
    # 创建项目和章节,预置"已有正文ABC"
    proj_id = service.create_writing_project({"name": "test", "status": "draft"})
    ch_id = service.create_chapter({"project_id": proj_id, "title": "ch1", "content": "已有正文ABC", "status": "draft"})
    
    # 创建 provider/agent(mock 会拦截实际调用,但需要 id 存在)
    prov_id = service.create_provider({"name": "mock", "provider_type": "openai_compatible", "api_key_encrypted": "", "base_url": ""})
    agent_id = service.create_agent({
        "name": "mock_agent", "task_type": "continue", "provider_id": prov_id, "system_prompt": "test prompt", 
        "model": "gpt-4", "temperature": 0.7, "max_tokens": 500,
    })
    
    # Mock stream_chapter_continue 返回续写片段"XYZ"(不含已有正文)
    def fake_continue(payload):
        from pixiv_novel_sync.ai.providers import AIStreamChunk
        yield AIStreamChunk(type="metadata", data={"job_id": "test_job"})
        for char in "XYZ":
            yield AIStreamChunk(type="delta", text=char, data={})
        yield AIStreamChunk(type="done", data={})
    
    import pixiv_novel_sync.ai.service
    original_continue = pixiv_novel_sync.ai.service.AIWritingService.stream_chapter_continue
    pixiv_novel_sync.ai.service.AIWritingService.stream_chapter_continue = fake_continue
    
    try:
        # 执行 pipeline(仅 continue 步骤)
        chunks = list(service.stream_chapter_pipeline({
            "project_id": proj_id, "chapter_id": ch_id, "steps": ["continue"], 
            "agent_ids": {"continue": agent_id},
        }))
        
        # 验证章节正文 = "已有正文ABC" + "XYZ"
        final_ch = service.get_chapter(ch_id)
        assert final_ch["content"] == "已有正文ABCXYZ", f"期望拼接,实际: {final_ch['content']}"
    finally:
        pixiv_novel_sync.ai.service.AIWritingService.stream_chapter_continue = original_continue
