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
        self._all_conns: set[sqlite3.Connection] = set()  # 弱引用集合供 close() 关闭全部

    @property
    def conn(self) -> sqlite3.Connection:
        """当前线程的 SQLite 连接,首次访问时 lazy 创建并初始化 PRAGMA。"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            # 每个连接独立开启 WAL + 设置超时
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            with self._lock:
                self._all_conns.add(conn)
        return self._local.conn

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
        避免多线程下 SQLITE_BUSY。嵌套调用是安全的（RLock）。
        """
        with self._lock:
            self._transaction_depth += 1
            outermost = self._transaction_depth == 1
            try:
                if outermost:
                    self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
                if outermost:
                    self.conn.commit()
            except BaseException:
                if outermost:
                    self.conn.rollback()
                raise
            finally:
                self._transaction_depth -= 1

    def close(self) -> None:
        """关闭所有线程的连接。"""
        with self._lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        if hasattr(self._local, "conn"):
            self._local.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
