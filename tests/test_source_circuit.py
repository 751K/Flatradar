"""每个 source 一个抓取熔断，策略按 source 配。

编排层不再有任何平台特例：熔断、canary 恢复探测都走同一条通用路径。

为什么每个 source 都要有：保留日志全量统计「整体抓取失败」次数里，xior 整源
RateLimitError 147 次、ourdomain / canvas 网络失败各六十多次——当年只有一个
source 装了熔断，保护装在了最不需要它的那个上。

策略按 source 配
----------------
- 403 要换出口 IP / 指纹才好 → 默认冷却中等
- Xior 的 429 按 IP 累积，等一会就好 → 冷却短，但**必须有**
- 429 是否该熔断按 source 配：其余平台的 429 由 scrapers 内部退避与自适应节奏处理
"""
from __future__ import annotations

import pytest

from mcore.circuit import CircuitPolicy, SourceCircuits
from scrapers.base import BlockedError, RateLimitError, ScrapeNetworkError
from storage import Storage


@pytest.fixture
def st(tmp_path):
    s = Storage(tmp_path / "t.db", timezone_str="UTC")
    yield s
    s.close()


@pytest.fixture
def cs(st):
    c = SourceCircuits()
    c.load(st)
    return c


class TestEverySourceGetsOne:
    def test_unknown_source_falls_back_to_a_default_policy(self, cs):
        """新接一个平台不该顺带获得「没有熔断」这个默认值。"""
        p = cs.policy("some-new-platform")
        assert isinstance(p, CircuitPolicy)
        assert p.base_cooldown > 0 and p.max_cooldown >= p.base_cooldown

    @pytest.mark.parametrize("source", [
        "codomo", "xior", "ourdomain", "canvas",
    ])
    def test_all_registered_sources_have_a_circuit(self, source, cs, st):
        cs.trip(source, BlockedError("403"), storage=st)
        assert cs.remaining(source) > 0

    def test_circuits_are_independent(self, cs, st):
        """一个 source 熔断不能牵连另一个。"""
        cs.trip("xior", RateLimitError("429"), storage=st)
        assert cs.remaining("xior") > 0
        assert cs.remaining("codomo") == 0


class TestPolicyIsPerSource:
    def test_codomo_uses_the_default_policy(self, cs):
        """Codomo 没有任何特例：走默认策略。"""
        assert cs.policy("codomo") == cs.policy("some-new-platform")

    def test_default_does_not_trip_on_rate_limit(self, cs, st):
        """默认策略的 429 由 scrapers 内部退避处理，不熔断。"""
        cs.trip("codomo", RateLimitError("429"), storage=st)
        assert cs.remaining("codomo") == 0

    def test_xior_trips_on_rate_limit(self, cs, st):
        """Xior 的 429 是按 IP 累积的，不退避就是接着撞——147 次实测。"""
        cs.trip("xior", RateLimitError("429"), storage=st)
        assert cs.remaining("xior") > 0

    def test_xior_cooldown_is_shorter_than_default(self, cs):
        """429 等一会就好，403 要换 IP。退避长度该反映这个差别。"""
        assert cs.policy("xior").base_cooldown < cs.policy("codomo").base_cooldown

    def test_network_errors_never_trip(self, cs, st):
        """网络失败已经有连续失败计数 + 冷却那条路，别叠两层退避。"""
        for source in ("codomo", "xior", "ourdomain"):
            cs.trip(source, ScrapeNetworkError("timeout"), storage=st)
            assert cs.remaining(source) == 0


class TestExponentialBackoffAndCanary:
    def test_cooldown_doubles_per_consecutive_trip(self, cs, st):
        got = [cs.trip("xior", RateLimitError("429"), storage=st) for _ in range(3)]
        assert got[1] == got[0] * 2 and got[2] == got[1] * 2

    def test_cooldown_is_capped(self, cs, st):
        for _ in range(20):
            last = cs.trip("xior", RateLimitError("429"), storage=st)
        assert last == cs.policy("xior").max_cooldown

    def test_open_then_canary_then_normal(self, cs, st):
        assert cs.plan("xior", n_tasks=4) == ("normal", 4)

        cs.trip("xior", RateLimitError("429"), storage=st)
        assert cs.plan("xior", n_tasks=4) == ("open", 0)

        cs.expire("xior", storage=st)          # 冷却到期，连败保留
        assert cs.plan("xior", n_tasks=4) == ("canary", 1)

        cs.recover("xior", storage=st)          # canary 成功
        assert cs.plan("xior", n_tasks=4) == ("normal", 4)

    def test_recovery_resets_the_streak(self, cs, st):
        for _ in range(3):
            cs.trip("xior", RateLimitError("429"), storage=st)
        cs.recover("xior", storage=st)
        first_again = cs.trip("xior", RateLimitError("429"), storage=st)
        assert first_again == cs.policy("xior").base_cooldown

    def test_no_tasks_means_nothing_to_plan(self, cs):
        assert cs.plan("xior", n_tasks=0) == ("none", 0)


class TestSurvivesRestart:
    def test_state_is_persisted_per_source(self, st):
        c1 = SourceCircuits(); c1.load(st)
        c1.trip("xior", RateLimitError("429"), storage=st)

        c2 = SourceCircuits(); c2.load(st)
        assert c2.remaining("xior") > 0, (
            "熔断状态没跨重启——部署一次就把退避清零，正是限流最狠的时候"
        )
        assert c2.remaining("codomo") == 0


class TestNeverBlocksStartup:
    """退避状态是**优化**，不是抓取的前提。读不出来最多退化成「这一轮不退避」，
    绝不该让它决定进程能不能起来。

    ``load()`` 的 docstring 一直写着「绝不抛」，但只有 meta 内容损坏被
    ``_as_float`` 兜住了；连接本身出问题（库被关掉、锁死、schema 损坏）会一路
    抛出去。迁移过程中真的踩到：测试关掉 Storage 之后 teardown 再碰熔断，
    10 个用例集体 ProgrammingError。
    """

    def test_closed_database_does_not_raise(self, tmp_path):
        from mcore.backoff import PersistedBackoff

        s = Storage(tmp_path / "t.db", timezone_str="UTC")
        s.close()
        b = PersistedBackoff("k", max_seconds=600)
        b.load(s)                      # 不抛
        assert b.remaining() == 0

    def test_circuits_load_survives_a_broken_storage(self, tmp_path):
        s = Storage(tmp_path / "t.db", timezone_str="UTC")
        s.close()
        c = SourceCircuits()
        c.load(s)                      # 不抛
        assert c.remaining("xior") == 0

    def test_trip_on_broken_storage_still_works_in_memory(self, tmp_path):
        """落库失败时熔断仍然生效——只是不跨重启，比完全不熔断强。"""
        s = Storage(tmp_path / "t.db", timezone_str="UTC")
        c = SourceCircuits()
        c.load(s)
        s.close()
        assert c.trip("xior", RateLimitError("429"), storage=s) > 0
        assert c.remaining("xior") > 0


class TestMonitorWiring:
    """守卫：monitor 必须真的用上 per-source 熔断，没有平台特例。"""

    def test_monitor_holds_source_circuits(self):
        import monitor

        assert isinstance(monitor._source_circuits, SourceCircuits)

    def test_platform_specific_circuits_are_gone(self):
        import monitor
        from mcore import scrape_round

        for mod in (monitor, scrape_round):
            for name in vars(mod):
                assert "h2s" not in name.lower(), (
                    f"{mod.__name__}.{name}：平台专属的熔断/调度又回来了"
                )

    def test_failures_trip_a_circuit(self):
        """源码级守卫：通用调度路径必须调 trip()。

        少了这一行，source 就还是「失败了下一轮原速再来」——xior 整源 429
        失败 147 次正是这么攒出来的。
        """
        import ast
        import pathlib

        src = pathlib.Path("mcore/scrape_round.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_dispatch_source":
                body = ast.unparse(node)
                assert "runtime.source_circuits.trip" in body, (
                    "通用调度路径没有打开熔断，source 依然裸奔"
                )
                assert "runtime.source_circuits.recover" in body, (
                    "canary 成功后没有关闭熔断，会一直停在 canary 模式"
                )
                return
        raise AssertionError("找不到 _dispatch_source，这条守卫已失效")
