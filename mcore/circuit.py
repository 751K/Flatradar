"""
mcore/circuit.py — 按 source 的抓取熔断
==========================================

每个 source 一个熔断器：整源失败后冷却一段时间，冷却到点只放 1 个 target 做
canary，成功才全量放开。状态落库，跨进程重启存活（见 mcore/backoff.py）。

策略按 source 配，不一刀切
--------------------------
各家的失败语义不同，退避参数也就不该相同：

- 403 一般要**换出口 IP / 指纹**才好 → 默认冷却中等（15 分钟起，最长 2 小时）
- Xior 的 429 按 IP 累积，**等一会就好** → 冷却短（10 分钟起，最长 1 小时），但必须有

**429 是否熔断也按 source 配**：多数平台的 429 由 scrapers 内部的
RATE_LIMIT_BACKOFF 与自适应节奏（mcore/pacing.py）处理，只有 Xior 需要熔断兜底。

网络错误一律不熔断：``monitor`` 已经有连续失败计数 + 冷却那条路，叠两层退避只会
让恢复变得难以预测。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from scrapers.base import BlockedError, RateLimitError

from .backoff import PersistedBackoff

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CircuitPolicy:
    """一个 source 的熔断参数。

    ``trips_on`` 是**异常类型白名单**——只有它列出的失败才打开熔断。刻意用白名单
    而不是黑名单：新增一种异常时默认不熔断，比默认熔断安全得多。
    """

    base_cooldown: int
    max_cooldown: int
    trips_on: tuple[type[BaseException], ...] = field(
        default_factory=lambda: (BlockedError,)
    )


#: Xior：429 按 IP 累积，等一会就好，所以冷却短得多，但必须有。
#: 10 分钟 ≈ 3–4 个轮次，足够让 window 内的计数掉下去。
_XIOR_POLICY = CircuitPolicy(
    base_cooldown=600, max_cooldown=3600,
    trips_on=(BlockedError, RateLimitError),
)

#: 其余 source 的默认。**新接一个平台不该顺带获得「没有熔断」这个默认值**。
#: 403 要换指纹/IP 才好，用中等长度的冷却。
_DEFAULT_POLICY = CircuitPolicy(base_cooldown=900, max_cooldown=7200)

_POLICIES: dict[str, CircuitPolicy] = {
    "xior": _XIOR_POLICY,
}


class SourceCircuits:
    """每个 source 一个熔断器，状态落库（跨进程重启存活）。"""

    def __init__(self) -> None:
        self._breakers: dict[str, PersistedBackoff] = {}
        self._storage = None

    # ── 生命周期 ────────────────────────────────────────────────────

    def load(self, storage) -> None:
        """进程启动时恢复所有已知 source 的熔断状态。"""
        self._storage = storage
        for source in self.known_sources():
            self._breaker(source).load(storage)

    @staticmethod
    def known_sources() -> tuple[str, ...]:
        """注册表里的全部 source。新增平台自动获得熔断，不需要改这里。"""
        try:
            import scrapers

            return tuple(sorted(scrapers.SCRAPER_REGISTRY))
        except Exception:  # pragma: no cover - 导入失败时退回已知策略
            return tuple(sorted(_POLICIES))

    def policy(self, source: str) -> CircuitPolicy:
        return _POLICIES.get(source, _DEFAULT_POLICY)

    def _breaker(self, source: str) -> PersistedBackoff:
        b = self._breakers.get(source)
        if b is None:
            b = PersistedBackoff(
                f"circuit_{source}", max_seconds=self.policy(source).max_cooldown,
            )
            if self._storage is not None:
                b.load(self._storage)
            self._breakers[source] = b
        return b

    # ── 读 ──────────────────────────────────────────────────────────

    def remaining(self, source: str) -> int:
        return self._breaker(source).remaining()

    def reason(self, source: str) -> str:
        return self._breaker(source).reason

    def fail_streak(self, source: str) -> int:
        return self._breaker(source).fail_streak

    def plan(self, source: str, *, n_tasks: int) -> tuple[str, int]:
        """本轮这个 source 该跑几个 task。

        Returns
        -------
        ``(mode, n)``
          none    没有任务
          open    熔断中，一个都不跑
          canary  冷却到期，只放 1 个探测恢复（成功了下一轮才全量放开）
          normal  正常
        """
        if n_tasks <= 0:
            return "none", 0
        b = self._breaker(source)
        if b.remaining() > 0:
            return "open", 0
        if b.fail_streak > 0:
            return "canary", 1
        return "normal", n_tasks

    # ── 写 ──────────────────────────────────────────────────────────

    def trips_on(self, source: str, exc: BaseException) -> bool:
        return isinstance(exc, self.policy(source).trips_on)

    def trip(self, source: str, exc: BaseException, *, storage=None) -> int:
        """按策略打开熔断；该异常不该熔断时返回 0（且不改任何状态）。"""
        policy = self.policy(source)
        if not isinstance(exc, policy.trips_on):
            return 0
        b = self._breaker(source)
        streak = b.bump()
        cooldown = min(
            policy.max_cooldown,
            policy.base_cooldown * (2 ** max(0, streak - 1)),
        )
        b.open(cooldown, reason=f"{type(exc).__name__}: {exc}",
               storage=storage or self._storage)
        logger.error(
            "🚫 source %s 熔断：连续第 %d 次（%s），暂停 %d 秒；其它 source 继续运行。原因: %s",
            source, streak, type(exc).__name__, cooldown, exc,
        )
        return cooldown

    def expire(self, source: str, *, storage=None) -> None:
        """冷却到点：允许 canary，但**保留连败计数**（下次失败继续往上爬）。"""
        self._breaker(source).expire(storage or self._storage)

    def recover(self, source: str, *, storage=None) -> None:
        """canary 成功：关闭熔断并清零连败。"""
        b = self._breaker(source)  # noqa: F841 - 下面要用
        if b.fail_streak or b.remaining():
            logger.info(
                "✅ source %s canary 成功，关闭熔断（之前连续失败 %d 次）",
                source, b.fail_streak,
            )
        b.reset(storage or self._storage)
