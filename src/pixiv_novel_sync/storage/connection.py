"""Database connection management layer."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class DatabaseConnection:
    """数据库连接管理基类。

    提供线程安全的连接池、事务管理和连接生命周期控制。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # threading.local 每线程连接:消除共享单连接导致的游标交错/ProgrammingError。
        # WAL 允许多个独立连接并发读,BEGIN IMMEDIATE 串行化写。
        self._local = threading.local()
        self._lock: threading.RLock = threading.RLock()  # 仅保护元状态(如 _all_conns)
        # generation 计数:close() 后其他线程持有的旧连接可被识别并重建。
        self._generation: int = 0
        # 连接 -> 所属线程,便于清理已死线程遗留的连接,避免泄漏。
        self._all_conns: dict[sqlite3.Connection, threading.Thread] = {}

    def _prune_dead_thread_conns_locked(self) -> None:
        """清理所属线程已退出的连接(调用方必须已持有 _lock)。"""
        for conn, thread in list(self._all_conns.items()):
            if not thread.is_alive():
                self._all_conns.pop(conn, None)
                try:
                    conn.close()
                except Exception:
                    pass

    @property
    def conn(self) -> sqlite3.Connection:
        """当前线程的 SQLite 连接,首次访问时 lazy 创建并初始化 PRAGMA。

        close() 之后 generation 计数递增;其他线程若仍持有旧连接,
        在此处检测到代际不一致会自动重建,避免使用已关闭的连接。
        """
        existing = getattr(self._local, "conn", None)
        if existing is not None and getattr(self._local, "generation", -1) == self._generation:
            return existing
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # 每个连接独立开启 WAL + 设置超时
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        self._local.conn = conn
        self._local.transaction_depth = 0
        with self._lock:
            self._prune_dead_thread_conns_locked()
            self._local.generation = self._generation
            self._all_conns[conn] = threading.current_thread()
        return conn

    @property
    def _transaction_depth(self) -> int:
        """当前线程的事务嵌套深度,thread-local 化避免跨线程串台。"""
        return getattr(self._local, "transaction_depth", 0)

    @_transaction_depth.setter
    def _transaction_depth(self, value: int) -> None:
        self._local.transaction_depth = value

    def _commit_if_needed(self) -> None:
        if self._transaction_depth == 0:
            self.conn.commit()

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """让一组 SELECT 共享 DEFERRED 快照，并安全加入已有事务。"""
        conn = self.conn
        owns_transaction = not conn.in_transaction
        if owns_transaction:
            conn.execute("BEGIN DEFERRED")
        try:
            yield conn
            if owns_transaction:
                conn.commit()
        except BaseException:
            if owns_transaction and conn.in_transaction:
                conn.rollback()
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务上下文：with db.transaction() as conn: ... 在退出时统一 commit / rollback。

        与 sqlite3 内置的隐式事务不同，使用显式 BEGIN IMMEDIATE 抢占写锁，
        避免多线程下 SQLITE_BUSY。嵌套调用是安全的（嵌套深度为 thread-local）。

        注意：不再在 yield 期间持有进程内 RLock —— 写串行化交给
        BEGIN IMMEDIATE + busy_timeout，其他线程的读/连接创建不会被阻塞。
        """
        conn = self.conn
        self._transaction_depth += 1
        outermost = self._transaction_depth == 1
        try:
            if outermost:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if outermost:
                conn.commit()
        except BaseException:
            if outermost and conn.in_transaction:
                conn.rollback()
            raise
        finally:
            self._transaction_depth -= 1

    def close(self) -> None:
        """关闭所有线程的连接。

        通过递增 generation 让其他线程的旧连接失效；它们下次访问
        conn 属性时会自动重建，而不是拿到已关闭的连接。
        """
        with self._lock:
            self._generation += 1
            conns = list(self._all_conns)
            self._all_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        self._local.conn = None
        self._local.transaction_depth = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
