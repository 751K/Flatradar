"""
scrapers/codomo.py — Codomo（原 Holland2Stay）抓取器
====================================================

2026-11 起 Holland2Stay 改名 Codomo：``www.holland2stay.com`` 全站 301 到
``https://codomo.eu``，旧的 GraphQL 端点、加密信封、operation 白名单、
CloakBrowser 过 Cloudflare 那一整套随之失效。

新站是 Next.js 服务端渲染，**纯 HTTP 就能拿到完整 HTML**，不需要浏览器、
不需要加密。房源只在全国列表页里：

    https://codomo.eu/homes/nl/en?page=N      每页 25 条，按「最早可入住」排序

城市子页（``/homes/nl/en/eindhoven``）只是楼盘介绍，不含房源卡片，所以与
Magis / Plaza 同一形态：**每批次抓一遍全国列表**，各城市 task 从同一份结果里
按城市切分。全国 ~150 条 ≈ 6 个请求。

source key 随改名改为 ``codomo``；库里的 ``holland2stay`` 历史数据由
mstorage 的一次性迁移改写（见 mstorage/_codomo_rename.py）。房源 id 仍是 URL
最后一段 slug（``g-h-betzweg-207``），与旧站 ``url_key`` 同形，新旧房源能接上。

状态映射
--------
列表里只有两类房源（页面筛选项 ``publish_mode``）：

    Book directly   → "Available to book"
    Show interest   → "Available in lottery"   登记意向、按人数排队，即旧站抽签

Reserved / Occupied 不再出现在列表里，因此这个 feed 不覆盖「已预留」，状态收敛
按「只列可订」的平台处理（与 Xior / Plaza 相同）。

解析细节见 scrapers/codomo_parse.py。
"""
from __future__ import annotations

import logging
import math
from urllib.parse import urlsplit
import time
from contextlib import contextmanager
from typing import Optional

from mcore.registry import KNOWN_CODOMO_CITIES
from models import Listing

from .base import (
    RATE_LIMIT_BACKOFF,
    AbstractScraper,
    BlockedError,
    RateLimitError,
    ScrapeNetworkError,
    ScrapeResult,
    ScrapeTask,
    UpstreamMaintenanceError,
    is_cloudflare_body,
    is_maintenance_body,
)
from .codomo_parse import CodomoCard, parse_listing_page

logger = logging.getLogger(__name__)

BASE_URL = "https://codomo.eu"
_HOST = "codomo.eu"
LIST_PATH = "/homes/nl/en"

#: 固定排序。站点默认按「最早可入住」排，同一天入住的房源很多，它们之间的先后
#: 每次请求都不一样——翻页时同一套会在两页各出现一次，另一套被挤掉。2026-11-17
#: 生产上约两成轮次因此差 1–2 套、判不完整。``newest`` 是站点排序下拉里的选项。
#: 固定排序之后仍有轮次差 1–2 套，真正的原因是分页缓存，见 ``_ROUND_PARAM``。
_SORT = "newest"

#: 每轮一个的缓存键参数名。
#:
#: Cloudflare 按完整 URL 分别缓存每一页（``cf-cache-status: HIT``，``age`` 各页
#: 不同），于是同一轮里拿到的 5 页是不同时刻的快照：各页自报总数一会儿 124、
#: 一会儿 125 / 126，翻页边界上同一套房在相邻两页各出现一次，另一套被挤掉，
#: 本轮判不完整（2026-11-20 复现，sort=newest 也一样）。同一轮所有页带同一个
#: ``r=<轮次时间戳>``，整轮都从源站取同一时刻的数据；下一轮换值。连扫 3 轮都是
#: 124/124、5 页总数一致、全部 MISS。
#:
#: 但源站本身也不一致（同日从服务器上测：全部 MISS 的同一轮里，各页总数在
#: 123 / 124 / 125 之间跳，像是多个后端副本的数据不同步）。v2.3.2 曾经按页重取
#: 最多 4 次、必要时整轮重来，部署后仍只有约一半轮次能凑齐，每轮还多出 10–20 个
#: 请求。所以现在只重取一次碰碰运气，凑不齐就交给 ``_MISS_TOLERANCE``。
_ROUND_PARAM = "r"
_PAGE_RETRIES = 1

#: 去重后的卡片数与自报总数差多少仍算完整。
#:
#: 完整性门槛存在的理由是下架收敛：只有完整的轮次才去老化没出现的房源。但老化
#: 看的是 ``last_seen`` 有多久没刷新（30 分钟转 Reserved，见 mcore/stale.py），
#: 不是「这一轮有没有它」。翻页边界被挤掉的那一两套，下一轮几乎必然回来，离
#: 30 分钟差得远——它们不会被误判。真正需要挡住的是「大面积没抓到」：某页没取到、
#: 卡片结构变了、被带去了维护页。那些情况差的是一整页（25 套）甚至全部，远超这个
#: 容差。所以差 1–2 套照样放行收敛，比为了凑出精确相等而每轮多打十几个请求划算。
_MISS_TOLERANCE_ABS = 2
_MISS_TOLERANCE_RATIO = 0.02

#: 站点每页固定 25 条。只用来估算页数上限，真正的页数以分页控件与总数为准。
_PAGE_SIZE = 25
#: 翻页安全上限：全国 ~150 条只有 6 页，40 页够容纳 1000 条。
_MAX_PAGES = 40
#: 翻页间隔（秒）。整批只有几个请求，不必压榨。
_PAGE_SPACING = 0.8

STATUS_DIRECT = "Available to book"
STATUS_INTEREST = "Available in lottery"

#: Codomo 城市 slug → 注册表里的城市名。注册表是唯一一份城市表。
_SLUG_TO_CITY: dict[str, str] = {c["slug"]: c["city"] for c in KNOWN_CODOMO_CITIES}
_KEY_TO_CITY: dict[str, str] = {c["key"]: c["city"] for c in KNOWN_CODOMO_CITIES}

_OCCUPANCY_WORDS = {"1": "One", "2": "Two", "3": "Three", "4": "Four"}

#: 卡片徽标里与已有字段重复、或不是房源属性的项，不写进 Offer。
_IGNORED_BADGES = frozenset({"book a viewing", "semi-furnished", "student only"})


# ── 卡片 → Listing ──────────────────────────────────────────────────────

def _status_of(card: CodomoCard) -> str:
    """把卡片的预订方式 / 状态文字映射到全平台共用的状态词。"""
    raw = (card.status or "").strip().lower()
    if "reserved" in raw:
        return "Reserved"
    if "occupied" in raw or "not available" in raw:
        return "Not available"
    if "coming soon" in raw:
        return "Coming soon"
    if card.booking_type == "interest":
        return STATUS_INTEREST
    if card.booking_type == "direct":
        return STATUS_DIRECT
    # 既没有「直接预订」也没有「登记意向」按钮：可能是改版了，宁可标未知也不
    # 猜成可订——可订会触发通知。
    return f"Unknown({card.status or '?'})"


def _features(card: CodomoCard) -> list[str]:
    feats: list[str] = []
    if card.bedrooms:
        b = card.bedrooms.strip()
        feats.append(f"Type: {'Studio' if b.lower() == 'studio' else b.split()[0]}")
    if card.area:
        feats.append(f"Area: {card.area} m²")
    if card.persons:
        feats.append(f"Occupancy: {_OCCUPANCY_WORDS.get(card.persons, card.persons + ' persons')}")
    if card.floor is not None:
        feats.append(f"Floor: {card.floor}")
    if card.finishing:
        feats.append(f"Finishing: {card.finishing}")
    if card.building:
        feats.append(f"Building: {card.building}")
    badges_low = {b.lower() for b in card.badges}
    if "student only" in badges_low:
        feats.append("Tenant: student only")
    for b in card.badges:
        if b.lower() not in _IGNORED_BADGES:
            feats.append(f"Offer: {b}")
    if card.min_stay:
        feats.append(f"Min stay: {card.min_stay.removeprefix('Min ').strip()}")
    if card.interest_count is not None:
        feats.append(f"Interested: {card.interest_count}")
    # 不写 Address：地图按 name + city 做 geocode（mstorage/_map_calendar.py），
    # name 本身就是街道地址，与旧 H2S 房源同一个缓存键，已缓存的坐标照样命中。
    return feats


def card_to_listing(card: CodomoCard, city: str) -> Listing:
    total = card.total_rent
    return Listing(
        id=card.slug,
        name=card.address or card.slug,
        status=_status_of(card),
        # 含服务费的到手价，与旧站 price_range 同口径——租金筛选跨平台共用这个数。
        price_raw=f"€{total}" if total else None,
        available_from=card.available_from,
        features=_features(card),
        url=BASE_URL + card.url_path,
        city=city,
        source="codomo",
    )


def city_for_card(card: CodomoCard) -> Optional[str]:
    """卡片所属城市（注册表里的名字）。不认识的 slug 返回 None。"""
    return _SLUG_TO_CITY.get(card.city_slug)


# ── Scraper ─────────────────────────────────────────────────────────────

class CodomoScraper(AbstractScraper):
    """Codomo 全国列表页抓取器（纯 HTTP）。"""

    source = "codomo"

    def __init__(self) -> None:
        #: 本批次的抓取结果：(城市 → listings, 是否完整)。batch_session 内首个
        #: scrape() 时抓一次，各城市 task 复用。
        self._batch: Optional[tuple[dict[str, list[Listing]], bool]] = None

    # ── 取页 ────────────────────────────────────────────────────────

    def _session(self):
        """与其余 HTTP 平台同策略的会话。代理全部冷却时显式直连（NO_PROXY_CURL），
        不能传 {}——curl 会回落到环境变量里那个刚被判定失效的代理。"""
        import curl_cffi.requests as req

        from config import get_impersonate, get_proxy_url
        from net import NO_PROXY_CURL, curl_scrape_proxy

        proxy = curl_scrape_proxy(get_proxy_url(self.source))
        proxies = {"http": proxy, "https": proxy} if proxy else NO_PROXY_CURL
        return req.Session(impersonate=get_impersonate(), proxies=proxies)

    def _get(self, session, page: int, round_key: str = "") -> str:
        url = BASE_URL + LIST_PATH
        params = {"sort": _SORT}
        if round_key:
            params[_ROUND_PARAM] = round_key
        if page > 1:
            params["page"] = page
        for attempt in range(len(RATE_LIMIT_BACKOFF) + 1):
            try:
                resp = session.get(url, params=params, timeout=30, headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                })
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                # 必须 from e：dispatcher 靠异常链识别代理故障（is_proxy_error）。
                raise ScrapeNetworkError(
                    f"Codomo 列表第 {page} 页请求失败: {type(e).__name__}: {e}") from e
            status = resp.status_code
            body = resp.text or ""
            # 维护时站点把所有页面 302 到另一个域名的占位页（2026-11-16 实测：
            # codomo-maintenance.pages.dev，标题 "Something new is on its way"），
            # 跟随重定向后拿到的是 200。正文措辞会换，按「被带离了 codomo.eu」判。
            final_host = urlsplit(str(getattr(resp, "url", "") or url)).hostname or ""
            if final_host and final_host != _HOST:
                raise UpstreamMaintenanceError(
                    f"Codomo 站点维护中（被重定向到 {final_host}）")
            if status == 200:
                if is_maintenance_body(body):
                    raise UpstreamMaintenanceError("Codomo 站点维护中")
                return body
            if status == 429:
                if attempt < len(RATE_LIMIT_BACKOFF):
                    wait = RATE_LIMIT_BACKOFF[attempt]
                    logger.warning("Codomo 返回 429，%d 秒后重试（第 %d 页）", wait, page)
                    time.sleep(wait)
                    continue
                raise RateLimitError("Codomo 持续 429")
            if status == 403:
                kind = "Cloudflare 挑战" if is_cloudflare_body(body) else "站点拒绝"
                raise BlockedError(f"Codomo 返回 403（{kind}）")
            if status in (502, 503, 504) and is_maintenance_body(body):
                raise UpstreamMaintenanceError(f"Codomo 维护（HTTP {status}）")
            raise ScrapeNetworkError(
                f"Codomo 列表第 {page} 页返回 HTTP {status}（站点可能改版，检查 {LIST_PATH}）")
        raise RateLimitError("Codomo 持续 429")  # pragma: no cover

    def _fetch_pages(self, session, round_key: str):
        """按同一个缓存键翻完全部页，返回 (cards, total, last_page, unresolved, totals_seen)。"""
        cards: dict[str, CodomoCard] = {}
        total: Optional[int] = None
        last_page = 1
        unresolved = 0
        totals_seen: set[int] = set()
        page = 1
        while page <= min(last_page, _MAX_PAGES):
            if page > 1:
                time.sleep(_PAGE_SPACING)
            parsed = parse_listing_page(self._get(session, page, round_key))
            retries = 0
            while (page > 1 and total is not None and parsed.total is not None
                   and parsed.total != total and retries < _PAGE_RETRIES):
                retries += 1
                time.sleep(_PAGE_SPACING)
                parsed = parse_listing_page(
                    self._get(session, page, f"{round_key}-{retries}"))
            if retries:
                logger.info("Codomo 第 %d 页总数与第 1 页不一致，重取 %d 次（最终 %s / %s）",
                            page, retries, parsed.total, total)
            unresolved += parsed.unresolved_placeholders
            if parsed.total is not None:
                totals_seen.add(parsed.total)
            if page == 1:
                total = parsed.total
                by_count = math.ceil(total / _PAGE_SIZE) if total else 1
                last_page = max(parsed.max_page or 1, by_count)
            new = 0
            for c in parsed.cards:
                if c.slug not in cards:
                    cards[c.slug] = c
                    new += 1
            if page > 1 and new == 0:
                # 超出范围的页号可能回第 1 页或空页；没有新卡片就别再翻
                break
            page += 1

        return cards, total, last_page, unresolved, totals_seen

    def _fetch_all(self) -> tuple[dict[str, list[Listing]], bool]:
        """抓全国列表，返回 ({城市: listings}, 是否完整)。

        完整的判据：所有页都取到，且去重后的卡片数与页面自报的总数相差不超过
        容差（见 ``_MISS_TOLERANCE_ABS``）。不完整的结果不参与「消失即下架」的收敛。
        """
        with self._session() as session:
            cards, total, last_page, unresolved, totals_seen = self._fetch_pages(
                session, str(int(time.time())))

        complete = True
        if total is None:
            logger.warning("Codomo 列表页读不到「N residences」总数，本轮标记不完整")
            complete = False
        elif len(cards) != total:
            gap = abs(total - len(cards))
            tolerance = max(_MISS_TOLERANCE_ABS, math.ceil(total * _MISS_TOLERANCE_RATIO))
            if gap <= tolerance:
                # 源站副本不同步造成的翻页边界抖动（各页总数 %s），见 _MISS_TOLERANCE_ABS
                logger.info(
                    "Codomo 自报 %d 套，解析出 %d 套，差 %d 在容差 %d 内（各页总数 %s），按完整处理",
                    total, len(cards), gap, tolerance, sorted(totals_seen))
            else:
                logger.warning(
                    "Codomo 自报 %d 套，实际解析出 %d 套（%d 页），本轮标记不完整",
                    total, len(cards), last_page)
                complete = False
        if total and not cards:
            logger.warning("Codomo 自报 %d 套但一张卡片都没解析出来——卡片结构可能变了", total)
            complete = False

        by_city: dict[str, list[Listing]] = {}
        unknown: dict[str, int] = {}
        for c in cards.values():
            city = city_for_card(c)
            if city is None:
                unknown[c.city_slug] = unknown.get(c.city_slug, 0) + 1
                continue
            if c.rent is None:
                logger.warning("Codomo %s 没解析出价格", c.slug)
            by_city.setdefault(city, []).append(card_to_listing(c, city))
        if unknown:
            # 与 Plaza 同一套漂移告警：站点上了新城市，注册表还没登记，这些房源
            # 不会分给任何城市。见 mcore/registry.py 的 KNOWN_CODOMO_CITIES。
            logger.warning("Codomo 出现未登记的城市（房源被丢弃）：%s",
                           ", ".join(f"{k}×{v}" for k, v in sorted(unknown.items())))
        if unresolved:
            logger.info("Codomo 页面有 %d 个未展开的流式占位符（不影响卡片）", unresolved)
        logger.info("Codomo 全国共 %d 套%s", len(cards), "（完整）" if complete else "")
        return by_city, complete

    @contextmanager
    def batch_session(self):
        self._batch = None
        try:
            yield
        finally:
            self._batch = None

    def invalidate_session(self) -> None:
        self._batch = None

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        if self._batch is None:
            self._batch = self._fetch_all()
        by_city, complete = self._batch
        # 按订阅 key 查城市，而不是按显示名：显示名是用户可改的（"Den Haag,90"
        # 与注册表的 "The Hague" 应当是同一个城市）。
        city = _KEY_TO_CITY.get(task.city_key, task.city_display)
        mine = list(by_city.get(city, []))
        logger.info("[%s] Codomo %d 条%s", task.city_display, len(mine),
                    "（完整）" if complete else "")
        return ScrapeResult(task=task, listings=mine, complete=complete)
