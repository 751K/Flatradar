"""dispatcher 的完整扫描信号聚合测试。

平台内部「什么时候算抓全」由各自的 scraper 测试覆盖（如 test_codomo_scraper.py）；
这里只管 dispatcher 怎么把每个 task 的 ``ScrapeResult.complete`` 聚合成
``completeness`` 表——那张表决定哪些城市能做 stale 收敛。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from models import Listing
from scrapers import dispatch_scrape_tasks
from scrapers.base import ScrapeNetworkError, ScrapeResult, ScrapeTask


def _codomo_tasks(*pairs: tuple[str, str]) -> list[ScrapeTask]:
    return [
        ScrapeTask(source="codomo", city_key=key, city_display=name)
        for name, key in pairs
    ]


def _listing(listing_id: str, city: str) -> Listing:
    return Listing(
        id=listing_id,
        name=f"Listing {listing_id}",
        status="Available to book",
        price_raw="€1000",
        available_from="2030-01-01",
        features=[],
        url=f"https://example.test/{listing_id}",
        city=city,
        source="codomo",
    )


def _results(*items):
    """把 (listings, complete) / 异常 依次变成 scrape() 的返回值。"""
    it = iter(items)

    def scrape(task):
        item = next(it)
        if isinstance(item, BaseException):
            raise item
        listings, complete = item
        return ScrapeResult(task=task, listings=listings, complete=complete)

    return scrape


_PATCH_SCRAPE = "scrapers.codomo.CodomoScraper.scrape"


class TestDispatchCompleteness:
    def test_returns_city_completeness_map(self):
        with patch(_PATCH_SCRAPE, side_effect=_results(
            ([_listing("a", "Eindhoven")], True),
            ([], False),
        )):
            listings, completeness = dispatch_scrape_tasks(
                _codomo_tasks(("Eindhoven", "29"), ("Amsterdam", "24"))
            )

        assert [l.id for l in listings] == ["a"]
        assert completeness == {"Eindhoven": True, "Amsterdam": False}

    def test_city_with_network_failure_is_omitted_from_completeness(self):
        with patch(_PATCH_SCRAPE, side_effect=_results(
            ([], True),
            ScrapeNetworkError("page 1 failed"),
        )):
            listings, completeness = dispatch_scrape_tasks(
                _codomo_tasks(("Eindhoven", "29"), ("Amsterdam", "24"))
            )

        assert listings == []
        assert completeness == {"Eindhoven": True}

    def test_all_network_failures_still_raise(self):
        with patch(_PATCH_SCRAPE, side_effect=ScrapeNetworkError("page 1 failed")):
            with pytest.raises(ScrapeNetworkError):
                dispatch_scrape_tasks(
                    _codomo_tasks(("Eindhoven", "29"), ("Amsterdam", "24"))
                )

    def test_multi_source_prefixes_keys(self):
        with patch(_PATCH_SCRAPE, side_effect=_results(([], True))):
            _, completeness = dispatch_scrape_tasks(
                _codomo_tasks(("Eindhoven", "29")), multi_source=True,
            )
        assert completeness == {"codomo:Eindhoven": True}
