"""
browser_fetcher.py — 站点无关的浏览器传输层
==============================================

给被 Cloudflare 挑战保护的站点提供统一的请求能力：过挑战 → 等 clearance →
在浏览器内发**同源**请求。浏览器内 fetch() 自动携带 cookies / TLS 指纹 /
CF clearance token，无需手动管理会话。

站点差异全部收在 ``SiteProfile`` 里（challenge_url / 默认头 / clearance 判据
/ 维护检测钩子 / 是否轮换出口 IP），其余流程对所有站点通用。当前只有
``XIOR_PROFILE``（WordPress admin-ajax）。

使用者：``scrapers/xior.py``（经 ``scrapers/_browser_backed.py``）。

线程安全
--------
每个 BrowserFetcher 实例**绑定创建它的线程**——Playwright 对象换线程即失效。
Scraper 侧每个浏览器型 source 运行在独立常驻子进程的主线程中。

两个独立的 Playwright sync 实例**不能共存于同一线程**（第一个会在该线程装上
event loop，第二个 launch() 随即被判成「在 asyncio loop 里用同步 API」），
所以不同浏览器型 source 不共用工作进程。
"""
from __future__ import annotations

import logging
import os
import platform
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional
from json import dumps as _json_dumps

logger = logging.getLogger(__name__)

# ── 延迟导入异常类 ──────────────────────────────────────────────────
# browser_fetcher 被 scrapers/xior 导入，而 scrapers.base（定义异常的模块）
# 通过 scrapers/__init__.py 提前触发了 xior 的加载。若在模块顶层 import
# scrapers.base 会导致循环导入。异常只在实际遇到错误时才需要，故延迟到首次
# raise 时加载。
_exc_cache: dict[str, type] = {}


def _exc(name: str) -> type:
    if name not in _exc_cache:
        from scrapers.base import (  # noqa: E402
            BlockedError,
            RateLimitError,
            ScrapeNetworkError,
            UpstreamMaintenanceError,
        )
        _exc_cache.update({
            "BlockedError": BlockedError,
            "RateLimitError": RateLimitError,
            "ScrapeNetworkError": ScrapeNetworkError,
            "UpstreamMaintenanceError": UpstreamMaintenanceError,
        })
    return _exc_cache[name]


_XIOR_MAIN_PAGE = "https://www.xiorstudenthousing.eu/netherlands/"
_XIOR_AJAX_PATH = "/wp-admin/admin-ajax.php"

# CF 挑战页判据。几个看起来能用但实测不能用的候选：
#   - ``challenges.cloudflare.com`` / ``/cdn-cgi/challenge-platform/``：
#     挑战解开后的真实页面里同样存在（CSP 头 + 站点自带 turnstile），
#     用它们判定会永远认为挑战没过。
#   - URL 里的 ``__cf_chl_rt_tk``：CF 靠 ``history.replaceState`` 回写 URL，
#     时机不定；实测挑战已解开、真实 DOM 已就位时 URL 仍可能带着它，
#     用它判定会把正常会话误判成被挡。
# 只有挑战页脚本自身的 ``_cf_chl_opt`` 会随文档被真实页面替换而消失。
_CF_CHALLENGE_HTML_MARKER = "_cf_chl_opt"

#: 页面级默认超时（毫秒）。覆盖 content() / 等待类操作；**不覆盖 evaluate()**。
#: 见 _open_browser 之后那段注释。
_PAGE_DEFAULT_TIMEOUT_MS = float(
    os.environ.get("BROWSER_PAGE_TIMEOUT_MS") or "45000")

# 响应体里出现这些 = 拿到的是 CF 挑战页而不是真实响应。用于把「clearance 还
# 没生效」和「这个 IP 被封了」区分开——前者重新导航就好，后者得换 IP。
_CF_CHALLENGE_PENDING_MARKERS: tuple[str, ...] = (
    _CF_CHALLENGE_HTML_MARKER,
    "just a moment",
    "cdn-cgi/challenge-platform",
)

# 挑战解开的等待上限。实测差异很大：macOS 本地约 3s，1 CPU 的生产 VPS 上
# headless Chromium 跑完 challenge 要 30s 量级。上限按最慢的环境留足余量，
# 超时说明这个 IP 当前过不去，交给上层熔断而不是硬发请求。
# Chromium 在代理层失败时给的错误码。真实原因（配额耗尽、认证失败、代理宕机）
# 只有代理自己知道，到了 Playwright 这层一律被压成这几个码，所以命中时必须
# 另行探测——见 ``_describe_navigation_failure``。
_PROXY_ERROR_MARKERS: tuple[str, ...] = (
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_NO_SUPPORTED_PROXIES",
    "ERR_PROXY_CERTIFICATE_INVALID",
)

_CHALLENGE_CLEAR_TIMEOUT = 90.0
_CHALLENGE_POLL_INTERVAL = 1.0


# 单次导航后等 clearance cookie 落地的上限（仅配了 clearance_probe 的 profile）。
# token 只能靠重新导航签发，超过这个窗口还没落地，继续轮询是白打必然 403 的
# 请求，换一次导航才有意义。
_CLEARANCE_TIMEOUT = 25.0
_CLEARANCE_POLL_INTERVAL = 2.0

# 初始化最多导航几次。每次都是一轮完整的 CF 挑战，次数过多反而加重怀疑。
_INIT_ATTEMPTS = 3
_XIOR_AJAX_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}


# ── 资源拦截 ────────────────────────────────────────────────────────
# 代理按流量计费，而浏览器为了过挑战会把整张页面连同图片、字体、统计脚本
# 一并下载。2026-08-04 全天代理侧记录：985MB 中约 97MB 属于此类，与房源数据
# 无关。我们要的只是 DOM 与 cf_clearance cookie。
#
# 按类型拦。stylesheet 不在其中——CF 的行为检测会读渲染结果，去掉样式表等于
# 改变页面的呈现方式，省下的量（合计不足 5MB）不值这个风险。
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})

# 按域名整体拦：统计、广告、客服挂件、地理编码。都是页面自己加载的第三方，
# 与「页面是否已渲染完成」无关。
#
# 刻意不含 cdn.jsdelivr.net（4.5MB/天）：站点的业务 JS 也走它，无法从域名
# 区分，拦错会连页面本身一起弄坏。
_BLOCKED_DOMAINS: tuple[str, ...] = (
    "googletagmanager.com",
    "google-analytics.com",
    "googlesyndication.com",
    "fonts.gstatic.com",
    "fonts.googleapis.com",
    "fontawesome.com",
    # cookieyes 的 CDN 挂在另一个注册域上（cdn-cookieyes.com，不是
    # cookieyes.com 的子域），按点边界匹配吃不到，必须单列。它还是这家
    # 三个域里最大的那个。
    "cookieyes.com",
    "cdn-cookieyes.com",
    "trustpilot.com",
    "chatbase.co",
    "ahrefs.com",
    "komoot.io",
)

# 无论类型与域名，这些一律放行——挑战本身要靠它们跑完。
# cloudflareinsights 是 CF 自家的 beacon，量很小（0.4MB/天），拦它省不下什么，
# 却可能让这个会话在 CF 眼里显得不完整。
_NEVER_BLOCKED: tuple[str, ...] = (
    "challenges.cloudflare.com",
    "cloudflareinsights.com",
)


def _should_block(url: str, resource_type: str) -> bool:
    """这个子请求该不该拦。纯函数，便于单测覆盖各种 URL 形态。"""
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False  # 解析不了就放行，拦错的代价高于多下一次
    if not host:
        return False
    if any(host == d or host.endswith("." + d) for d in _NEVER_BLOCKED):
        return False
    if any(host == d or host.endswith("." + d) for d in _BLOCKED_DOMAINS):
        return True
    return resource_type in _BLOCKED_RESOURCE_TYPES


# ── 持久化 profile ──────────────────────────────────────────────────
# 挑战载荷占了代理流量的一多半（2026-08-04：985MB 中 558MB），成因是每次重建
# 浏览器都是全新的空缓存。而 launch() + new_page() 走的是 incognito context，
# HTTP 缓存只在内存里，浏览器一关即弃——只给 --disk-cache-dir 无济于事，实测
# 缓存目录里只会留下几个索引文件，字节数一点不降。
#
# 必须换成 launch_persistent_context()。本地实测（2026-08-05，某站点首页）：
#   冷 profile  3.93 MB
#   暖 profile  0.25 MB   143 个请求命中磁盘缓存，页面照常渲染
#
# 一个 profile 目录同一时刻只能被一个 Chromium 打开，而同一 source 可能同时有
# 不止一个浏览器（例如工具脚本与常驻抓取）。因此按槽位加文件锁，抢不到就
# 退回临时 profile——省流量不能以抢不到浏览器为代价。
_PROFILE_SLOTS = 3

# 每个槽位的磁盘缓存上限。生产 VPS 磁盘已用 82%，不能让它无限长。
# 128MB 足够装下挑战载荷与站点静态资源（实测暖 profile 全目录约 12MB）。
_DISK_CACHE_SIZE = 128 * 1024 * 1024


def _profile_root():
    from config import DATA_DIR

    return DATA_DIR / "browser_profiles"


def _acquire_profile_slot(source: str):
    """占一个 profile 槽位，返回 ``(目录, 锁文件对象)``；全被占用时返回 ``(None, None)``。

    锁必须持有到浏览器关闭为止，所以把文件对象一并交出去——提前关闭文件会
    释放 flock，另一个线程就能打开同一个 profile，Chromium 随即报锁冲突。
    """
    import fcntl

    root = _profile_root()
    for slot in range(_PROFILE_SLOTS):
        path = root / f"{source}-{slot}"
        try:
            path.mkdir(parents=True, exist_ok=True)
            handle = open(root / f"{source}-{slot}.lock", "w")
        except OSError as e:
            logger.warning("profile 槽位 %s 不可用: %s", path, e)
            continue
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()  # 已被别的线程/进程占着，试下一个
            continue
        return path, handle
    return None, None


# Chromium 用这三个文件做单实例互斥，内容是指向 ``<hostname>-<pid>`` 的符号链接。
_SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _clear_stale_singleton_locks(path) -> None:
    """清掉 profile 里残留的 Chromium 单实例锁。

    容器 force-recreate 时旧 Chromium 是被杀掉的，这三个锁留在了 bind mount
    里。新容器 hostname 变了、锁里记的 pid 也不存在，Chromium 判定该 profile
    正被别的实例占用，启动即退出，Playwright 报
    ``Target page, context or browser has been closed``。

    也就是说：**每次部署都会让持久化 profile 失效**，而且是静默降级——日志里
    只有一行「退回临时 profile」，流量悄悄涨回去。2026-08-05 上线当天就复现了。

    删掉是安全的：同一时刻只有一个实例在用这个目录，这一点已由我们自己的槽位
    flock 保证，Chromium 这层锁对我们是多余的。
    """
    for name in _SINGLETON_FILES:
        target = path / name
        try:
            # 用 lstat 而非 exists()：这些是符号链接，指向的路径通常已经不在了，
            # exists() 会跟随链接并返回 False，于是一个都删不掉。
            target.lstat()
        except OSError:
            continue
        try:
            target.unlink()
        except OSError as e:
            logger.warning("清理 %s 失败，持久化 profile 可能启动不了: %s", target, e)


def _pids_using_profile(path) -> list[int]:
    """还开着这个 profile 目录的 Chromium 进程。

    Playwright 的 ``BrowserContext`` 不暴露进程 pid，而我们需要的正是「``close()``
    之后进程到底还在不在」。靠 ``--user-data-dir=<我们这个目录>`` 反查——这个参数
    是我们自己传进去的，精确到目录，不会误伤别的浏览器。

    生产是 Linux，走 ``/proc``（零开销、不 fork）。没有 ``/proc`` 的平台（本地
    macOS 开发）退回 ``ps``——**不是为了 macOS 也要回收，是为了这段逻辑在本地也
    能被测到**。只在生产上生效的代码，等于只在生产上才发现写错了。
    """
    import os

    needle = f"--user-data-dir={path}"
    out: list[int] = []

    if os.path.isdir("/proc"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as fh:
                    cmd = fh.read().decode("utf8", "replace").replace("\0", " ")
            except OSError:
                continue          # 进程刚退出，正常
            if needle in cmd:
                out.append(int(entry))
        return out

    import subprocess

    try:
        ps = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                            text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    for line in ps.stdout.splitlines():
        line = line.strip()
        if needle not in line:
            continue
        pid, _, _ = line.partition(" ")
        if pid.isdigit():
            out.append(int(pid))
    return out


def _reap_profile(path) -> int:
    """确认这个 profile 上的 Chromium 真的没了；没死的强杀。返回杀掉几个。

    **为什么不能只调 ``close()`` 就当干净了**（2026-09-09 生产事故）：连接一旦断
    了（Chromium 被 OOM 杀、驱动崩了、代理把页面拖死），``close()`` 就是对着一根
    断掉的管子说话——它不报错，进程却还在。而 ``close()`` 紧接着会释放槽位 flock，
    于是下一次 ``_open_browser`` 拿到同一个目录，``_clear_stale_singleton_locks``
    把还活着那个实例的单实例锁删掉，两个 Chromium 同时开同一个 profile。

    实测后果：常驻浏览器每次「就绪」5–6 分钟就死，一次泄漏一整套（node 驱动 +
    整棵进程树）。18 小时泄漏 5 套，容器内存 1.14 → 1.73 GiB（上限 2 GiB），随后
    xior 渲染器卡死 600 秒、另一个浏览器型 source 过不了 CF 挑战并熔断。

    所以槽位锁释放之前必须先确认目录真的空了。
    """
    import os
    import signal
    import time as _t

    pids = _pids_using_profile(path)
    if not pids:
        return 0
    logger.warning(
        "close() 之后仍有 %d 个 Chromium 开着 profile %s，强制回收：%s",
        len(pids), getattr(path, "name", path), pids,
    )
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass          # 已经没了
        deadline = _t.monotonic() + (3.0 if sig is signal.SIGTERM else 1.0)
        while _t.monotonic() < deadline:
            pids = _pids_using_profile(path)
            if not pids:
                return len(_pids_using_profile(path)) or 1
            _t.sleep(0.1)
    left = _pids_using_profile(path)
    if left:
        # 杀不掉（僵尸 / 权限）。**槽位照样要放**——扣着它只会让所有人退回临时
        # profile，流量翻几倍，而目录冲突的风险由下一次的 singleton 清理兜。
        logger.error("profile %s 上仍有杀不掉的进程 %s，槽位照常释放",
                     getattr(path, "name", path), left)
    return 1


def _release_lock(handle) -> None:
    """放掉槽位锁。关闭文件即释放 flock，出错也不能往外抛——它挂在 close()
    路径上，抛出去会把浏览器的资源释放一起带停。"""
    if handle is None:
        return
    try:
        handle.close()
    except OSError:
        logger.debug("释放 profile 锁失败", exc_info=True)


def _redact_proxy(url: str) -> str:
    """代理 URL 里含用户名密码，日志里只留 host:port。"""
    return url.rsplit("@", 1)[-1] if "@" in url else url


@dataclass(frozen=True)
class ProbeRequest:
    """初始化阶段用来确认 clearance 已生效的最小请求。"""

    path: str
    method: str = "POST"
    body: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SiteProfile:
    """一个受 Cloudflare 保护的站点，需要浏览器传输层才能访问。

    把「过挑战 → 等 clearance → 发同源请求」这套流程里所有**站点相关**的
    部分收进来，其余逻辑（挑战判据、重试、超时）对所有站点通用。

    字段
    ----
    name            日志用的站点名
    source          对应的 source 名，用于取该 source 专属的代理 session
    challenge_url   过 CF 挑战时导航到的页面。它同时决定了同源请求的 origin，
                    所以必须和后续请求同域。
    default_headers 该站点请求的默认头
    clearance_probe 可选。有的话，初始化最后一步会用它确认 clearance 真的生效
                    （见 ``_wait_for_clearance``）。没有就跳过，交给首个真实
                    请求遇到 403 时重新导航。
    clearance_pending_markers
                    403 响应里出现这些标记 = clearance 还没生效（**瞬时**，
                    重新导航即可），而不是这个 IP 被封（换 IP 才有用）。
    maintenance_check
                    可选钩子，收到 (title, html) —— 仅在挑战解开后调用。
    rotating_proxy  True 时每次创建浏览器都换一个代理 session（即换出口 IP）。

                    **换 IP 的时机是「建浏览器」，不是「每请求」**，所以它
                    并不牺牲 clearance 复用：clearance 本来就只在单个浏览器
                    的生命周期内有效，而**新建浏览器无论如何都要重解一次挑战**
                    ——那一刻用旧 IP 还是新 IP，成本完全一样。

                    反过来，固定 IP 在这一刻省不下任何东西，却带来一个真实
                    代价：**IP 一旦被 CF 盯上就再也换不掉**（session id 是
                    source 名的哈希，恒定），403 之后 invalidate_session()
                    重建出来的还是同一个 IP，恢复路径形同虚设。

                    结论：除非有理由要长期钉住某个出口 IP，否则都该开。
    """

    name: str
    source: str
    challenge_url: str
    default_headers: Mapping[str, str] = field(default_factory=dict)
    clearance_probe: Optional[ProbeRequest] = None
    clearance_pending_markers: tuple[str, ...] = _CF_CHALLENGE_PENDING_MARKERS
    maintenance_check: Optional[Callable[[str, str], None]] = None
    rotating_proxy: bool = False


# Xior 的 AJAX 端点要 property/room id 才能拿到有意义的响应，profile 层拿不到，
# 所以不配 probe：初始化只等挑战解开，clearance 没生效时由首个真实请求触发
# 重新导航（fetch() 内处理）。
XIOR_PROFILE = SiteProfile(
    name="Xior",
    source="xior",
    challenge_url=_XIOR_MAIN_PAGE,
    default_headers=_XIOR_AJAX_HEADERS,
    # Xior 的 AJAX 端点按 IP 累积限流（~15–20 req/window）。固定出口 IP 上
    # 每轮 12 个请求，几轮之后必然 429——2026-08-02 实测，第 2 轮第一个请求
    # 就被拒。换浏览器就换 IP，把累积量摊开。
    rotating_proxy=True,
)


class BrowserFetcher:
    """
    管理 CloakBrowser 生命周期，在浏览器内发**同源**请求。

    浏览器内 fetch() 自带 cookies / TLS 指纹 / CF clearance token。

    **为什么不把 clearance 导出给 HTTP 客户端**：2026-08-05 实测导出的
    cf_clearance + UA + 同一出口 IP + curl_cffi Chrome 指纹确实能用，但票离开
    浏览器后只活 15–20 分钟；浏览器内的会话因为一直在发请求、cookie 被服务端
    持续刷新，能撑到 ``_BROWSER_MAX_AGE``。改用 HTTP 客户端只会让过挑战更频繁。
    挑战载荷只能靠**减少浏览器重建次数**来省（持久化 profile + 放宽重建周期）。

    站点差异全部收在 ``SiteProfile`` 里（见该类）；本类只负责通用流程：
    过挑战 → 等 clearance → 发请求 → 处理 clearance 过期。

    用法
    ----
    ::

        with BrowserFetcher(profile=XIOR_PROFILE) as f:
            resp = f.fetch_form("/wp-admin/admin-ajax.php", {"action": "..."})

    资源
    ----
    空闲 ~190MB，3 个 tab ~280MB。使用完后必须 close() 或通过上下文管理器释放。
    """

    def __init__(
        self,
        headless: bool = True,
        profile: "SiteProfile" = None,  # type: ignore[assignment]
    ):
        self._profile = profile if profile is not None else XIOR_PROFILE
        self._headless = headless
        self._browser = None
        self._page = None
        self._initialized = False
        self._effective_headless = headless
        self._proxy_url = ""
        self._blocked_count = 0
        self._profile_lock = None
        self._profile_path = None
        # 见 _install_byte_accounting
        self._wire_bytes = 0
        self._response_count = 0
        self._cached_count = 0
        self._cdp = None

    @property
    def profile(self) -> "SiteProfile":
        return self._profile

    @property
    def proxy_url(self) -> str:
        """本实例**实际在用**的代理 URL（未配置代理时为空串）。

        存在的理由和 ``_describe_navigation_failure`` 里探代理是同一个：任何
        「跟这个浏览器走同一条线路」的需求都必须读这个值，而**不能**重新调
        ``get_proxy_url()``——rotating 的 profile 上那样会拿到另一个 session，
        也就是另一个出口 IP。

        含用户名密码，写日志前过 ``_redact_proxy()``。
        """
        return self._proxy_url

    # ── 上下文管理器 ──────────────────────────────────────────────────
    def __enter__(self) -> "BrowserFetcher":
        self._launch()
        return self

    def _launch(self) -> None:
        import os


        if platform.system() == "Darwin" and self._effective_headless:
            logger.info("macOS 本地 CloakBrowser 使用 headed 调试模式，避免 headless SIGABRT")
            self._effective_headless = False

        # Docker/Linux 兼容参数：
        # - disable-dev-shm-usage: /dev/shm 默认 64MB，Chromium 会崩，改用 /tmp
        # - disable-gpu: headless 不需要 GPU 加速，避免无 GPU 环境报错
        #
        # macOS 本地 CloakBrowser headless v145 存在 SIGABRT 风险；这些 Linux
        # 参数也不该无条件注入到本地调试浏览器。
        chromium_args = []
        if platform.system() == "Linux":
            chromium_args = ["--disable-dev-shm-usage", "--disable-gpu"]

        # 代理必须**显式**传给 launch()，不能指望 Chromium 自己解析
        # HTTP_PROXY / HTTPS_PROXY 环境变量。
        #
        # 实测（2026-08-02，webshare sticky residential 端点）：
        #   仅环境变量   → 浏览器 79.116.229.115 / curl_cffi 213.73.166.139
        #   显式 proxy=  → 两者都是 213.10.194.180（连续两轮复现）
        # 环境变量那条路拿到的出口甚至不在 sticky 端点指定的国家，说明
        # Chromium 走的是另一套代理解析。
        #
        # 后果不止是「不一致」：Cloudflare 的 clearance 绑 IP，浏览器与
        # 其它 curl_cffi 抓取器落在不同出口，就无法共享会话；而且浏览器
        # 会完全绕过 get_proxy_url() 的故障切换与冷却逻辑。
        #
        # 配置来源仍然只有 .env 一处——get_proxy_url() 读的就是那几个环境
        # 变量，这里只是把它已经解析好的值显式交给浏览器。
        from config import get_proxy_url  # 延迟导入，避免循环依赖

        proxy_url = get_proxy_url(
            self._profile.source, rotating=self._profile.rotating_proxy
        ) or None
        if proxy_url:
            logger.info(
                "%s 浏览器走代理 %s",
                self._profile.name, _redact_proxy(proxy_url),
            )
        # 记下来给 _describe_navigation_failure 用：诊断必须探**这个浏览器实际
        # 在用**的那条代理线路。重新调 get_proxy_url() 在 rotating 的 profile 上
        # 会拿到另一个 session，探到的是别的出口 IP，结论无效。
        self._proxy_url = proxy_url or ""

        # 降级直连时必须**显式关掉** Chromium 的代理解析。
        #
        # 上面那段注释讲的是「显式传 proxy 才能保证出口一致」，但反向不成立：
        # 传 proxy=None **关不掉**它——Chromium 会自己去读 HTTP_PROXY /
        # HTTPS_PROXY，继续拨那个刚被判定为失效的代理。
        #
        # 2026-08-26 生产实测（webshare 两个账号同时 402）：五次「降级直连
        # 原生 IP」全部在 3 秒内报 ERR_TUNNEL_CONNECTION_FAILED，而代码自己
        # 打出的诊断是「代理层报错，但本次并未走代理」——那句矛盾正是这个
        # bug 的指纹。加上 --no-proxy-server 之后，浏览器才真的连到 CF
        # （失败时间从 3 秒变成 275 秒，因为它终于开始真的解挑战了）。
        # 只在**环境里确实有代理会漏进来**时才加：没配过代理的自部署用户
        # 本来就没有 env 可漏，给他们平白加个 flag 只会让排查时多一个变量。
        if not proxy_url and any(
            os.environ.get(k) for k in
            ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
             "http_proxy", "https_proxy", "all_proxy")
        ):
            logger.info(
                "%s 降级直连：环境里还有代理变量，显式加 --no-proxy-server 挡住 "
                "Chromium 自己的代理解析", self._profile.name,
            )
            chromium_args = chromium_args + ["--no-proxy-server"]

        self._browser, self._page = self._open_browser(chromium_args, proxy_url)
        # 给页面一把默认超时。**不设的话 Playwright 的默认是 30 秒**，看起来够用，
        # 但它只覆盖导航与等待类操作——``page.content()`` 受它管，``page.evaluate``
        # 不受（后者等的是 JS promise，Playwright 的 API 里根本没有 timeout 参数）。
        #
        # 所以这一行只是把「受管的那部分」收紧到一个我们自己定的数；evaluate 的
        # 兜底在 monitor 侧的墙钟超时（_SOURCE_DISPATCH_TIMEOUT_SEC）。两层都要，
        # 少任何一层都会让一个 wedged 页面挂住整个 source。
        try:
            self._page.set_default_timeout(_PAGE_DEFAULT_TIMEOUT_MS)
        except Exception:
            logger.debug("设置页面默认超时失败，沿用 Playwright 默认", exc_info=True)
        self._blocked_count = 0
        self._wire_bytes = 0
        self._response_count = 0
        self._cached_count = 0
        self._cdp = None
        self._install_resource_blocking()
        self._install_byte_accounting()

    def _open_browser(self, chromium_args: list[str], proxy_url: str | None):
        """开浏览器，优先复用磁盘上的 profile；拿不到就退回临时 profile。

        返回 ``(browser_or_context, page)``。两种返回物的 ``close()`` 与
        ``new_page()`` 语义一致，本类其余部分不必区分。
        """
        import os

        from cloakbrowser import launch, launch_persistent_context

        if os.environ.get("BROWSER_PERSIST_PROFILE", "1").strip() not in ("0", "false", "no"):
            path, lock = _acquire_profile_slot(self._profile.source)
            if path is None:
                logger.info(
                    "%s 的 profile 槽位已被占满（共 %d 个），本次用临时 profile",
                    self._profile.source, _PROFILE_SLOTS,
                )
            else:
                # 必须在 launch 之前：Chromium 一见到别人的锁就直接退出，
                # 事后再清也救不回这一次。
                _clear_stale_singleton_locks(path)
                try:
                    ctx = launch_persistent_context(
                        str(path),
                        headless=self._effective_headless,
                        humanize=True,
                        args=chromium_args + [f"--disk-cache-size={_DISK_CACHE_SIZE}"],
                        proxy=proxy_url,
                    )
                except Exception as e:
                    # profile 损坏、锁冲突、磁盘满……都不该让抓取停摆
                    logger.warning(
                        "%s 持久化 profile 启动失败，退回临时 profile: %s",
                        self._profile.name, e,
                    )
                    _release_lock(lock)
                else:
                    self._profile_lock = lock
                    self._profile_path = path
                    # cookie 一律不留。clearance 绑出口 IP，而 rotating_proxy
                    # 意味着下次开浏览器多半换了 IP——带着上一个 IP 的
                    # cf_clearance 去请求，CF 只会当作可疑并重新挑战。
                    # 要复用的只是磁盘缓存里那些静态资源。
                    try:
                        ctx.clear_cookies()
                    except Exception:
                        logger.debug("清 cookie 失败，继续", exc_info=True)
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    logger.info("%s 复用 profile %s", self._profile.name, path.name)
                    return ctx, page

        browser = launch(
            headless=self._effective_headless,
            humanize=True,
            args=chromium_args,
            proxy=proxy_url,
        )
        return browser, browser.new_page()

    def _install_resource_blocking(self) -> None:
        """拦掉与房源数据无关的子请求，省代理流量。见 ``_should_block``。

        ``BROWSER_BLOCK_RESOURCES=0`` 可整体关闭：拦截会改变页面的加载行为，
        万一 CF 因此起疑，需要一条不重新发版就能退回原状的路。
        """
        import os

        if os.environ.get("BROWSER_BLOCK_RESOURCES", "1").strip() in ("0", "false", "no"):
            logger.info("%s 资源拦截已关闭（BROWSER_BLOCK_RESOURCES）", self._profile.name)
            return

        def _handler(route):
            # 任何一步出错都必须放行并吞掉异常：route 处理器抛出去会让这个
            # 请求悬着直到超时，为省流量把页面拖垮不划算。
            try:
                request = route.request
                if _should_block(request.url, request.resource_type):
                    self._blocked_count += 1
                    route.abort()
                    return
            except Exception:
                logger.debug("资源拦截判定异常，放行", exc_info=True)
            try:
                route.continue_()
            except Exception:
                logger.debug("route.continue_ 失败", exc_info=True)

        try:
            self._page.route("**/*", _handler)
        except Exception as e:
            # 拦截是省钱的优化，不是抓取的前提，装不上就照常跑
            logger.warning("%s 资源拦截未能装载，按原样加载页面: %s", self._profile.name, e)

    def _install_byte_accounting(self) -> None:
        """统计本次会话真实走了多少字节。``BROWSER_BYTE_ACCOUNTING=0`` 关闭。

        为什么必须是 CDP 而不是别的
        --------------------------
        代理按流量计费，而在这之前**没有任何地方知道浏览器到底下了多少字节**：

        - ``route.abort()`` 在下载之前就掐断了，被拦掉的请求根本没有大小；
        - ``response.body()`` 要把整个 body 拉进内存，为了称重去下载它，
          省流量的事就白做了；
        - ``Content-Length`` 在 chunked 响应上直接缺席。

        ``Network.loadingFinished.encodedDataLength`` 是 Chromium 自己记的
        **线上字节数**，含响应头、按压缩后计算，不额外产生任何请求。

        顺带记 ``fromDiskCache``：持久化 profile 声称复用磁盘缓存来省流量，
        但每次会话都换出口 IP 又清了 cookie，缓存到底命中没有一直没人验证过。
        命中的响应 encodedDataLength 记 0，两个数放一起就能看出来。

        全程 try/except：这是计量，不是抓取的前提，任何一步失败都只降级成
        「这次没数」，不能影响页面加载。
        """
        import os

        if os.environ.get("BROWSER_BYTE_ACCOUNTING", "1").strip() in ("0", "false", "no"):
            return

        try:
            cdp = self._page.context.new_cdp_session(self._page)
        except Exception as e:
            # 非 Chromium 内核、或 CDP 不可用 → 没有计量，照常抓
            logger.debug("%s 字节计量未能装载: %s", self._profile.name, e)
            return

        def _finished(params):
            try:
                self._wire_bytes += int(params.get("encodedDataLength") or 0)
            except Exception:
                pass

        def _response(params):
            try:
                self._response_count += 1
                if (params.get("response") or {}).get("fromDiskCache"):
                    self._cached_count += 1
            except Exception:
                pass

        try:
            cdp.send("Network.enable")
            cdp.on("Network.loadingFinished", _finished)
            cdp.on("Network.responseReceived", _response)
        except Exception as e:
            logger.debug("%s 字节计量启用失败: %s", self._profile.name, e)
            return
        self._cdp = cdp

    def _rebuild_browser(self) -> bool:
        """推倒浏览器重建，换一个出口 IP。换成了返回 True。

        403 和挑战超时都是「当前这个出口 IP 过不去」。同一个浏览器再导航一次
        用的还是同一个 IP，重试等于把同一次失败重复三遍——2026-08-03 生产事故
        就是这么熔断的。

        只有 ``rotating_proxy`` 的 profile 重建才会落到新 session；固定 IP 的
        profile 重建后拿到的是同一个出口，白付一次冷启动，所以不做。
        """
        # 两处拒绝都要**吵一声**。调用方（``ensure_initialized`` / ``fetch``）都是
        # ``self._rebuild_browser()`` 一行带过、忽略返回值，所以静默返回 False 会让
        # 「重试 3 次，每次换一个出口 IP」悄悄退化成「在同一个 IP 上原样重试 3 次」
        # ——日志上完全看不出区别，而那正是 2026-08-03 那次熔断的成因描述。
        if not self._profile.rotating_proxy:
            logger.warning(
                "%s 请求重建浏览器换 IP，但该 profile 不轮换出口（rotating_proxy=False），"
                "本次不重建。调用方的重试将落在同一个出口 IP 上。",
                self._profile.name,
            )
            return False
        if self._browser is None:
            # 还没 __enter__ 就走到这里，说明调用方绕过了正常生命周期。
            # 凭空 launch 一个浏览器不是「重建」，交回给调用方处理。
            logger.warning(
                "%s 请求重建浏览器，但当前还没有浏览器实例（调用方绕过了 __enter__），"
                "本次不重建。",
                self._profile.name,
            )
            return False
        old = _redact_proxy(self._proxy_url) if self._proxy_url else "直连"
        self.close()
        self._launch()
        new = _redact_proxy(self._proxy_url) if self._proxy_url else "直连"
        logger.info(
            "%s 重建浏览器以更换出口 IP：%s → %s", self._profile.name, old, new
        )
        return True

    def __exit__(self, *args):
        self.close()

    def close(self):
        """关闭浏览器，释放资源。"""
        if self._browser is not None:
            if self._blocked_count or self._wire_bytes:
                # 拦了多少、下了多少写进日志：改了拦截规则之后，这是唯一能看出
                # 它还在生效、以及生效到什么程度的地方。两个数必须在同一行——
                # 拦截数单独看只能说明「拦到了」，说明不了省下多少钱。
                logger.info(
                    "%s 本次会话拦截 %d 个子请求，实际下行 %.2f MB（%d 个响应，%d 个命中磁盘缓存）",
                    self._profile.name, self._blocked_count,
                    self._wire_bytes / 1e6, self._response_count, self._cached_count,
                )
            try:
                if self._cdp is not None:
                    self._cdp.detach()
            except Exception:
                pass
            self._cdp = None
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._page = None
            self._initialized = False
        # 锁必须在浏览器**真的**关掉之后才放：提前释放会让另一个线程拿到同一个
        # profile，而 Chromium 还没退出。
        #
        # 上面那句 close() 外面包着 `except: pass`——连接断了的时候它既不报错也
        # 不生效，进程照旧活着。只信它的后果见 _reap_profile 的 docstring
        # （2026-09-09：泄漏 5 套浏览器，拖垮两个浏览器型 source）。所以这里先核实。
        if self._profile_path is not None:
            try:
                _reap_profile(self._profile_path)
            except Exception:
                # 回收失败不能把 close() 带停：锁不放的话所有人都退回临时 profile。
                logger.warning("回收 profile %s 失败", self._profile_path, exc_info=True)
        _release_lock(self._profile_lock)
        self._profile_lock = None
        self._profile_path = None

    # ── CF 挑战初始化 ─────────────────────────────────────────────────
    def ensure_initialized(self) -> None:
        """
        懒加载：首次请求前访问主页完成 CF Turnstile 挑战。

        公开方法——调用方可以提前调用来热身，也可以在 fetch 首次调用时自动触发。
        """
        if self._initialized:
            return

        start = time.monotonic()
        last_error: Exception | None = None

        # 每次尝试 = 一次完整导航 + 短暂等 cookie 落地。等不到就**重新导航**，
        # 而不是继续对 API 轮询：token 由导航签发，轮询换不出来，只会朝一个
        # 拿不到 clearance 的会话打几十个必然 403 的请求，反过来加重 CF 的
        # 怀疑。（同理见 fetch 里 clearance 过期的处理。）
        for attempt in range(1, _INIT_ATTEMPTS + 1):
            try:
                challenge_elapsed, clearance_elapsed = self._navigate_and_verify(
                    attempt
                )
            except (_exc("BlockedError"), _exc("ScrapeNetworkError")) as e:
                # **两类都要重试，判据是「换个出口 IP 有没有可能解决」。**
                #
                # BlockedError      挑战没解开 / clearance 不生效 —— 这个出口 IP
                #                   被盯上了，换一个再试
                # ScrapeNetworkError goto() 本身失败 —— 单个出口 IP 不通、或者
                #                   挑战页压根没加载完
                #
                # 这里以前**只接 BlockedError**，于是 goto 失败直接穿透，零重试
                # 零换 IP。线上计数（保留日志全量）：
                #
                #     初始化重试（BlockedError 路径）        164 次
                #     主站加载失败（穿透，零重试）            558 次   ← 3.4 倍
                #       其中消息写着「（CF 挑战可能未通过）」   439 次
                #
                # 也就是说，为挑战失败准备的这套恢复逻辑被挑战失败绕过了 439 次。
                # goto 超时和 _wait_for_challenge_clear 超时是同一个根因，只是
                # 检测点差一步，却走了完全相反的两条路。
                #
                # 唯一不重试的是代理服务端级故障：换 session 也是同一个坏账户。
                from scrapers.base import is_proxy_service_error  # 延迟导入避免循环

                if is_proxy_service_error(e):
                    # 402 配额耗尽 / 407 认证失败 / 502 / 503 —— 账户级或代理
                    # 服务自身故障。2026-08-05 欠费停服 5 小时期间明确回
                    # response 402，重试三次只是白等，还延后了 monitor 那边的
                    # 「标记故障 → 切备用 → 降级直连」。
                    logger.error(
                        "%s 初始化遇到代理服务端故障，不重试，直接上抛：%s",
                        self._profile.name, e,
                    )
                    raise
                last_error = e
                logger.warning(
                    "初始化第 %d/%d 次未通过（%s）：%s",
                    attempt, _INIT_ATTEMPTS, type(e).__name__, e,
                )
                if attempt < _INIT_ATTEMPTS:
                    self._rebuild_browser()
                continue

            # 两段耗时按**成功的那次导航**分开记（不含前面失败尝试的耗时，
            # 否则失败的等待会被算进 clearance）：挑战慢通常是机器/网络慢，
            # clearance 慢更像 CF 在加码校验，排查时是两个方向。
            logger.info(
                "CF 挑战完成，clearance 已生效 "
                "(第 %d 次导航：挑战 %.1fs + clearance %.1fs；累计 %.1fs)",
                attempt, challenge_elapsed, clearance_elapsed,
                time.monotonic() - start,
            )
            self._initialized = True
            return

        raise last_error  # type: ignore[misc]  # 循环至少跑一次，必非 None

    def _navigate_and_verify(self, attempt: int) -> tuple[float, float]:
        """导航主站 → 等挑战解开 → 查维护 → 等 clearance。

        返回 ``(挑战耗时, clearance 耗时)``，单位秒，只计本次导航。

        抛什么，取决于在哪一步没过 —— **不是清一色 BlockedError**
        ----------------------------------------------------------
        ``goto()`` 失败          → ``ScrapeNetworkError``（消息由
                                  ``_describe_navigation_failure`` 拼，可能是
                                  代理层原因，也可能只是「CF 挑战可能未通过」
                                  这个排除法猜测）
        挑战没解开 / clearance   → ``BlockedError``
        主站在维护               → ``UpstreamMaintenanceError``

        前两类都由 ``ensure_initialized`` 重试并换出口 IP；维护不在重试之列
        （重试多少次都一样）。

        这段 docstring 以前写的是「任一环节没过就抛 ``BlockedError``」——**那是
        错的**，而且正是它让「goto 失败绕过整套重试逻辑」这个 bug 藏了很久：
        读代码的人按文档假设了行为，没去核对 ``except`` 接的是哪几个类。
        """
        logger.info(
            "CloakBrowser 加载 %s 主站完成 CF 挑战...（第 %d 次）",
            self._profile.name, attempt,
        )
        start = time.monotonic()
        try:
            self._page.goto(
                self._profile.challenge_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except Exception as e:
            detail = self._describe_navigation_failure(e)
            logger.error("%s 主站加载失败: %s", self._profile.name, detail)
            raise _exc("ScrapeNetworkError")(
                f"{self._profile.name} 主站加载失败{detail}"
            ) from e

        # goto(domcontentloaded) 返回时页面通常还是 CF 挑战页，必须先等挑战
        # 真正解开，再做任何基于页面内容的判定——否则读到的标题和正文是
        # Cloudflare 的，不是站点的。
        self._wait_for_challenge_clear()
        challenge_elapsed = time.monotonic() - start

        # 挑战已解开，此刻的标题/正文才代表站点真实状态
        self._raise_if_maintenance_page()

        # 最后一关：确认 clearance 真的生效。等页面元素出现不算数——元素和
        # 能否发请求无关；配了 clearance_probe 的 profile 直接探 API，能拿到
        # 响应才算初始化完成。
        clearance_start = time.monotonic()
        self._wait_for_clearance()
        return challenge_elapsed, time.monotonic() - clearance_start

    def _describe_navigation_failure(self, error: Exception) -> str:
        """把 goto() 的异常翻译成一句说得清原因的话。

        Chromium 在代理层失败时只给 ``ERR_TUNNEL_CONNECTION_FAILED`` 这类码，
        看不出是配额耗尽、认证失败还是代理宕机；而默认文案「CF 挑战可能未通过」
        会把排查引向 Cloudflare。命中代理错误码时就直接问代理要真实状态码。

        返回值拼在「主站加载失败」后面，自带括号。
        """
        text = str(error)
        if not any(marker in text for marker in _PROXY_ERROR_MARKERS):
            return f"（CF 挑战可能未通过）: {error}"
        if not self._proxy_url:
            return f"（代理层报错，但本次并未走代理）: {error}"

        from config import probe_proxy  # 延迟导入，避免循环依赖
        from urllib.parse import urlparse

        target = urlparse(self._profile.challenge_url)
        try:
            reason = probe_proxy(
                self._proxy_url,
                target.hostname or "",
                target.port or 443,
            )
        except Exception as e:  # 诊断本身不能盖掉原始错误
            logger.debug("代理探测异常: %s", e)
            return f"（代理层报错，探测代理时又失败了: {e}）: {error}"

        if reason:
            return f"（{reason}）: {error}"
        return f"（代理本身可用，问题在目标站点或链路上）: {error}"

    def _is_challenge_page(self) -> bool:
        """当前页面是否仍是 CF 挑战页。"""
        try:
            return _CF_CHALLENGE_HTML_MARKER in self._page.content()
        except Exception:
            # 页面内容取不到（导航中 / 崩溃）时保守认为仍在挑战，
            # 由 _wait_for_challenge_clear 的超时统一处理。
            return True

    def _wait_for_challenge_clear(
        self, timeout: float = _CHALLENGE_CLEAR_TIMEOUT
    ) -> None:
        """轮询到 CF 挑战解开为止；超时抛 BlockedError。

        这是「挑战是否通过」的唯一判据。以前用 ``[data-cy=FilterList-item]``
        选择器代替，超时后仅告警并继续，导致挑战没过也照发请求——
        必然 403，再触发会话重建，最终把 source 打进熔断。
        """
        deadline = time.monotonic() + timeout
        while True:
            if not self._is_challenge_page():
                return
            if time.monotonic() >= deadline:
                raise _exc("BlockedError")(
                    f"CF 挑战 {timeout:.0f}s 内未解开，{self._profile.name} "
                    "主站仍停在挑战页。可能需要更换 IP 或等待冷却。"
                )
            time.sleep(_CHALLENGE_POLL_INTERVAL)

    def _raise_if_maintenance_page(self) -> None:
        """挑战解开后，让 profile 的钩子判断站点是否处于维护态。

        必须在 CF 挑战解开后调用：挑战页是 Cloudflare 生成的，其标题和正文
        与站点的真实状态无关，在挑战解开前判定等于拿 CF 的页面去猜站点在
        不在维护。没有配钩子的站点直接跳过。
        """
        check = self._profile.maintenance_check
        if check is None or self._is_challenge_page():
            return

        try:
            title = (self._page.title() or "").strip()
        except Exception:
            title = ""
        try:
            html = self._page.content()[:4000]
        except Exception:
            html = ""

        check(title, html)

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def is_alive(self) -> bool:
        """浏览器进程和页面是否还在。

        为什么需要它 —— 「对象还在」不等于「浏览器还在」
        ------------------------------------------------
        Chromium 崩掉 / 被 OOM killer 干掉之后，``self._browser`` 仍然是个对象，
        ``close()`` 也从没被调用过。而调用方判断「要不要复用」看的是「非 None
        且没超龄」，于是死掉的实例会被一直复用到 ``_BROWSER_MAX_AGE`` 才自愈。

        中间那些轮次不会自己恢复：崩溃在 ``page.evaluate`` 处抛出，被 scraper
        改判成 ``ScrapeNetworkError``，而 dispatcher 的网络分支**刻意不丢会话**（丢的话每次代理抖动都要重过一整轮 CF 挑战）。

        实现上只读**本地标志位**，不发任何 IPC：``is_closed()`` / ``is_connected()``
        由连接层维护，进程没了会被置位。所以这个检查几乎零成本，可以每次复用前跑。
        """
        if self._browser is None or self._page is None:
            return False
        try:
            if self._page.is_closed():
                return False
        except Exception:
            # 连状态都问不出来，按死了算——保守方向是重建，代价只是一次冷启动
            return False

        # launch() 给的是 Browser（有 is_connected）；
        # launch_persistent_context() 给的是 BrowserContext（没有，要从 .browser 问）。
        # 本类其余部分都不区分这两种返回物，这里也不该例外。
        for obj in (self._browser, getattr(self._browser, "browser", None)):
            probe = getattr(obj, "is_connected", None)
            if probe is None:
                continue
            try:
                return bool(probe())
            except Exception:
                return False
        return True

    # ── 通用同源请求 ────────────────────────────────────────────────────
    def _raw_fetch(
        self,
        path: str,
        *,
        method: str = "POST",
        body: str = "",
        headers: Mapping[str, str] | None = None,
        timeout_ms: int = 30_000,
    ) -> dict:
        """在页面里发一次同源请求，原样返回 ``{status, ok, text, headers}``。

        不做任何状态码处理，也**不会**触发 ``ensure_initialized``——
        clearance 探测要在初始化过程中调用它，走公开方法会无限递归。
        """
        merged = dict(self._profile.default_headers)
        if headers:
            merged.update(headers)

        headers_json = _json_dumps(merged)
        body_json = _json_dumps(body)
        path_json = _json_dumps(path)
        method_json = _json_dumps(method.upper())
        # GET / HEAD 不能带 body，带了浏览器直接抛 TypeError
        send_body = method.upper() not in ("GET", "HEAD")

        js_code = f"""
            async () => {{
                const controller = new AbortController();
                const timeout = setTimeout(() => controller.abort(), {timeout_ms});
                try {{
                    const init = {{
                        method: {method_json},
                        credentials: 'include',
                        mode: 'same-origin',
                        cache: 'no-store',
                        redirect: 'follow',
                        referrer: window.location.href,
                        referrerPolicy: 'strict-origin-when-cross-origin',
                        headers: {headers_json},
                        signal: controller.signal,
                    }};
                    if ({'true' if send_body else 'false'}) {{
                        init.body = {body_json};
                    }}
                    const resp = await fetch({path_json}, init);
                    const text = await resp.text();
                    const headers = {{}};
                    for (const [key, value] of resp.headers.entries()) {{
                        const lower = key.toLowerCase();
                        if (['cf-ray', 'content-type', 'server', 'vary'].includes(lower)) {{
                            headers[lower] = value;
                        }}
                    }}
                    return {{ status: resp.status, ok: resp.ok, text: text, headers: headers }};
                }} catch (err) {{
                    return {{ error: err.message || String(err) }};
                }} finally {{
                    clearTimeout(timeout);
                }}
            }}
        """
        return self._page.evaluate(js_code)

    def _is_clearance_required(self, result: dict) -> bool:
        """403 是否属于「clearance 还没落地」这种**瞬时**状态。

        两者都是 403，但含义相反：clearance 未生效重新导航就好，IP 被封则
        换 IP 才有用。混为一谈会把马上要生效的会话反复推倒重建。

        判据由 profile 给（``clearance_pending_markers``）；默认是 CF 挑战页的
        特征（``_cf_chl_opt`` / ``Just a moment``）。
        """
        if result.get("status") != 403:
            return False
        text = (result.get("text") or "").lower()
        return any(
            marker.lower() in text
            for marker in self._profile.clearance_pending_markers
        )

    def _wait_for_clearance(self, timeout: float = _CLEARANCE_TIMEOUT) -> None:
        """轮询到 profile 的 clearance 探针不再回「待校验」为止。

        挑战页消失只说明文档被真实页面替换了，``cf_clearance`` cookie 未必
        已经生效——实测两者之间有约 2s 的空窗，这期间发请求必然 403。
        真正的「初始化完成」是这个探测通过。
        """
        probe = self._profile.clearance_probe
        if probe is None:
            # 没配探针的站点跳过：由首个真实请求遇到 403 时重新导航兜底
            return

        deadline = time.monotonic() + timeout
        while True:
            try:
                result = self._raw_fetch(
                    probe.path,
                    method=probe.method,
                    body=probe.body,
                    headers=probe.headers,
                    timeout_ms=15_000,
                )
            except Exception as e:
                # 页面还在动（导航 / 重绘）时 evaluate 可能直接抛，等下一轮
                logger.debug("clearance 探测异常，重试: %s", e)
                result = {"error": str(e)}

            if "error" not in result and not self._is_clearance_required(result):
                return
            if time.monotonic() >= deadline:
                raise _exc("BlockedError")(
                    f"CF clearance {timeout:.0f}s 内未生效，{self._profile.name} "
                    "持续要求浏览器校验。可能需要更换 IP 或等待冷却。"
                )
            time.sleep(_CLEARANCE_POLL_INTERVAL)

    def fetch_form(
        self,
        path: str,
        data: Mapping[str, str],
        *,
        timeout_ms: int = 30_000,
        headers: Mapping[str, str] | None = None,
    ) -> dict:
        """发一次 ``application/x-www-form-urlencoded`` POST，返回解析后的 JSON。

        给 WordPress admin-ajax 这类表单端点用（Xior）。
        """
        from urllib.parse import urlencode

        result = self.fetch(
            path,
            method="POST",
            body=urlencode(dict(data)),
            headers=headers,
            timeout_ms=timeout_ms,
        )

        import json

        try:
            return json.loads(result["text"])
        except json.JSONDecodeError as e:
            raise _exc("ScrapeNetworkError")(
                f"{self._profile.name} 响应非 JSON: {e}"
            ) from e

    def fetch_plain(
        self,
        path: str,
        *,
        method: str = "GET",
        body: str = "",
        headers: Mapping[str, str] | None = None,
        timeout_ms: int = 30_000,
    ) -> dict:
        """发一次同源请求，原样返回 ``{status, ok, text, headers}``，不处理状态码。

        与 ``fetch()`` 的区别是刻意的：**不做 403/CF 重建**——登录失败回 401、需要重新校验回 403，都是业务
          语义的响应，换 IP / 重建浏览器解决不了。状态码原样交回调用方判断。

        仍然会 ``ensure_initialized``：端点同样在 Cloudflare 后面，clearance
        cookie 得先就位。
        """
        self.ensure_initialized()
        result = self._raw_fetch(
            path, method=method, body=body, headers=headers,
            timeout_ms=timeout_ms,
        )
        if "error" in result:
            raise _exc("ScrapeNetworkError")(
                f"{self._profile.name} 明文请求失败 {path}: {result['error']}"
            )
        return result

    def fetch(
        self,
        path: str,
        *,
        method: str = "POST",
        body: str = "",
        headers: Mapping[str, str] | None = None,
        timeout_ms: int = 30_000,
    ) -> dict:
        """发一次同源请求并处理 CF 相关的失败，返回 ``{status, ok, text, headers}``。

        这是所有站点共用的请求入口：``fetch_form`` 只是在它外面套一层响应解析。

        Raises
        ------
        BlockedError              403 且重建会话后仍被挡
        RateLimitError            429
        ScrapeNetworkError        网络/超时错误
        UpstreamMaintenanceError  重建过程中发现站点在维护
        """
        self.ensure_initialized()
        result = self._raw_fetch(
            path, method=method, body=body, headers=headers, timeout_ms=timeout_ms
        )
        if "error" in result:
            raise _exc("ScrapeNetworkError")(f"浏览器内 fetch 失败: {result['error']}")

        status = result["status"]

        # clearance 过期是长会话里的常态（token 有寿命，且实测生产环境的
        # token 比本地短得多），不是被封。
        #
        # 恢复方式只有一种：重新导航主站把挑战跑完——token 是页面通过挑战时
        # 下发的，对着 API 轮询永远换不出新 token，只会白等到超时再误判成
        # 屏蔽。ensure_initialized() 里的 _wait_for_clearance 之所以有效，
        # 是因为那里刚做完 goto，等的是 cookie 落地而不是 token 重签。
        if self._is_clearance_required(result):
            logger.info(
                "%s 要求重新校验，重新走主站挑战流程...", self._profile.name
            )
            self._initialized = False
            self.ensure_initialized()
            result = self._raw_fetch(
                path, method=method, body=body, headers=headers, timeout_ms=timeout_ms
            )
            if "error" in result:
                raise _exc("ScrapeNetworkError")(
                    f"clearance 恢复后重试失败: {result['error']}"
                )
            status = result["status"]

        if status == 403:
            logger.warning(
                "%s 返回 403，尝试重建 CF 会话... headers=%s body=%s",
                self._profile.name,
                result.get("headers", {}),
                result.get("text", "")[:300],
            )
            # 走到这里说明不是 clearance 过期（那条路在上面已经处理并重试过），
            # 是这个出口 IP 被挡了。原地重跑挑战换不掉 IP，先换浏览器。
            self._initialized = False
            self._rebuild_browser()
            try:
                self.ensure_initialized()
            except _exc("UpstreamMaintenanceError"):
                # 重建时发现平台在维护：这是维护，不是屏蔽。压成 BlockedError
                # 会让 monitor 走熔断 + admin 告警，而不是安静的维护冷却。
                raise
            except Exception as e:
                raise _exc("BlockedError")(
                    f"{self._profile.name} 返回 403，CF 会话重建失败。"
                    "可能需要更换 IP 或等待冷却。"
                ) from e
            retry = self._raw_fetch(
                path, method=method, body=body, headers=headers, timeout_ms=timeout_ms
            )
            if "error" in retry:
                raise _exc("ScrapeNetworkError")(f"重建后重试失败: {retry['error']}")
            if retry["status"] == 403:
                logger.warning(
                    "%s 重建会话后仍返回 403 headers=%s body=%s",
                    self._profile.name,
                    retry.get("headers", {}),
                    retry.get("text", "")[:300],
                )
                raise _exc("BlockedError")(
                    f"{self._profile.name} 持续返回 403。可能需要更换 IP 或等待冷却。"
                )
            result = retry
            status = result["status"]

        if status == 429:
            raise _exc("RateLimitError")(
                f"{self._profile.name} 返回 429 Too Many Requests"
            )

        if status == 404:
            # 404 = 这个路径在上游不存在了，几乎只有一个成因：端点被迁走。
            #
            # 单独拎出来是因为它此前落进下面那条通用分支，报成
            # 「抓取网络失败 … 请检查代理/网络」——2026-08-11 上游迁走端点时，
            # 日志连刷三天「请检查代理/网络」，而代理一直是好的。诊断被引向了完全错误的
            # 方向，是那次静默三天的直接原因之一。
            #
            # 仍然抛 ScrapeNetworkError：调用方的隔离与重试语义不该变，改的
            # 只是这句话指向哪儿。
            raise _exc("ScrapeNetworkError")(
                f"{self._profile.name} HTTP 404 —— 端点 {path} 不存在，"
                f"上游很可能改了 API 路径（不是代理或网络问题，别往那个方向查）。"
                f"响应: {result['text'][:200]}"
            )

        if not result["ok"] and status >= 400:
            raise _exc("ScrapeNetworkError")(
                f"{self._profile.name} HTTP {status}: {result['text'][:300]}"
            )

        return result
