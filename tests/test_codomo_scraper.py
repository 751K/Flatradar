"""Codomo（原 Holland2Stay）列表页解析与抓取器。

fixture 是 2026-11-16 的线上原文（gzip）：
- codomo_nl_p4.html.gz        第 4 页，含流式渲染占位符（规格、价格在文档末尾）
- codomo_nl_p6.html.gz        最后一页（17 条）
- codomo_nl_interest.html.gz  ?publish_mode=interest，全是登记意向房源
"""
from __future__ import annotations

import gzip
from pathlib import Path
from unittest.mock import patch

import pytest

from mcore.registry import KNOWN_CODOMO_CITIES
from scrapers.base import BlockedError, ScrapeNetworkError, ScrapeTask
from scrapers.codomo import (
    STATUS_DIRECT,
    STATUS_INTEREST,
    CodomoScraper,
    card_to_listing,
    city_for_card,
)
from scrapers.codomo_parse import parse_listing_page

FIX = Path(__file__).parent / "fixtures"


def _page(name: str) -> str:
    return gzip.decompress((FIX / name).read_bytes()).decode("utf-8")


# ── 解析 ────────────────────────────────────────────────────────────────

class TestParse:
    def test_page_counts_and_total(self):
        p = parse_listing_page(_page("codomo_nl_p4.html.gz"))
        assert len(p.cards) == 25
        assert p.total == 142
        assert p.max_page == 6

    def test_streamed_placeholders_are_resolved(self):
        """第 4 页有 6 张卡片的规格与价格是 Suspense 占位符，必须换回来。"""
        p = parse_listing_page(_page("codomo_nl_p4.html.gz"))
        for c in p.cards:
            assert c.rent, c.slug
            assert c.area and c.persons and c.floor is not None and c.building, c.slug

    def test_direct_card_fields(self):
        p = parse_listing_page(_page("codomo_nl_p4.html.gz"))
        c = next(c for c in p.cards if c.booking_type == "direct")
        assert c.status == "Available"
        assert c.available_from and len(c.available_from) == 10
        assert c.min_stay.startswith("Min ")
        assert c.service_costs is not None
        assert c.total_rent == c.rent + c.service_costs
        assert c.url_path.startswith("/homes/nl/en/")
        assert c.slug == c.url_path.rsplit("/", 1)[-1]

    def test_interest_cards(self):
        p = parse_listing_page(_page("codomo_nl_interest.html.gz"))
        assert p.total == 28
        assert p.cards and all(c.booking_type == "interest" for c in p.cards)
        assert all(c.interest_count is not None for c in p.cards)

    def test_student_only_badge(self):
        p = parse_listing_page(_page("codomo_nl_interest.html.gz"))
        c = next(c for c in p.cards if "Student only" in c.badges)
        assert c.building  # 徽标不能把楼盘名挤掉

    def test_garbage_html_yields_no_cards(self):
        p = parse_listing_page("<html><body><h1>Hi</h1></body></html>")
        assert p.cards == [] and p.total is None


# ── 卡片 → Listing ──────────────────────────────────────────────────────

class TestListing:
    def test_direct_listing(self):
        c = next(c for c in parse_listing_page(_page("codomo_nl_p4.html.gz")).cards
                 if c.booking_type == "direct")
        city = city_for_card(c)
        assert city
        l = card_to_listing(c, city)
        assert l.source == "codomo"
        assert l.status == STATUS_DIRECT
        assert l.id == c.slug
        assert l.url == "https://codomo.eu" + c.url_path
        assert l.price_raw == f"€{c.total_rent}"
        keys = {f.split(":", 1)[0] for f in l.features}
        assert {"Type", "Area", "Occupancy", "Floor", "Finishing", "Building"} <= keys

    def test_interest_is_lottery(self):
        c = parse_listing_page(_page("codomo_nl_interest.html.gz")).cards[0]
        l = card_to_listing(c, city_for_card(c) or "X")
        assert l.status == STATUS_INTEREST
        assert any(f.startswith("Interested: ") for f in l.features)

    def test_unknown_cta_is_not_bookable(self):
        """既没有直接预订也没有登记意向：不能当成可订去发通知。"""
        c = parse_listing_page(_page("codomo_nl_p4.html.gz")).cards[0]
        c.booking_type = ""
        c.status = "Something new"
        assert card_to_listing(c, "Eindhoven").status.startswith("Unknown(")

    def test_den_haag_maps_to_the_hague(self):
        c = parse_listing_page(_page("codomo_nl_p4.html.gz")).cards[0]
        c.city_slug = "den-haag"
        assert city_for_card(c) == "The Hague"


# ── 抓取器 ──────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status: int, text: str, url: str = "https://codomo.eu/homes/nl/en"):
        self.status_code = status
        self.text = text
        self.url = url


class _Session:
    def __init__(self, pages: dict[int, _Resp]):
        self.pages = pages
        self.calls: list[int] = []
        self.params: list[dict] = []

    def get(self, url, params=None, **kw):
        n = int((params or {}).get("page", 1))
        self.calls.append(n)
        self.params.append(dict(params or {}))
        return self.pages[n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _scraper_with(session: _Session) -> CodomoScraper:
    s = CodomoScraper()
    s._session = lambda: session  # type: ignore[method-assign]
    return s


def _task(key="29", name="Eindhoven"):
    return ScrapeTask(source="codomo", city_key=key, city_display=name)


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("scrapers.codomo.time.sleep"):
        yield


class TestScraper:
    def test_partial_pages_are_incomplete(self):
        """只拿到 2 页（50 条）而自报 142 条 → 不完整，不能参与下架收敛。"""
        p4 = _page("codomo_nl_p4.html.gz")
        p6 = _page("codomo_nl_p6.html.gz")
        sess = _Session({1: _Resp(200, p4), **{i: _Resp(200, p6) for i in range(2, 7)}})
        s = _scraper_with(sess)
        with s.batch_session():
            r = s.scrape(_task())
        assert r.complete is False
        assert sess.calls[0] == 1

    def test_batch_fetches_once_for_all_cities(self):
        """整批只抓一遍全国列表，各城市 task 从同一份结果里切。"""
        p6 = _page("codomo_nl_p6.html.gz")
        page = parse_listing_page(p6)
        page.total, page.max_page = len(page.cards), 1   # 当成一页就是全部
        sess = _Session({1: _Resp(200, p6)})
        s = _scraper_with(sess)
        key_of = {c["city"]: c["key"] for c in KNOWN_CODOMO_CITIES}
        cities = sorted({city_for_card(c) for c in page.cards} - {None})
        with patch("scrapers.codomo.parse_listing_page", return_value=page), \
                s.batch_session():
            results = [s.scrape(_task(key=key_of[c], name=c)) for c in cities]
        assert sess.calls == [1]
        assert all(r.complete for r in results)
        assert sum(len(r.listings) for r in results) == len(page.cards)
        for r in results:
            assert all(l.city == r.task.city_display for l in r.listings)

    def test_display_name_does_not_matter(self):
        """按订阅 key 归城：用户把 The Hague 写成 Den Haag 也照样分得到。"""
        p6 = _page("codomo_nl_p6.html.gz")
        page = parse_listing_page(p6)
        page.total, page.max_page = len(page.cards), 1
        for c in page.cards:
            c.city_slug = "den-haag"
        s = _scraper_with(_Session({1: _Resp(200, p6)}))
        with patch("scrapers.codomo.parse_listing_page", return_value=page), \
                s.batch_session():
            r = s.scrape(_task(key="90", name="Den Haag"))
        assert len(r.listings) == len(page.cards)

    def test_403_is_blocked(self):
        s = _scraper_with(_Session({1: _Resp(403, "<html>Just a moment...</html>")}))
        with s.batch_session(), pytest.raises(BlockedError):
            s.scrape(_task())

    def test_redirect_off_site_is_maintenance(self):
        """维护时整站 302 到 codomo-maintenance.pages.dev，跟随后是 200。"""
        from scrapers.base import UpstreamMaintenanceError
        page = _Resp(200, "<html><title>Codomo | Something new is on its way</title></html>",
                     url="https://codomo-maintenance.pages.dev/")
        s = _scraper_with(_Session({1: page}))
        with s.batch_session(), pytest.raises(UpstreamMaintenanceError):
            s.scrape(_task())

    def test_404_is_network_error(self):
        s = _scraper_with(_Session({1: _Resp(404, "not found")}))
        with s.batch_session(), pytest.raises(ScrapeNetworkError):
            s.scrape(_task())

    def test_transport_error_is_chained(self):
        class Boom(_Session):
            def get(self, *a, **k):
                raise OSError("CONNECT tunnel failed")
        s = _scraper_with(Boom({}))
        with s.batch_session(), pytest.raises(ScrapeNetworkError) as ei:
            s.scrape(_task())
        assert isinstance(ei.value.__cause__, OSError)


def test_registered_as_codomo():
    from scrapers import SCRAPER_REGISTRY
    assert SCRAPER_REGISTRY["codomo"] is CodomoScraper
    assert "holland2stay" not in SCRAPER_REGISTRY


def test_every_page_request_uses_a_fixed_sort():
    """默认排序在同日入住的房源之间不稳定，翻页会重复一套、漏一套。"""
    from scrapers import codomo

    p4 = _page("codomo_nl_p4.html.gz")
    p6 = _page("codomo_nl_p6.html.gz")
    sess = _Session({1: _Resp(200, p4), **{i: _Resp(200, p6) for i in range(2, 7)}})
    s = _scraper_with(sess)
    with s.batch_session():
        s.scrape(_task())
    assert len(sess.params) > 1, "应当翻了好几页"
    assert all(p.get("sort") == codomo._SORT for p in sess.params)


# ── Cloudflare 分页缓存：同一轮各页要是同一时刻的数据（2026-11-20）─────

def _two_page_round(second_total_delta: int):
    """两页、共 n 张卡片的一轮；第 2 页自报总数可以偏一点，模拟另一时刻的缓存。"""
    from dataclasses import replace

    base = parse_listing_page(_page("codomo_nl_p6.html.gz"))
    half = len(base.cards) // 2
    n = len(base.cards)
    p1 = replace(base, cards=base.cards[:half], total=n, max_page=2)
    p2 = replace(base, cards=base.cards[half:], total=n + second_total_delta, max_page=2)
    return {1: p1, 2: p2}


def _run_round(pages):
    sess = _Session({1: _Resp(200, "p1"), 2: _Resp(200, "p2")})
    s = _scraper_with(sess)
    with patch("scrapers.codomo.parse_listing_page",
               side_effect=lambda html: pages[1 if html == "p1" else 2]), \
            s.batch_session():
        by_city, complete = s._fetch_all()
    return sess, complete


def test_all_pages_of_a_round_share_one_cache_key():
    """Cloudflare 按完整 URL 缓存每一页；同一轮带同一个 r=，整轮都绕开缓存。"""
    from scrapers import codomo

    with patch("scrapers.codomo.time.time", return_value=1795183363.4):
        sess, complete = _run_round(_two_page_round(0))
    keys = {p.get(codomo._ROUND_PARAM) for p in sess.params}
    assert keys == {"1795183363"}
    assert complete is True


def test_cache_key_changes_between_rounds():
    from scrapers import codomo

    with patch("scrapers.codomo.time.time", return_value=100.0):
        a, _ = _run_round(_two_page_round(0))
    with patch("scrapers.codomo.time.time", return_value=250.0):
        b, _ = _run_round(_two_page_round(0))
    assert a.params[0][codomo._ROUND_PARAM] != b.params[0][codomo._ROUND_PARAM]


def test_page_from_another_backend_state_is_refetched():
    """源站副本不同步：第 2 页先拿到另一份数据（总数 +1），换缓存键重取后对上。"""
    from scrapers import codomo

    good = _two_page_round(0)
    bad_p2 = _two_page_round(+1)[2]
    served = iter(["p1", "p2-bad", "p2"])

    class _Flaky(_Session):
        def get(self, url, params=None, **kw):
            self.params.append(dict(params or {}))
            return _Resp(200, next(served))

    sess = _Flaky({})
    s = _scraper_with(sess)
    pick = {"p1": good[1], "p2-bad": bad_p2, "p2": good[2]}
    with patch("scrapers.codomo.time.time", return_value=500.0), \
            patch("scrapers.codomo.parse_listing_page", side_effect=lambda h: pick[h]), \
            s.batch_session():
        _, complete = s._fetch_all()
    assert complete is True
    keys = [p[codomo._ROUND_PARAM] for p in sess.params]
    assert keys == ["500", "500", "500-1"], "重取要换缓存键，否则拿回的是同一份缓存"


def test_retries_are_bounded():
    """源站副本不同步时只重取一次碰运气，不为凑齐总数每轮多打十几个请求。"""
    from scrapers import codomo

    sess, _ = _run_round(_two_page_round(+1))
    assert len(sess.params) == 2 + codomo._PAGE_RETRIES


def _round_with_cards(drop: int, total_delta: int = 0):
    """一页的轮次：自报总数 = 卡片数 + drop（模拟翻页边界挤掉 drop 套）。"""
    from dataclasses import replace

    base = parse_listing_page(_page("codomo_nl_p6.html.gz"))
    page = replace(base, total=len(base.cards) + drop, max_page=1)
    # 第 2 页起返回同一份卡片 = 没有新卡片，翻页就此停下
    sess = _Session({i: _Resp(200, "p1") for i in range(1, 4)})
    s = _scraper_with(sess)
    with patch("scrapers.codomo.parse_listing_page", return_value=page), s.batch_session():
        _, complete = s._fetch_all()
    return complete


def test_boundary_jitter_within_tolerance_counts_as_complete():
    """差 1–2 套是翻页边界抖动：下架看 last_seen 是否超过 30 分钟，被挤掉的
    房源下一轮就回来，不会被误判，所以照常参与收敛。"""
    assert _round_with_cards(1) is True
    assert _round_with_cards(2) is True
    assert _round_with_cards(-1) is True   # 多出一套（同一套跨页重复的反面）同理


def test_missing_a_page_is_still_incomplete():
    """差一整页（25 套）是没抓到，不是抖动，必须挡住收敛。"""
    assert _round_with_cards(25) is False
