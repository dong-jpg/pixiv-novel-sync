# 🔴 Critical Bugs 修复计划

**优先级**: P0 - 立即修复  
**预计工作量**: 2-3 小时  
**影响范围**: 并发安全、数据一致性

---

## Bug #1: 信号量泄漏 - 可能导致系统死锁

### 📍 位置
`src/pixiv_novel_sync/web/managers.py:314`

### 🐛 问题描述
```python
# 当前代码（有问题）
def _run_single_task(self, task_name: str):
    try:
        # ... 任务执行
    except Exception as exc:
        logger.error(f"Task {task_name} failed: {exc}")
    
    # ❌ 如果上面的 try 块因未捕获的异常退出，这行永远不会执行
    self.sync_job_manager._semaphore.release()
```

### 💥 影响
一次异常可能导致信号量永不释放，后续所有任务永久阻塞，系统进入死锁状态。

### ✅ 修复方案
```python
# 修复后代码
def _run_single_task(self, task_name: str):
    try:
        # ... 任务执行
    except Exception as exc:
        logger.error(f"Task {task_name} failed: {exc}")
    finally:
        # ✅ 无论如何都会释放信号量
        self.sync_job_manager._semaphore.release()
```

### 🔍 相关问题
- `web/managers.py:736` - SyncJobManager.start_job 同样问题
- `jobs/quick_sync.py:134` - run_check_bookmarks_task 需要追踪 acquired 状态

---

## Bug #2: SQLite 多线程竞态条件

### 📍 位置
`src/pixiv_novel_sync/ai/retrieval.py:61`

### 🐛 问题描述
```python
# 当前代码（有问题）
class TFIDFRetriever:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(
            db_path,
            check_same_thread=False  # ❌ 允许多线程但未加锁
        )
        self._lock = threading.Lock()
    
    def search(self, query: str):
        # ❌ 直接访问 self.conn，未加锁
        cursor = self.conn.execute(sql, params)
        return cursor.fetchall()
```

### 💥 影响
多线程并发调用 AI 检索时会触发 `SQLite objects created in a thread can only be used in that same thread` 错误，导致崩溃。

### ✅ 修复方案
```python
# 修复后代码
class TFIDFRetriever:
    def search(self, query: str):
        with self._lock:  # ✅ 所有数据库操作都加锁
            cursor = self.conn.execute(sql, params)
            results = cursor.fetchall()
        return results
    
    def index_chapter(self, ...):
        with self._lock:  # ✅ 写操作也加锁
            self.conn.execute("DELETE FROM ...")
            self.conn.executemany("INSERT INTO ...", chunks)
            self.conn.commit()
```

### 🔍 相关问题
- `ai/retrieval.py:233` - EmbeddingRetriever 同样问题
- `ai/retrieval.py:387` - APIEmbeddingRetriever 同样问题

---

## Bug #3: 外键约束未启用

### 📍 位置
`src/pixiv_novel_sync/storage/connection.py:19`

### 🐛 问题描述
```python
# 当前代码（有问题）
class Database:
    def _get_connection(self):
        if not hasattr(self._local, 'conn'):
            conn = sqlite3.connect(self.db_path)
            # ❌ 未启用外键约束
            self._local.conn = conn
        return self._local.conn
```

### 💥 影响
即使在 schema.py 中定义了外键，SQLite 默认不启用，导致：
- novels.user_id 可能指向不存在的用户
- novels.series_id 可能指向不存在的系列
- 级联删除不生效

### ✅ 修复方案
```python
# 修复后代码
class Database:
    def _get_connection(self):
        if not hasattr(self._local, 'conn'):
            conn = sqlite3.connect(self.db_path)
            # ✅ 启用外键约束
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn
```

---

## Bug #4: AutoSyncScheduler 锁外读取

### 📍 位置
`src/pixiv_novel_sync/web/managers.py:208-219`

### 🐛 问题描述
```python
# 当前代码（有问题）
def _should_run_task(self, task_name: str) -> bool:
    with self._lock:
        # ... 初始化 next_run
        self._task_next_run[task_name] = next_run
    
    # ❌ 锁外读取，可能 KeyError
    next_run = self._task_next_run[task_name]
    return now >= next_run
```

### ✅ 修复方案
```python
# 修复后代码
def _should_run_task(self, task_name: str) -> bool:
    with self._lock:
        # ✅ 所有读写都在锁内
        if task_name not in self._task_next_run:
            # ... 初始化
            self._task_next_run[task_name] = next_run
        
        next_run = self._task_next_run[task_name]
        should_run = now >= next_run
        
        if should_run:
            # 更新下次运行时间
            self._task_next_run[task_name] = calculate_next_run()
    
    return should_run
```

---

## Bug #5: 用户删除顺序错误

### 📍 位置
`src/pixiv_novel_sync/storage/users.py:259`

### 🐛 问题描述
```python
# 当前代码（有问题）
def delete_user(self, user_id: int):
    with self.transaction():
        # ❌ 错误的删除顺序
        conn.execute("DELETE FROM novel_fts WHERE ...")
        conn.execute("DELETE FROM sync_check_list WHERE ...")
        conn.execute("DELETE FROM novels WHERE ...")  # 如果这里失败
        conn.execute("DELETE FROM users WHERE ...")    # 前面的删除已生效
```

### 💥 影响
中间步骤失败会导致数据库处于不一致状态。

### ✅ 修复方案
```python
# 修复后代码
def delete_user(self, user_id: int):
    with self.transaction():
        # ✅ 正确的删除顺序：从属表 → 主表
        # 1. 删除小说相关的从属数据
        conn.execute("DELETE FROM novel_fts WHERE novel_id IN (...)")
        conn.execute("DELETE FROM assets WHERE novel_id IN (...)")
        conn.execute("DELETE FROM novel_texts WHERE novel_id IN (...)")
        conn.execute("DELETE FROM sources WHERE novel_id IN (...)")
        
        # 2. 删除小说主表
        conn.execute("DELETE FROM novels WHERE user_id=?", (user_id,))
        
        # 3. 删除用户相关数据
        conn.execute("DELETE FROM sync_check_list WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM recommendation_items WHERE user_id=?", (user_id,))
        
        # 4. 最后删除用户
        conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
```

---

## Bug #6: FTS 索引更新无事务保护

### 📍 位置
`src/pixiv_novel_sync/storage/novels.py:371`

### 🐛 问题描述
```python
# 当前代码（有问题）
def replace_fts(self, novel_id: int, title: str, text: str):
    # ❌ DELETE 和 INSERT 不在同一事务
    conn.execute("DELETE FROM novel_fts WHERE novel_id=?", (novel_id,))
    conn.execute("INSERT INTO novel_fts VALUES (?, ?, ?)", (novel_id, title, text))
```

### 💥 影响
INSERT 失败会导致 FTS 索引丢失，小说无法被搜索到。

### ✅ 修复方案
```python
# 修复后代码
def replace_fts(self, novel_id: int, title: str, text: str):
    with self.transaction():  # ✅ 使用事务保护
        conn.execute("DELETE FROM novel_fts WHERE novel_id=?", (novel_id,))
        conn.execute("INSERT INTO novel_fts VALUES (?, ?, ?)", (novel_id, title, text))
```

---

## 🛠️ 修复执行计划

### Phase 1: 核心并发安全 (P0)
**预计时间**: 1 小时

1. ✅ 修复信号量泄漏 (Bug #1)
   - `web/managers.py:314` - 添加 finally 块
   - `web/managers.py:736` - 改用 acquired 标志位
   - `jobs/quick_sync.py:134` - 追踪 acquired 状态

2. ✅ 修复 SQLite 竞态 (Bug #2)
   - `ai/retrieval.py` - 所有数据库操作加锁

3. ✅ 启用外键约束 (Bug #3)
   - `storage/connection.py:19` - 添加 PRAGMA

### Phase 2: 数据一致性 (P0)
**预计时间**: 30 分钟

4. ✅ 修复锁外读取 (Bug #4)
   - `web/managers.py:219` - 移入锁保护

5. ✅ 修复删除顺序 (Bug #5)
   - `storage/users.py:259` - 调整删除顺序

6. ✅ 添加事务保护 (Bug #6)
   - `storage/novels.py:371` - 包裹事务

### Phase 3: 验证测试 (P0)
**预计时间**: 30 分钟

- 编写并发测试用例
- 模拟异常场景验证信号量释放
- 验证外键约束生效
- 压力测试 AI 检索

---

## 📝 修复后检查清单

- [ ] 所有信号量 acquire/release 都在 try-finally 块中
- [ ] SQLite 连接的所有操作都在锁保护下
- [ ] PRAGMA foreign_keys=ON 已启用
- [ ] 共享状态的读写都在锁内
- [ ] 多表删除顺序正确（从属→主）
- [ ] 关键数据库操作都有事务保护
- [ ] 编写单元测试验证修复
- [ ] 更新 KNOWLEDGE_GRAPH.md 文档

---

## 🔄 后续改进建议

### 并发安全审计
- 审查所有 `threading.Lock` 使用
- 检查所有 `_semaphore.acquire()` 是否有对应 release
- 使用 `with semaphore:` 上下文管理器代替手动 acquire/release

### 资源管理改进
- 为 Session/连接类添加 `__enter__`/`__exit__`
- 使用 `contextlib.closing()` 确保资源关闭
- 实现 `__del__` 兜底清理

### 数据库约束加强
- 为 novels.user_id、novels.series_id 添加外键
- 为高频查询字段添加索引
- 定期 VACUUM 优化数据库

---

**预计总修复时间**: 2-3 小时  
**建议排期**: 本周内完成  
**风险评估**: 低（修复明确，影响可控）
