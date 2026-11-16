"""通知节流的判定函数。

节流状态本身在 ``mcore.state.runtime.throttles``；这里只提供按间隔 claim 的
薄封装。日志通道仍用 ``monitor``。
"""
from __future__ import annotations

import logging

from mcore.state import (
    BLOCK_NOTIFY_INTERVAL,
    INTERNAL_NOTIFY_INTERVAL,
    MAINTENANCE_NOTIFY_INTERVAL,
    PROXY_NOTIFY_INTERVAL,
    runtime,
)

logger = logging.getLogger("monitor")


def _storage():
    return runtime.throttle_storage


def should_notify_block() -> bool:
    """是否该发屏蔽通知。30 分钟最多一次，避免持续屏蔽时刷屏。"""
    return runtime.throttles["block"].claim(
        BLOCK_NOTIFY_INTERVAL, storage=_storage())


def should_notify_maintenance() -> bool:
    """是否该给 admin 发维护通知。1 小时最多一次。"""
    return runtime.throttles["maintenance"].claim(
        MAINTENANCE_NOTIFY_INTERVAL, storage=_storage())


def should_notify_proxy() -> bool:
    """是否该给 admin 发代理失效通知。30 分钟最多一次。"""
    return runtime.throttles["proxy"].claim(
        PROXY_NOTIFY_INTERVAL, storage=_storage())


def should_notify_internal() -> bool:
    """是否该给 admin 发未分类/管线内部错误通知。30 分钟最多一次。"""
    return runtime.throttles["internal"].claim(
        INTERNAL_NOTIFY_INTERVAL, storage=_storage())
