"""数据库 Schema 和迁移相关方法。

本模块包含所有数据库表结构定义和迁移逻辑。
"""
import logging
import sqlite3
from typing import Any


logger = logging.getLogger(__name__)


class SchemaMixin:
    """数据库 Schema 和迁移相关方法的 Mixin 类。"""

    def init_schema(self) -> None:
        # PRAGMA 已在 conn property 中每连接执行,这里只建表
        with self._lock:
            self.conn.executescript(
                """

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                account TEXT,
                raw_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                last_checked_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS novels (
                novel_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                series_id INTEGER,
                title TEXT NOT NULL,
                caption TEXT,
                visible INTEGER NOT NULL,
                restrict_value TEXT NOT NULL,
                x_restrict INTEGER NOT NULL,
                text_length INTEGER NOT NULL,
                total_bookmarks INTEGER NOT NULL,
                total_views INTEGER NOT NULL,
                cover_url TEXT,
                tags_json TEXT NOT NULL,
                create_date TEXT,
                raw_json TEXT NOT NULL,
                meta_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS novel_texts (
                novel_id INTEGER PRIMARY KEY REFERENCES novels(novel_id) ON DELETE CASCADE,
                text_raw TEXT NOT NULL,
                has_content INTEGER NOT NULL DEFAULT 0,
                text_markdown TEXT,
                text_hash TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS assets (
                asset_id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id INTEGER NOT NULL REFERENCES novels(novel_id) ON DELETE CASCADE,
                asset_type TEXT NOT NULL,
                remote_url TEXT NOT NULL,
                local_path TEXT NOT NULL,
                file_hash TEXT,
                downloaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(novel_id, asset_type, remote_url)
            );

            CREATE TABLE IF NOT EXISTS sources (
                novel_id INTEGER NOT NULL REFERENCES novels(novel_id) ON DELETE CASCADE,
                source_type TEXT NOT NULL,
                source_key TEXT NOT NULL,
                discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (novel_id, source_type, source_key)
            );

            CREATE TABLE IF NOT EXISTS series (
                series_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                user_id INTEGER NOT NULL,
                cover_url TEXT,
                total_novels INTEGER DEFAULT 0,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS novel_fts USING fts5(
                novel_id UNINDEXED,
                title,
                caption,
                author_name,
                body
            );

            CREATE TABLE IF NOT EXISTS task_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                task_name TEXT NOT NULL,
                job_id TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_seconds REAL,
                stats_json TEXT,
                error_message TEXT,
                logs_json TEXT,
                is_auto_sync INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_task_logs_type ON task_logs(task_type);
            CREATE INDEX IF NOT EXISTS idx_task_logs_started_at ON task_logs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_task_logs_auto_sync ON task_logs(is_auto_sync);

            CREATE INDEX IF NOT EXISTS idx_novels_user_id ON novels(user_id);
            CREATE INDEX IF NOT EXISTS idx_novels_series_id ON novels(series_id);
            CREATE INDEX IF NOT EXISTS idx_novels_last_seen_at ON novels(last_seen_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sources_source_type ON sources(source_type);

            -- Phase 5性能:高频WHERE条件索引
            CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
            CREATE INDEX IF NOT EXISTS idx_assets_novel_id ON assets(novel_id);
            CREATE INDEX IF NOT EXISTS idx_sources_novel_id ON sources(novel_id);
            """
        )
        # 迁移：为旧版 users 表添加 status 和 last_checked_at 字段
        self._migrate_users_table()
        # 修复：重置错误标记为 cleared 的用户状态
        self._fix_cleared_status()
        # 迁移：为 novels 表添加 status 和 last_checked_at 字段
        self._migrate_novels_table()
        # 迁移：为 series 表添加 is_subscribed、status、last_checked_at 字段
        self._migrate_series_table()
        # 修复：将进程重启后遗留的 running 状态日志标记为 failed
        self._fix_stale_running_logs()
        # 迁移：创建待确认删除表
        self._migrate_pending_deletions_table()
        # 迁移：创建救援纠错和独立只读 Token 表
        self._migrate_rescue_tables()
        # 迁移：创建同步水位线表
        self._migrate_sync_watermarks_table()
        # 迁移：创建/升级预检查表。旧服务端库可能已有无 scope 的 sync_check_list。
        self.init_sync_check_table()
        # 迁移：创建 AI 创作工作台相关表
        self._migrate_ai_tables()
        # 迁移：创建偏好画像与推书相关表
        self._migrate_preference_tables()
        # 迁移：创建 AI 写作项目（章节/伏笔/状态记忆）相关表
        self._migrate_ai_writing_tables()
        # 迁移：创建阅读进度追踪表
        self._migrate_reading_progress_table()
        # 迁移：为旧版 novel_texts 表添加正文完整度辅助列
        self._migrate_novel_texts_table()
        self._migrate_core_foreign_keys()
        self._commit_if_needed()

    def _has_foreign_key(self, table_name: str, column_name: str, target_table: str) -> bool:
        return any(
            row[3] == column_name and row[2] == target_table
            for row in self.conn.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
        )

    def _migrate_core_foreign_keys(self) -> None:
        if not self._has_foreign_key("novel_texts", "novel_id", "novels"):
            self._rebuild_novel_texts_with_foreign_key()
        if not self._has_foreign_key("assets", "novel_id", "novels"):
            self._rebuild_assets_with_foreign_key()
        if not self._has_foreign_key("sources", "novel_id", "novels"):
            self._rebuild_sources_with_foreign_key()
        self.conn.execute("PRAGMA foreign_keys=ON")
        violations = self.conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            # 不再 RuntimeError：历史数据残留 FK 违规不应让全部路由 500。
            # 记录警告并继续，让应用保持可用，运维侧可据日志清理数据。
            logger.warning(
                "SQLite foreign_key_check violations detected (count=%d): %r. "
                "Application will continue to run; please clean up orphan rows.",
                len(violations),
                violations[:10],
            )

    def _rebuild_novel_texts_with_foreign_key(self) -> None:
        self._rebuild_table_with_foreign_key(
            "novel_texts",
            """
            CREATE TABLE novel_texts (
                novel_id INTEGER PRIMARY KEY REFERENCES novels(novel_id) ON DELETE CASCADE,
                text_raw TEXT NOT NULL,
                has_content INTEGER NOT NULL DEFAULT 0,
                text_markdown TEXT,
                text_hash TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            INSERT INTO novel_texts (novel_id, text_raw, has_content, text_markdown, text_hash, fetched_at)
            SELECT old.novel_id, old.text_raw, old.has_content, old.text_markdown, old.text_hash, old.fetched_at
            FROM novel_texts_old old
            JOIN novels n ON n.novel_id = old.novel_id
            """,
        )

    def _rebuild_assets_with_foreign_key(self) -> None:
        self._rebuild_table_with_foreign_key(
            "assets",
            """
            CREATE TABLE assets (
                asset_id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id INTEGER NOT NULL REFERENCES novels(novel_id) ON DELETE CASCADE,
                asset_type TEXT NOT NULL,
                remote_url TEXT NOT NULL,
                local_path TEXT NOT NULL,
                file_hash TEXT,
                downloaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(novel_id, asset_type, remote_url)
            )
            """,
            """
            INSERT OR IGNORE INTO assets (asset_id, novel_id, asset_type, remote_url, local_path, file_hash, downloaded_at)
            SELECT old.asset_id, old.novel_id, old.asset_type, old.remote_url, old.local_path, old.file_hash, old.downloaded_at
            FROM assets_old old
            JOIN novels n ON n.novel_id = old.novel_id
            """,
        )

    def _rebuild_sources_with_foreign_key(self) -> None:
        self._rebuild_table_with_foreign_key(
            "sources",
            """
            CREATE TABLE sources (
                novel_id INTEGER NOT NULL REFERENCES novels(novel_id) ON DELETE CASCADE,
                source_type TEXT NOT NULL,
                source_key TEXT NOT NULL,
                discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (novel_id, source_type, source_key)
            )
            """,
            """
            INSERT OR IGNORE INTO sources (novel_id, source_type, source_key, discovered_at)
            SELECT old.novel_id, old.source_type, old.source_key, old.discovered_at
            FROM sources_old old
            JOIN novels n ON n.novel_id = old.novel_id
            """,
        )

    def _rebuild_table_with_foreign_key(self, table_name: str, create_sql: str, copy_sql: str) -> None:
        old_name = f"{table_name}_old"
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute(f"DROP TABLE IF EXISTS {old_name}")
            self.conn.execute(f"ALTER TABLE {table_name} RENAME TO {old_name}")
            self.conn.execute(create_sql)
            self.conn.execute(copy_sql)
            self.conn.execute(f"DROP TABLE {old_name}")
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_users_table(self) -> None:
        """为旧版 users 表添加 status 和 last_checked_at 字段"""
        cursor = self.conn.execute("PRAGMA table_info(users)")
        columns = {row[1] for row in cursor.fetchall()}
        if "status" not in columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'")
        if "last_checked_at" not in columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN last_checked_at TEXT")

    def _fix_cleared_status(self) -> None:
        """重置错误标记为 cleared 的用户状态为 unknown"""
        try:
            self.conn.execute("UPDATE users SET status = 'unknown' WHERE status = 'cleared'")
            self._commit_if_needed()
        except Exception:
            pass

    def _fix_stale_running_logs(self) -> None:
        """将进程重启后遗留的 running 状态日志标记为 failed"""
        try:
            self.conn.execute(
                "UPDATE task_logs SET status = 'failed', error_message = '进程重启，任务中断', "
                "finished_at = CURRENT_TIMESTAMP WHERE status = 'running'"
            )
            self._commit_if_needed()
        except Exception:
            pass

    def _migrate_novels_table(self) -> None:
        """为 novels 表添加 status 和 last_checked_at 字段"""
        cursor = self.conn.execute("PRAGMA table_info(novels)")
        columns = {row[1] for row in cursor.fetchall()}
        if "status" not in columns:
            self.conn.execute("ALTER TABLE novels ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'")
        if "last_checked_at" not in columns:
            self.conn.execute("ALTER TABLE novels ADD COLUMN last_checked_at TEXT")

    def _migrate_novel_texts_table(self) -> None:
        """为旧版 novel_texts 表添加正文完整度辅助列并回填。"""
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(novel_texts)").fetchall()}
        if "has_content" not in columns:
            self.conn.execute(
                "ALTER TABLE novel_texts ADD COLUMN has_content INTEGER NOT NULL DEFAULT 0"
            )
            self.conn.execute(
                "UPDATE novel_texts SET has_content = CASE WHEN TRIM(text_raw) != '' THEN 1 ELSE 0 END"
            )

    def _migrate_rescue_tables(self) -> None:
        """创建救援人工纠错和单例 API Token 表。"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rescue_overrides (
                item_type TEXT NOT NULL CHECK (item_type IN ('novel', 'series')),
                item_id INTEGER NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('include', 'exclude')),
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (item_type, item_id)
            );

            CREATE INDEX IF NOT EXISTS idx_rescue_overrides_action
                ON rescue_overrides(action);

            CREATE TABLE IF NOT EXISTS rescue_api_token (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                token_hash TEXT NOT NULL,
                token_prefix TEXT NOT NULL,
                rotated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS rescue_catalog (
                item_type TEXT NOT NULL CHECK (item_type IN ('novel', 'series')),
                item_id INTEGER NOT NULL,
                content_kind TEXT NOT NULL CHECK (
                    content_kind IN ('series', 'series_chapter', 'standalone')
                ),
                series_id INTEGER,
                title TEXT NOT NULL,
                user_id INTEGER NOT NULL DEFAULT 0,
                author_name TEXT NOT NULL DEFAULT '',
                cover_url TEXT,
                rescue_state TEXT NOT NULL CHECK (rescue_state IN ('success', 'partial')),
                remote_status TEXT NOT NULL,
                eligibility_reason TEXT NOT NULL,
                expected_count INTEGER,
                local_count INTEGER NOT NULL DEFAULT 0,
                complete_count INTEGER NOT NULL DEFAULT 0,
                last_checked_at TEXT,
                updated_at TEXT,
                refreshed_at TEXT NOT NULL,
                PRIMARY KEY (item_type, item_id)
            );

            CREATE TABLE IF NOT EXISTS rescue_catalog_sources (
                item_type TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                source_kind TEXT NOT NULL CHECK (
                    source_kind IN (
                        'bookmark', 'subscribed_series', 'following_user',
                        'user_backup', 'other'
                    )
                ),
                source_type TEXT NOT NULL,
                source_key TEXT NOT NULL DEFAULT '',
                source_user_id INTEGER,
                source_user_name TEXT,
                PRIMARY KEY (item_type, item_id, source_kind, source_key),
                FOREIGN KEY (item_type, item_id)
                    REFERENCES rescue_catalog(item_type, item_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS rescue_catalog_meta (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                refreshed_at TEXT NOT NULL,
                item_count INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL
            );

            -- Last known novel-to-series links let incremental refreshes recover
            -- a parent after the visible chapter row is suppressed or deleted.
            -- Deliberately no FK: a raw novel delete must leave the old link
            -- available until refresh_rescue_item() consumes and removes it.
            CREATE TABLE IF NOT EXISTS rescue_catalog_memberships (
                novel_id INTEGER PRIMARY KEY,
                series_id INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_rescue_catalog_memberships_series
                ON rescue_catalog_memberships(series_id, novel_id);

            CREATE INDEX IF NOT EXISTS idx_rescue_catalog_kind_state
                ON rescue_catalog(content_kind, rescue_state);
            CREATE INDEX IF NOT EXISTS idx_rescue_catalog_checked
                ON rescue_catalog(last_checked_at DESC, item_id DESC);
            CREATE INDEX IF NOT EXISTS idx_rescue_catalog_updated
                ON rescue_catalog(updated_at DESC, item_id DESC);
            CREATE INDEX IF NOT EXISTS idx_rescue_catalog_sources_kind
                ON rescue_catalog_sources(source_kind, item_type, item_id);
            """
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO rescue_catalog_memberships (novel_id, series_id)
            SELECT novel_id, series_id
            FROM novels
            WHERE series_id IS NOT NULL
            """
        )
        self._commit_if_needed()

    def _migrate_series_table(self) -> None:
        """为 series 表添加 is_subscribed、status、last_checked_at 字段"""
        cursor = self.conn.execute("PRAGMA table_info(series)")
        columns = {row[1] for row in cursor.fetchall()}
        if "is_subscribed" not in columns:
            self.conn.execute("ALTER TABLE series ADD COLUMN is_subscribed INTEGER NOT NULL DEFAULT 0")
        if "status" not in columns:
            self.conn.execute("ALTER TABLE series ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'")
        if "last_checked_at" not in columns:
            self.conn.execute("ALTER TABLE series ADD COLUMN last_checked_at TEXT")

    def init_sync_check_table(self) -> None:
        """初始化同步检查表"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_check_list (
                scope TEXT NOT NULL DEFAULT '_',
                novel_id INTEGER NOT NULL,
                exists_local INTEGER NOT NULL DEFAULT 0,
                checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (scope, novel_id)
            );
            """
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(sync_check_list)").fetchall()}
        if "scope" not in columns:
            self.conn.executescript(
                """
                ALTER TABLE sync_check_list RENAME TO sync_check_list_old;
                CREATE TABLE sync_check_list (
                    scope TEXT NOT NULL DEFAULT '_',
                    novel_id INTEGER NOT NULL,
                    exists_local INTEGER NOT NULL DEFAULT 0,
                    checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scope, novel_id)
                );
                INSERT OR REPLACE INTO sync_check_list (scope, novel_id, exists_local, checked_at)
                SELECT '_', novel_id, exists_local, checked_at FROM sync_check_list_old;
                DROP TABLE sync_check_list_old;
                """
            )
        self._commit_if_needed()

    def _migrate_preference_tables(self) -> None:
        """创建偏好画像与推荐相关表。"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS preference_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                source_scope_json TEXT NOT NULL,
                stats_json TEXT NOT NULL,
                profile_json TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS recommendation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                search_plan_json TEXT NOT NULL,
                stats_json TEXT,
                error_message TEXT,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS recommendation_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                profile_id INTEGER NOT NULL,
                item_type TEXT NOT NULL,
                novel_id INTEGER,
                series_id INTEGER,
                title TEXT NOT NULL,
                author_id INTEGER,
                author_name TEXT,
                caption TEXT,
                tags_json TEXT NOT NULL,
                text_length INTEGER NOT NULL DEFAULT 0,
                series_total_text_length INTEGER NOT NULL DEFAULT 0,
                series_total_novels INTEGER NOT NULL DEFAULT 0,
                total_bookmarks INTEGER NOT NULL DEFAULT 0,
                total_views INTEGER NOT NULL DEFAULT 0,
                score REAL NOT NULL DEFAULT 0,
                reason TEXT,
                matched_json TEXT NOT NULL,
                source_query TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_recommendation_items_identity
              ON recommendation_items(item_type, COALESCE(novel_id, 0), COALESCE(series_id, 0));
            CREATE INDEX IF NOT EXISTS idx_recommendation_items_status ON recommendation_items(status);
            CREATE INDEX IF NOT EXISTS idx_recommendation_items_score ON recommendation_items(score DESC);

            CREATE TABLE IF NOT EXISTS recommendation_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT NOT NULL,
                novel_id INTEGER,
                series_id INTEGER,
                author_id INTEGER,
                feedback_type TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS recommendation_mutes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mute_type TEXT NOT NULL,
                mute_value TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(mute_type, mute_value)
            );

            CREATE INDEX IF NOT EXISTS idx_recommendation_feedback_author_id ON recommendation_feedback(author_id);
            CREATE INDEX IF NOT EXISTS idx_recommendation_feedback_series_id ON recommendation_feedback(series_id);
            CREATE INDEX IF NOT EXISTS idx_recommendation_feedback_novel_id ON recommendation_feedback(novel_id);

            -- 增量偏好分析累加器: 单行标量状态(id 固定为 1)
            CREATE TABLE IF NOT EXISTS preference_accumulator (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                novel_count INTEGER NOT NULL DEFAULT 0,
                series_novel_count INTEGER NOT NULL DEFAULT 0,
                total_chars INTEGER NOT NULL DEFAULT 0,
                length_buckets_json TEXT NOT NULL DEFAULT '{}',
                source_dist_json TEXT NOT NULL DEFAULT '{}',
                x_restrict_dist_json TEXT NOT NULL DEFAULT '{}',
                min_text_length INTEGER NOT NULL DEFAULT 1000,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            -- 词项累计计数: 标签/共现对/关键词/标题词/简介词/作者
            CREATE TABLE IF NOT EXISTS preference_term_counts (
                term_type TEXT NOT NULL,
                term TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (term_type, term)
            );

            CREATE INDEX IF NOT EXISTS idx_preference_term_counts_type_count
              ON preference_term_counts(term_type, count DESC);

            -- 已分析小说去重表
            CREATE TABLE IF NOT EXISTS preference_analyzed_novels (
                novel_id INTEGER PRIMARY KEY,
                analyzed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    def _migrate_ai_tables(self) -> None:
        """创建 AI 创作工作台相关表。"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL,
                base_url TEXT,
                api_key_encrypted TEXT,
                default_model TEXT,
                available_models_json TEXT,
                timeout_seconds INTEGER NOT NULL DEFAULT 120,
                max_retries INTEGER NOT NULL DEFAULT 2,
                proxy TEXT,
                context_window INTEGER NOT NULL DEFAULT 128000,
                stream_enabled INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                task_type TEXT NOT NULL,
                provider_id INTEGER NOT NULL REFERENCES ai_providers(id) ON DELETE RESTRICT,
                model TEXT,
                system_prompt TEXT NOT NULL,
                temperature REAL NOT NULL DEFAULT 0.8,
                top_p REAL NOT NULL DEFAULT 0.9,
                max_tokens INTEGER NOT NULL DEFAULT 4000,
                context_window INTEGER NOT NULL DEFAULT 16000,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL UNIQUE,
                task_type TEXT NOT NULL,
                agent_id INTEGER,
                status TEXT NOT NULL DEFAULT 'running',
                input_json TEXT NOT NULL,
                output_text TEXT,
                output_json TEXT,
                error_message TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source_job_id TEXT,
                parent_draft_id INTEGER,
                style_profile_id INTEGER,
                novel_profile_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source_type TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                metadata_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_style_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source_type TEXT,
                source_ids_json TEXT,
                profile_json TEXT NOT NULL,
                sample_prompt TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_novel_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source_type TEXT,
                source_ids_json TEXT,
                profile_json TEXT NOT NULL,
                continuation_prompt TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_prompt_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                template TEXT NOT NULL,
                description TEXT,
                is_builtin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_ai_agents_task_type ON ai_agents(task_type);
            CREATE INDEX IF NOT EXISTS idx_ai_agents_provider_id ON ai_agents(provider_id);
            CREATE INDEX IF NOT EXISTS idx_ai_jobs_job_id ON ai_jobs(job_id);
            CREATE INDEX IF NOT EXISTS idx_ai_jobs_created_at ON ai_jobs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_drafts_updated_at ON ai_drafts(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_documents_hash ON ai_documents(content_hash);
            CREATE INDEX IF NOT EXISTS idx_ai_prompt_templates_category ON ai_prompt_templates(category);
            """
        )
        # 迁移：为已有 ai_providers 表添加 context_window 列
        try:
            self.conn.execute("ALTER TABLE ai_providers ADD COLUMN context_window INTEGER NOT NULL DEFAULT 128000")
            self.conn.commit()
        except Exception:
            pass  # 列已存在则忽略
        # 迁移：为已有 ai_providers 表添加 stream_enabled 列
        try:
            self.conn.execute("ALTER TABLE ai_providers ADD COLUMN stream_enabled INTEGER NOT NULL DEFAULT 1")
            self.conn.commit()
        except Exception:
            pass

    def _migrate_pending_deletions_table(self) -> None:
        """创建待确认删除表"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_deletions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                title TEXT,
                author_name TEXT,
                cover_url TEXT,
                source_type TEXT,
                detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'pending',
                confirmed_at TEXT,
                restored_at TEXT,
                UNIQUE(item_type, item_id)
            );
            CREATE INDEX IF NOT EXISTS idx_pending_deletions_status ON pending_deletions(status);
            CREATE INDEX IF NOT EXISTS idx_pending_deletions_detected_at ON pending_deletions(detected_at DESC);
            CREATE INDEX IF NOT EXISTS idx_pending_deletions_item_type_status ON pending_deletions(item_type, status);
            """
        )

    def _migrate_sync_watermarks_table(self) -> None:
        """创建同步水位线表"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_watermarks (
                sync_type TEXT NOT NULL,
                key TEXT NOT NULL DEFAULT '_',
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (sync_type, key)
            );
            """
        )

    def _migrate_ai_writing_tables(self) -> None:
        """创建 AI 写作项目相关表（项目、章节、伏笔、状态记忆、对话向导）。"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_writing_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                outline_json TEXT,
                style_profile_id INTEGER,
                novel_profile_id INTEGER,
                settings_json TEXT,
                cover_path TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                chapter_number INTEGER NOT NULL,
                title TEXT,
                content TEXT,
                summary TEXT,
                key_events_json TEXT,
                outline TEXT,
                word_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'draft',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, chapter_number)
            );

            CREATE TABLE IF NOT EXISTS ai_foreshadows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                planted_chapter INTEGER,
                target_resolve_chapter INTEGER,
                resolved_chapter INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                importance TEXT NOT NULL DEFAULT 'normal',
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_project_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                state_type TEXT NOT NULL,
                content TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, state_type)
            );

            CREATE TABLE IF NOT EXISTS ai_chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id INTEGER,
                scope TEXT NOT NULL DEFAULT 'wizard',
                title TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'active',
                imported_project_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_ai_chapters_project ON ai_chapters(project_id, chapter_number);
            CREATE INDEX IF NOT EXISTS idx_ai_foreshadows_project ON ai_foreshadows(project_id, status);
            CREATE INDEX IF NOT EXISTS idx_ai_project_states_project ON ai_project_states(project_id);
            CREATE INDEX IF NOT EXISTS idx_ai_chat_sessions_scope ON ai_chat_sessions(scope, status);
            CREATE INDEX IF NOT EXISTS idx_ai_chat_messages_session ON ai_chat_messages(session_id);
            """
        )
        project_cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(ai_writing_projects)").fetchall()
        }
        if "cover_path" not in project_cols:
            self.conn.execute("ALTER TABLE ai_writing_projects ADD COLUMN cover_path TEXT")
        # 给已有 ai_chapters 表补 metadata_json 列（老库迁移）
        try:
            cols = {row[1] for row in self.conn.execute("PRAGMA table_info(ai_chapters)").fetchall()}
            if "metadata_json" not in cols:
                self.conn.execute("ALTER TABLE ai_chapters ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass

    def _migrate_reading_progress_table(self) -> None:
        """创建阅读进度追踪表。"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reading_progress (
                novel_id INTEGER PRIMARY KEY,
                progress INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'unread',
                last_read_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_reading_progress_status ON reading_progress(status);
            CREATE INDEX IF NOT EXISTS idx_reading_progress_last_read ON reading_progress(last_read_at DESC);
            """
        )
