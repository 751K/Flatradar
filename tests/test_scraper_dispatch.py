from __future__ import annotations

import asyncio
import threading
import pytest

import scrapers
from models import Listing
from scrapers.base import AbstractScraper, BlockedError, ScrapeResult, ScrapeTask


class _OkScraper(AbstractScraper):
    source = "ok"

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        listing = Listing(
            id=f"{task.source}-1",
            name="Ok Listing",
            status="Available",
            price_raw="€700",
            available_from="2026-06-01",
            features=[],
            url="https://example.com",
            city=task.city_display,
            source=task.source,
        )
        return ScrapeResult(task=task, listings=[listing], complete=True)


class _BlockedScraper(AbstractScraper):
    source = "blocked"

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        raise BlockedError("Cloudflare 403")


def test_dispatch_keeps_partial_success_when_one_source_blocked(monkeypatch):
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "ok", _OkScraper)
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "blocked", _BlockedScraper)
    tasks = [
        ScrapeTask(source="ok", city_key="1", city_display="Eindhoven"),
        ScrapeTask(source="blocked", city_key="2", city_display="Diemen"),
    ]

    listings, completeness = scrapers.dispatch_scrape_tasks(tasks)

    assert [l.source for l in listings] == ["ok"]
    assert completeness == {
        "ok:Eindhoven": True,
        "blocked:Diemen": False,
    }


def test_dispatch_still_raises_blocked_when_all_sources_blocked(monkeypatch):
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "blocked", _BlockedScraper)

    with pytest.raises(BlockedError):
        scrapers.dispatch_scrape_tasks([
            ScrapeTask(source="blocked", city_key="2", city_display="Diemen"),
        ])


def test_monitor_isolated_dispatch_runs_without_thread_asyncio_loop(monkeypatch):
    import monitor

    main_thread = threading.get_ident()

    def fake_dispatch(tasks, **kwargs):
        assert threading.get_ident() != main_thread
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return [], {"xior:Eindhoven": True}

    from mcore import scrape_round
    monkeypatch.setattr(scrape_round, "dispatch_scrape_tasks", fake_dispatch)

    async def run():
        return await monitor._dispatch_scrape_tasks_async(
            asyncio.get_running_loop(),
            [ScrapeTask(source="xior", city_key="p1", city_display="Eindhoven")],
            isolated=True,
        )

    listings, completeness = asyncio.run(run())

    assert listings == []
    assert completeness == {"xior:Eindhoven": True}


def test_isolated_dispatch_reuses_one_thread_across_rounds(monkeypatch):
    """浏览器型 source 必须每轮落在同一个线程上。

    回归：以前每轮 ``with ThreadPoolExecutor(...)`` 新建又销毁线程。
    Playwright 对象绑定创建线程，线程一换浏览器就作废，于是每轮都要重建
    浏览器并完整重过一次 CF 挑战。
    """
    import monitor

    seen: list[int] = []

    def fake_dispatch(tasks, **kwargs):
        seen.append(threading.get_ident())
        return [], {}

    from mcore import scrape_round
    monkeypatch.setattr(scrape_round, "dispatch_scrape_tasks", fake_dispatch)

    async def run_rounds():
        loop = asyncio.get_running_loop()
        for _ in range(3):
            await monitor._dispatch_scrape_tasks_async(
                loop, [], isolated=True, browser_source="xior"
            )

    asyncio.run(run_rounds())

    assert len(seen) == 3
    assert len(set(seen)) == 1, f"浏览器 source 抓取跨轮换了线程: {seen}"
    assert seen[0] != threading.get_ident()


def test_get_scraper_reuses_instance_across_calls(monkeypatch):
    """实例跨轮复用——浏览器型 source 的浏览器挂在实例上。"""
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "ok", _OkScraper)

    first = scrapers.get_scraper("ok")
    second = scrapers.get_scraper("ok")

    assert first is second


def test_get_scraper_rebuilds_when_registered_class_changes(monkeypatch):
    """注册表被换掉时不能继续返回用旧类建的实例。"""
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "ok", _OkScraper)
    first = scrapers.get_scraper("ok")

    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "ok", _BlockedScraper)
    second = scrapers.get_scraper("ok")

    assert second is not first
    assert isinstance(second, _BlockedScraper)


# ── Browser 复用回归 ────────────────────────────────────────────

def test_browser_source_reuses_one_browser_for_all_targets(monkeypatch):
    """浏览器型 source 多 target 批量抓取应该只建 1 个 BrowserFetcher。

    batch_session 确保一个浏览器实例服务于批次内所有 target；每个 target 一个
    浏览器等于每个 target 一整轮 CF 挑战。
    """
    import scrapers.xior as xior

    browser_instances = []

    class _FakeBrowserFetcher:
        def __init__(self, headless=True, profile=None):
            browser_instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ensure_initialized(self):
            pass

    def _scrape(self, task):
        self._ensure_browser()
        return ScrapeResult(task=task, listings=[], complete=True)

    monkeypatch.setattr(xior, "BrowserFetcher", _FakeBrowserFetcher)
    monkeypatch.setattr(xior.XiorScraper, "scrape", _scrape)
    scrapers.reset_scraper_instances()
    try:
        tasks = [
            ScrapeTask(source="xior", city_key=f"p{i}", city_display=f"B{i}")
            for i in range(5)
        ]
        scrapers.dispatch_scrape_tasks(tasks)
    finally:
        scrapers.reset_scraper_instances()

    assert len(browser_instances) == 1, f"应只建 1 个 BrowserFetcher，实际 {len(browser_instances)}"


def test_browser_backed_sources_all_run_on_the_isolated_thread(monkeypatch):
    """Xior 也必须走长存单线程，不能留在默认 executor。

    回归：曾经只有一个浏览器 source 被路由到隔离线程，Xior 迁到浏览器传输层
    后仍留在默认 executor。Playwright 对象绑定创建线程，默认
    executor 的线程会漂移，最终抛
    ``greenlet.error: Cannot switch to a different thread``。
    潜伏期取决于线程池碰巧复用了哪个线程。
    """
    import monitor

    assert "xior" in monitor._BROWSER_SOURCES
    # 纯 HTTP 的 source 不该被拉进浏览器线程——它没有 Playwright 对象，
    # 挤进去只会和浏览器抢那一个线程。
    assert "ourdomain" not in monitor._BROWSER_SOURCES
    assert "codomo" not in monitor._BROWSER_SOURCES


def test_each_browser_source_gets_its_own_thread(monkeypatch):
    """两个 Playwright sync 实例不能共存于同一线程。

    回归：把两个浏览器 source 路由到同一个长存线程后，第一个实例在该线程装上
    event loop，第二个 launch() 随即报
    ``Playwright Sync API inside the asyncio loop``，后一个整轮失败。

    目前只有 Xior 一个浏览器 source，这里用一个虚构的 source 名验证路由规则。
    """
    import monitor

    seen: dict[str, set[int]] = {}

    def fake_dispatch(tasks, **kwargs):
        src = tasks[0].source if tasks else "?"
        seen.setdefault(src, set()).add(threading.get_ident())
        return [], {}

    from mcore import scrape_round
    monkeypatch.setattr(scrape_round, "dispatch_scrape_tasks", fake_dispatch)

    async def run():
        loop = asyncio.get_running_loop()
        for _ in range(2):                      # 两轮，确认各自线程还稳定
            for src in ("browser_b", "xior"):
                await monitor._dispatch_scrape_tasks_async(
                    loop,
                    [ScrapeTask(source=src, city_key="1", city_display="C")],
                    isolated=True,
                    browser_source=src,
                )

    asyncio.run(run())

    assert set(seen) == {"browser_b", "xior"}
    # 每个 source 跨轮固定在同一条线程上
    assert all(len(v) == 1 for v in seen.values()), seen
    # 但两个 source 不能是同一条
    assert seen["browser_b"] != seen["xior"], seen


class TestNoUnwiredScraperHooks:
    """``AbstractScraper`` 上不留没人调的扩展点。

    基类曾挂着 ``prewarm_session()`` 与 ``try_book(listing)`` 两个 no-op 钩子，
    设想是「支持自动预订的平台各自实现」。实际从未接线：下单走 ``bookers/*``，
    全仓库没有一处调过它们。
    """

    def test_hooks_are_gone_from_the_base_class(self):
        from scrapers.base import AbstractScraper

        for name in ("prewarm_session", "try_book"):
            assert not hasattr(AbstractScraper, name), (
                f"AbstractScraper.{name} 又回来了——没有任何调用者，"
                "留着只会让人以为抓取层管预订"
            )

    def test_no_scraper_implements_them_either(self):
        import scrapers

        for source, cls in scrapers.SCRAPER_REGISTRY.items():
            for name in ("prewarm_session", "try_book"):
                assert not hasattr(cls, name), f"{source} 还实现着 {name}"

    def test_compat_shim_module_is_gone(self):
        """顶层 ``scraper.py`` 只剩 re-export，生产代码零 import，已删除。"""
        import importlib.util
        import pathlib

        assert not pathlib.Path("scraper.py").exists(), (
            "scraper.py 又回来了——它只是 scrapers.base 的 re-export 垫片"
        )
        assert importlib.util.find_spec("scrapers.base") is not None


class TestBrowserLifecycleIsShared:
    """浏览器生命周期只准有一份实现。

    浏览器型 scraper 的 ``_ensure_browser`` / ``_close_browser`` /
    ``invalidate_session`` / ``batch_session`` 曾是两份行级 75% 重复的拷贝。
    子类只该说明用哪个 SiteProfile、活多久、建好之后要不要额外重置点什么。

    现在都继承 ``scrapers._browser_backed.BrowserBackedScraper``。
    """

    def test_browser_sources_inherit_the_shared_base(self):
        import scrapers
        from scrapers._browser_backed import BrowserBackedScraper

        for source in ("xior",):
            cls = scrapers.SCRAPER_REGISTRY[source]
            assert issubclass(cls, BrowserBackedScraper), (
                f"{source} 没走共享的浏览器生命周期"
            )

    def test_subclasses_do_not_reimplement_the_lifecycle(self):
        """子类只准提供 profile / 存活时长 / 钩子，不准自己重写生命周期。"""
        import scrapers
        from scrapers._browser_backed import BrowserBackedScraper

        shared = ("_ensure_browser", "_close_browser",
                  "invalidate_session", "batch_session")
        for source in ("xior",):
            cls = scrapers.SCRAPER_REGISTRY[source]
            for name in shared:
                assert name not in vars(cls), (
                    f"{source} 又自己实现了 {name}——共享实现就白抽了"
                )
                assert getattr(cls, name) is getattr(BrowserBackedScraper, name)

    def test_xior_keeps_its_own_profile_and_lifetime(self):
        """共享的是流程，不是参数：Xior 靠频繁换 IP 摊开限流，存活期远短于默认。"""
        from browser_fetcher import XIOR_PROFILE
        from scrapers._browser_backed import BrowserBackedScraper
        import scrapers

        xior_cls = scrapers.SCRAPER_REGISTRY["xior"]
        assert xior_cls._BROWSER_PROFILE is XIOR_PROFILE
        assert xior_cls._BROWSER_MAX_AGE < BrowserBackedScraper._BROWSER_MAX_AGE

    def test_the_fetcher_factory_stays_patchable_per_module(self):
        """``_new_fetcher`` 必须由子类覆盖，引用**自己模块**的 BrowserFetcher。

        这看着像多余的一行，但它是测试的接缝。迁移过程中把它收进基类之后，
        所有 ``monkeypatch.setattr(scrapers.xior, "BrowserFetcher", …)``
        的桩**全部失效**——而失效的后果不是测试报错，是测试真的去启动一个
        Chromium（cloakbrowser 的升级提示直接打进了测试输出才发现）。
        """
        import scrapers

        for source in ("xior",):
            cls = scrapers.SCRAPER_REGISTRY[source]
            assert "_new_fetcher" in vars(cls), (
                f"{source} 没覆盖 _new_fetcher——测试的打桩接缝会失效，"
                "用例会去启动真实浏览器"
            )


def test_no_method_is_silently_shadowed_by_a_duplicate_definition():
    """同名方法在一个类里定义两次时，后一个静默覆盖前一个。

    迁移时踩到过：重排方法位置留下了两份 ``_begin_batch``，Python 不报任何错，
    只是后面那份（旧的）赢了，新加的字段永远不被赋值——症状是一条测试莫名其妙
    失败，而代码看起来完全正确。
    """
    import ast
    import pathlib

    for path in pathlib.Path("scrapers").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            names = [n.name for n in node.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            dupes = {n for n in names if names.count(n) > 1}
            assert not dupes, f"{path}:{node.name} 里重复定义了 {dupes}"


class TestCompletenessKeyHasOneImplementation:
    """completeness 字典的 key 构造只准有一份实现。

    monitor 曾在 dispatch 编排里自己写了一个 ``_ckey``，
    注释是「与 ``scrapers._completeness_key`` 保持一致的 key 构造」——**靠注释
    维持的一致性不是一致性**。两边必须同时改，而没有任何东西保证这点。

    key 形态错开的后果不是报错，是**静默的错误收敛**：monitor 写进
    ``completeness_all`` 的 key 与 dispatcher 返回的对不上，
    ``mark_stale_listings`` 就会拿一个 source 的完整性去收敛另一个 source 的
    listing，或者干脆漏掉某个城市的收敛。
    """

    def test_monitor_does_not_reimplement_it(self):
        import inspect

        import monitor

        src = inspect.getsource(monitor.run_once)
        assert "def _ckey(" not in src, (
            "monitor 又自己写了一份 key 构造——两边必须同时改，靠注释维持不住"
        )

    def test_monitor_imports_the_shared_one(self):
        import scrapers

        from mcore import scrape_round
        assert scrape_round.completeness_key is scrapers.completeness_key

    @pytest.mark.parametrize("multi,expected", [
        (True, "codomo:Eindhoven"),
        (False, "Eindhoven"),
    ])
    def test_key_shape(self, multi, expected):
        import scrapers

        assert scrapers.completeness_key(
            "codomo", "Eindhoven", multi_source=multi,
        ) == expected

    def test_dispatcher_output_matches_the_shared_helper(self):
        """行为级守卫：dispatcher 真正产出的 key 必须等于共享函数算出来的。"""
        import scrapers
        from scrapers.base import ScrapeResult

        class _Fake(scrapers.base.AbstractScraper):
            source = "codomo"

            def scrape(self, task):
                return ScrapeResult(task=task, listings=[], complete=True)

        monkey = scrapers.SCRAPER_REGISTRY.copy()
        scrapers.SCRAPER_REGISTRY["codomo"] = _Fake
        scrapers.reset_scraper_instances()
        try:
            tasks = [ScrapeTask(source="codomo", city_key="29",
                                city_display="Eindhoven")]
            for multi in (True, False):
                _, completeness = scrapers.dispatch_scrape_tasks(
                    tasks, multi_source=multi,
                )
                assert set(completeness) == {
                    scrapers.completeness_key(
                        "codomo", "Eindhoven", multi_source=multi,
                    )
                }
        finally:
            scrapers.SCRAPER_REGISTRY.clear()
            scrapers.SCRAPER_REGISTRY.update(monkey)
            scrapers.reset_scraper_instances()


class TestDeadBrowserIsNotReused:
    """浏览器进程没了就得丢弃重建，不能因为「对象还在、年龄没到」就接着用。

    怎么走到这一步的
    ----------------
    Playwright 抛 ``Target page, context or browser has been closed`` 时::

        fetch() 里 evaluate 抛
          → scraper 第 1 页 → 改判成 ScrapeNetworkError
          → dispatcher 的 ScrapeNetworkError 分支：只记 network_failures，
            **不 _safe_invalidate**
          → 下一轮 _ensure_browser() 看到 _fetcher 非 None 且没超龄 → 原样复用

    dispatcher 那条 ``except Exception → _safe_invalidate`` 的注释写着「这类异常
    通常意味着底层会话已不可用」——正是为这种情况准备的，但浏览器崩溃在到达它
    之前就被改判成网络错误了。

    而 ``_ensure_browser`` 只检查「非 None」和「年龄」，**没有存活性检查**。
    阻塞窗口 = ``_BROWSER_MAX_AGE``（Xior 15 分钟，基类默认 2 小时）。

    ⚠️ 这条是**路径风险，不是已观测事故**：线上日志里那 2 次
    ``Target closed`` 都发生在持久化 profile 启动回退里，已经被处理掉了。
    修它是因为窗口太长、代价太低，不是因为它烧过。

    为什么不改成「ScrapeNetworkError 也丢会话」
    ------------------------------------------
    那样每次代理抖动都会丢掉一个好会话，下一轮要重过一整轮 CF 挑战。存活性
    检查是本地标志位，不发 IPC，几乎零成本，而且判的是真正的那件事。
    """

    @staticmethod
    def _scraper(monkeypatch, alive_after_first=True):
        import scrapers.xior as xior

        built = []

        class _Fake:
            def __init__(self, headless=True, profile=None):
                built.append(self)
                self.is_alive = True

            def __enter__(self): return self
            def __exit__(self, *a): return False
            def ensure_initialized(self): pass

        monkeypatch.setattr(xior, "BrowserFetcher", _Fake)
        s = xior.XiorScraper()
        return s, built

    def test_dead_browser_is_discarded_and_rebuilt(self, monkeypatch):
        s, built = self._scraper(monkeypatch)
        first = s._ensure_browser()
        assert len(built) == 1

        # 活着 → 复用
        assert s._ensure_browser() is first
        assert len(built) == 1

        # 进程没了 → 必须重建
        first.is_alive = False
        second = s._ensure_browser()
        assert second is not first, (
            "死掉的浏览器被原样复用了——之后每一轮都会以同样方式失败，"
            "直到 _BROWSER_MAX_AGE 才自愈"
        )
        assert len(built) == 2

    def test_real_fetcher_exposes_liveness(self):
        """``_ensure_browser`` 用 getattr 兜底（测试替身没有这个属性），

        所以真实类必须真的有它，否则存活检查会静默退化成「永远活着」。
        """
        from browser_fetcher import BrowserFetcher

        assert isinstance(
            getattr(BrowserFetcher, "is_alive", None), property
        ), "BrowserFetcher.is_alive 不见了，存活检查会静默失效"

    def test_liveness_is_false_before_launch(self):
        from browser_fetcher import BrowserFetcher

        f = BrowserFetcher.__new__(BrowserFetcher)
        f._browser = None
        f._page = None
        assert f.is_alive is False

    def test_liveness_is_false_when_page_closed(self):
        from browser_fetcher import BrowserFetcher

        class _Page:
            def is_closed(self): return True

        class _Browser:
            def is_connected(self): return True

        f = BrowserFetcher.__new__(BrowserFetcher)
        f._browser = _Browser()
        f._page = _Page()
        assert f.is_alive is False

    def test_liveness_is_false_when_browser_disconnected(self):
        from browser_fetcher import BrowserFetcher

        class _Page:
            def is_closed(self): return False

        class _Browser:
            def is_connected(self): return False

        f = BrowserFetcher.__new__(BrowserFetcher)
        f._browser = _Browser()
        f._page = _Page()
        assert f.is_alive is False

    def test_persistent_context_liveness_goes_through_its_browser(self):
        """``launch_persistent_context()`` 返回的是 BrowserContext，它没有
        ``is_connected``——得从 ``.browser`` 上问。两种返回物本类其余部分都不
        区分，存活检查也不该例外。"""
        from browser_fetcher import BrowserFetcher

        class _Page:
            def is_closed(self): return False

        class _Ctx:
            class browser:
                @staticmethod
                def is_connected(): return False

        f = BrowserFetcher.__new__(BrowserFetcher)
        f._browser = _Ctx()
        f._page = _Page()
        assert f.is_alive is False
