"""
403 / Cloudflare 屏蔽与平台维护的处理测试。

异常从 ``scrapers.base`` 导入，用 Codomo（纯 HTTP，整批抓一次全国列表）做载体：
BlockedError / ScrapeNetworkError 从 scraper 一路传到 dispatcher，再由 monitor
的通用 source 熔断 + 通知节流处理。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import monitor
from monitor import run_once
from scrapers.base import (
    BlockedError,
    ScrapeNetworkError,
    ScrapeTask,
    UpstreamMaintenanceError,
    is_maintenance_body,
)
from notifier import BaseNotifier
from users import UserConfig
from config import AutoBookConfig, Config, TargetFilter
from storage import Storage


_PATCH_FETCH_ALL = "scrapers.codomo.CodomoScraper._fetch_all"


def _cfg(tmp_path) -> Config:
    return Config(
        check_interval=300,
        codomo_cities=[TargetFilter(name="Eindhoven", key="29")],
        db_path=tmp_path / "test.db", log_level="WARNING",
    )


# ─── BlockedError 传播测试 ──────────────────────────────────────

def _codomo_tasks(*pairs):
    return [
        ScrapeTask(source="codomo", city_key=key, city_display=name)
        for name, key in pairs
    ]


class TestBlockedErrorPropagation:
    """BlockedError 必须从 scraper 一路传到 dispatcher，不被中间层吞。"""

    def test_blocked_error_propagates_through_dispatch(self):
        from scrapers import dispatch_scrape_tasks

        with patch(_PATCH_FETCH_ALL, side_effect=BlockedError("test")):
            with pytest.raises(BlockedError):
                dispatch_scrape_tasks(_codomo_tasks(("Eindhoven", "29")))


# ─── ScrapeNetworkError 传播测试 ─────────────────────────────────

class TestScrapeNetworkErrorPropagation:
    def test_dispatch_raises_when_all_tasks_fail(self):
        from scrapers import dispatch_scrape_tasks

        with patch(_PATCH_FETCH_ALL, side_effect=ScrapeNetworkError("page 1 failed")):
            with pytest.raises(ScrapeNetworkError) as excinfo:
                dispatch_scrape_tasks(
                    _codomo_tasks(("Eindhoven", "29"), ("Amsterdam", "24"))
                )

        assert "全部 2 个任务网络失败" in str(excinfo.value)


# ─── monitor 的 BlockedError 处理（通用 source 熔断 + 通知节流）───

class TestMonitorBlockedHandling:
    def setup_method(self):
        monitor._throttle_notify_block.reset()

    def teardown_method(self):
        monitor._throttle_notify_block.reset()

    def _run(self, tmp_path, scrape_fn, user, notifications_capture=None):
        """跑一轮；唯一的 source 被挡时 run_once 会把 BlockedError 抛给 main_loop。"""
        cfg = _cfg(tmp_path)

        class CapturingNotifier(BaseNotifier):
            has_channels = True
            async def _send(self, t):
                if notifications_capture is not None:
                    notifications_capture.append(t)
                return True
            async def close(self): pass

        notifs = [(user, CapturingNotifier())]
        storage = Storage(tmp_path / "test.db", timezone_str="UTC")

        async def go():
            with patch("mcore.scrape_round.dispatch_scrape_tasks", side_effect=scrape_fn):
                await run_once(cfg, storage, notifs, dry_run=False)

        try:
            with pytest.raises(BlockedError):
                asyncio.run(go())
        finally:
            storage.close()

    @staticmethod
    def _user():
        return UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )

    def test_run_once_opens_source_circuit_on_blocked_error(self, tmp_path):
        scrape = lambda *a, **k: (_ for _ in ()).throw(BlockedError("cf block"))

        self._run(tmp_path, scrape, self._user())

        assert monitor._source_circuits.fail_streak("codomo") == 1
        assert monitor._source_circuits.remaining("codomo") > 0

    def test_block_is_not_pushed_to_users(self, tmp_path):
        """抓取被屏蔽是运维问题，用户既判断不了也处置不了。

        每小时被这类消息打扰几次，足够让人把整个通知渠道静音，连真正的房源
        通知一起埋掉。改由 _notify_admin_only 只发 admin（web 面板 + admin push）。
        """
        scrape = lambda *a, **k: (_ for _ in ()).throw(
            BlockedError("Cloudflare WAF 屏蔽（HTTP 403）")
        )
        notifs_received: list[str] = []

        self._run(tmp_path, scrape, self._user(), notifs_received)

        assert notifs_received == [], f"屏蔽通知发到用户渠道了: {notifs_received}"

    def _run_capturing_admin(self, tmp_path, scrape_fn, user, admin_msgs):
        """屏蔽通知只发 admin，所以捕获点也挪到 notify_admin_only。"""
        async def fake_admin(storage, web_notifier, msg, *, kind):
            if kind == "scrape_blocked":
                admin_msgs.append(msg)

        with patch("mcore.round_failures.notify_admin_only", side_effect=fake_admin):
            self._run(tmp_path, scrape_fn, user)

    def test_admin_notification_throttled_30min(self, tmp_path):
        scrape = lambda *a, **k: (_ for _ in ()).throw(BlockedError("test"))
        admin_msgs: list[str] = []

        for _ in range(3):
            self._run_capturing_admin(tmp_path, scrape, self._user(), admin_msgs)
            monitor._source_circuits.expire("codomo")

        assert len(admin_msgs) == 1, (
            f"30 分钟内多次屏蔽应该只发 1 条，实际 {len(admin_msgs)}"
        )

    def test_admin_notification_unthrottled_after_interval(self, tmp_path):
        scrape = lambda *a, **k: (_ for _ in ()).throw(BlockedError("test"))
        admin_msgs: list[str] = []

        self._run_capturing_admin(tmp_path, scrape, self._user(), admin_msgs)
        assert len(admin_msgs) == 1

        # 把节流窗口拨到过去
        monitor._throttle_notify_block.expire()
        monitor._source_circuits.expire("codomo")

        self._run_capturing_admin(tmp_path, scrape, self._user(), admin_msgs)
        assert len(admin_msgs) == 2, "超过 30 分钟后应该重新通知"


# ── 平台维护态（原 test_scraper_maintenance.py） ────────────────────
#
# 判定路径：scraper 拿到维护占位页时调 ``is_maintenance_body`` 并抛
# ``UpstreamMaintenanceError``（如 ``scrapers/codomo.py`` 的 ``_get``）。
# dispatcher 优先级 + monitor admin 通知逻辑见下。

_MAINT_HTML = """<!DOCTYPE html>
<html><body>
<h1>We'll be back soon</h1>
<p>We are currently performing scheduled maintenance to update our systems.
We anticipate being back online by 11:30(CET).</p>
</body></html>"""


class TestIsMaintenanceBody:
    def test_we_will_be_back_soon(self):
        assert is_maintenance_body("We'll be back soon")
        assert is_maintenance_body("We will be back soon")
        assert is_maintenance_body("WE'LL BE BACK SOON")

    def test_scheduled_maintenance(self):
        assert is_maintenance_body("Currently performing scheduled maintenance")

    def test_full_maintenance_page(self):
        assert is_maintenance_body(_MAINT_HTML)

    def test_empty_returns_false(self):
        assert is_maintenance_body("") is False
        assert is_maintenance_body(None) is False  # type: ignore[arg-type]

    def test_normal_html_returns_false(self):
        normal = "<html><body><h1>Codomo</h1><p>Find your home</p></body></html>"
        assert is_maintenance_body(normal) is False

    def test_json_returns_false(self):
        assert is_maintenance_body('{"data":{"products":{"items":[]}}}') is False


class TestDispatcherMaintenancePriority:
    def test_maintenance_wins_over_blocked(self):
        from contextlib import contextmanager
        from scrapers import dispatch_scrape_tasks

        tasks = [
            ScrapeTask(source="codomo", city_key="29", city_display="Eindhoven"),
            ScrapeTask(source="ourdomain", city_key="amsterdam", city_display="Amsterdam"),
        ]

        def fake_scrape(self, task):
            if task.source == "codomo":
                raise UpstreamMaintenanceError("codomo maintenance")
            raise BlockedError("ourdomain blocked")

        @contextmanager
        def _noop_batch(self):
            yield

        with patch("scrapers.codomo.CodomoScraper.scrape", fake_scrape), \
             patch("scrapers.codomo.CodomoScraper.batch_session", _noop_batch), \
             patch("scrapers.ourdomain.OurDomainScraper.scrape", fake_scrape):
            with pytest.raises(UpstreamMaintenanceError):
                dispatch_scrape_tasks(tasks)


class TestMonitorMaintenanceAdminNotify:
    def setup_method(self):
        import monitor
        monitor._throttle_notify_maintenance.reset()

    def teardown_method(self):
        import monitor
        monitor._throttle_notify_maintenance.reset()

    def _run(self, tmp_path, *, user_notify_capture, admin_notify_capture):
        import asyncio
        from monitor import run_once
        from notifier import BaseNotifier
        from users import UserConfig
        from config import AutoBookConfig
        from storage import Storage

        cfg = _cfg(tmp_path)

        class CapUserNotifier(BaseNotifier):
            has_channels = True
            async def _send(self, t):
                user_notify_capture.append(t)
                return True
            async def close(self): pass

        class CapAdminNotifier(BaseNotifier):
            has_channels = True
            async def _send(self, t): return True
            async def send_error(self, message):
                admin_notify_capture.append(message)
                return True
            async def close(self): pass

        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        notifs = [(user, CapUserNotifier())]
        admin = CapAdminNotifier()
        storage = Storage(tmp_path / "test.db", timezone_str="UTC")

        async def go():
            with patch(
                "mcore.scrape_round.dispatch_scrape_tasks",
                side_effect=UpstreamMaintenanceError("test maintenance"),
            ):
                await run_once(cfg, storage, notifs, web_notifier=admin, dry_run=False)

        try:
            asyncio.run(go())
        finally:
            storage.close()

    def test_first_maintenance_notifies_admin_only(self, tmp_path):
        user_msgs: list[str] = []
        admin_msgs: list[str] = []
        with pytest.raises(UpstreamMaintenanceError):
            self._run(
                tmp_path,
                user_notify_capture=user_msgs,
                admin_notify_capture=admin_msgs,
            )
        assert user_msgs == [], f"用户通道不应该收到维护通知，实际收到 {user_msgs}"
        assert len(admin_msgs) == 1, f"admin 应该收到 1 条，实际 {len(admin_msgs)}"
        assert "维护" in admin_msgs[0] or "maintenance" in admin_msgs[0].lower()

    def test_maintenance_notify_throttled_1h(self, tmp_path):
        user_msgs: list[str] = []
        admin_msgs: list[str] = []
        for _ in range(3):
            with pytest.raises(UpstreamMaintenanceError):
                self._run(
                    tmp_path,
                    user_notify_capture=user_msgs,
                    admin_notify_capture=admin_msgs,
                )
        assert len(admin_msgs) == 1, (
            f"1 小时内多次维护应该只发 1 条，实际 {len(admin_msgs)}"
        )

    def test_maintenance_notify_unthrottled_after_interval(self, tmp_path):
        import monitor
        user_msgs: list[str] = []
        admin_msgs: list[str] = []

        with pytest.raises(UpstreamMaintenanceError):
            self._run(tmp_path, user_notify_capture=user_msgs, admin_notify_capture=admin_msgs)
        assert len(admin_msgs) == 1

        monitor._throttle_notify_maintenance.expire()

        with pytest.raises(UpstreamMaintenanceError):
            self._run(tmp_path, user_notify_capture=user_msgs, admin_notify_capture=admin_msgs)
        assert len(admin_msgs) == 2

    def test_maintenance_writes_meta(self, tmp_path):
        from storage import Storage
        user_msgs: list[str] = []
        admin_msgs: list[str] = []
        with pytest.raises(UpstreamMaintenanceError):
            self._run(tmp_path, user_notify_capture=user_msgs, admin_notify_capture=admin_msgs)
        st = Storage(tmp_path / "test.db", timezone_str="UTC")
        try:
            assert st.get_meta("upstream_maintenance_seen_at", default="") != ""
            assert st.get_meta("upstream_maintenance_last_at", default="") != ""
        finally:
            st.close()


class TestMaintenanceIsNotReclassified:
    """维护异常必须原样抛出，不能被改判成网络错误。

    曾经的事故：重抛白名单漏了 ``UpstreamMaintenanceError``，它落进
    ``except Exception`` 被包成 ``ScrapeNetworkError``，日志一句话里同时写着
    「网络错误」和「平台维护中」，维护期走的是网络失败路径（5 分钟冷却 + ERROR）
    而不是设计好的维护路径（15 分钟安静冷却 + 不发用户告警 + dashboard banner）。
    """

    class _Resp:
        def __init__(self, status, text):
            self.status_code = status
            self.text = text

    def _session(self, *, resp=None, exc=None):

        class _S:
            def get(self, *a, **k):
                if exc is not None:
                    raise exc
                return resp
        return _S()

    def test_maintenance_page_raises_maintenance(self):
        from scrapers.codomo import CodomoScraper

        s = self._session(resp=self._Resp(200, _MAINT_HTML))
        with pytest.raises(UpstreamMaintenanceError):
            CodomoScraper()._get(s, 1)

    def test_maintenance_message_does_not_say_network(self):
        from scrapers.codomo import CodomoScraper

        s = self._session(resp=self._Resp(503, _MAINT_HTML))
        with pytest.raises(UpstreamMaintenanceError) as ei:
            CodomoScraper()._get(s, 1)
        assert "网络" not in str(ei.value)

    def test_request_errors_become_network_errors(self):
        """反向守卫：请求本身抛的异常归网络类，且保留异常链（代理识别靠它）。"""
        from scrapers.codomo import CodomoScraper

        boom = OSError("connection reset")
        s = self._session(exc=boom)
        with pytest.raises(ScrapeNetworkError) as ei:
            CodomoScraper()._get(s, 1)
        assert ei.value.__cause__ is boom

    def test_403_is_blocked(self):
        from scrapers.codomo import CodomoScraper

        s = self._session(resp=self._Resp(403, "<!DOCTYPE html>cloudflare"))
        with pytest.raises(BlockedError):
            CodomoScraper()._get(s, 1)
