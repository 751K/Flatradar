"""预订结果不确定时的持久化保护：通用存储基类与「是否已发出变更请求」的上下文。"""
from __future__ import annotations

import contextvars
import hashlib
import os
import sqlite3
from contextlib import contextmanager

_current = contextvars.ContextVar('booking_attempt', default=None)


def account_key(identity):
    """平台账号标识（邮箱 / 用户名）的规范化哈希，保护表里不存明文账号。"""
    return hashlib.sha256(identity.strip().casefold().encode()).hexdigest()


class GuardStore:
    """预订保护记录的存储基类：只负责定位数据库文件与开连接。

    具体的保护表与语义由子类定义（目前只有 ``mcore.plaza_guard.PlazaGuard``）。
    每次操作单独开连接，是因为预订在 executor 线程里跑，不能共用主线程的
    Storage 连接。
    """

    def __init__(self, storage):
        self.path = storage.conn.execute('PRAGMA database_list').fetchone()[2]
        if not self.path:
            raise RuntimeError('持久化数据库不可用')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()


def process_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


@contextmanager
def attempt_context(guard, attempt):
    token = _current.set((guard, attempt))
    try:
        yield
    finally:
        _current.reset(token)


def record_mutation(listing):
    context = _current.get()
    if context:
        guard, attempt = context
        guard.update(attempt, mutated=1, listing=listing)
        return True
    return False


def mutation_started():
    context = _current.get()
    if not context:
        return False
    row = context[0].get_attempt(context[1])
    return bool(row and row['mutated'])
