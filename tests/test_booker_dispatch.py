"""
bookers/__init__.py 的 dispatcher 单元测试
==========================================

覆盖三种路径：
1. listing.source="plaza" → 路由到 PlazaBooker（验证转发）
2. listing.source="xior"/"ourdomain" → 路由到 RENTCafe booker
3. listing.source="unknown_xxx" → 返回 phase="unsupported"

以及 mcore.booking.book_with_fallback 过滤候选的回归保护：
- 混合 source 列表：Plaza + 未知 source 候选混着进，未知 source 应被过滤、
  不会触发任何 dispatch（重要：避免对不支持平台产生"预订失败"误报）
"""
from __future__ import annotations

from unittest.mock import patch


from bookers import (
    AbstractBooker,
    BOOKER_REGISTRY,
    BookingRequest,
    PlazaBooker,
    dispatch_book,
    get_booker,
    supports_booking,
)
from bookers.base import BookingResult as RawBookingResult
from mcore.booking import book_with_fallback
from models import Listing
from users import AutoBookConfig, UserConfig


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _make_listing(lid: str, source: str = "plaza") -> Listing:
    return Listing(
        id=lid,
        name=f"unit {lid}",
        status="Available to book",
        price_raw="€700",
        available_from="2026-06-01",
        features=["Area: 30.0 m²"],
        url="http://example.com",
        city="Amsterdam",
        source=source,
    )


def _make_user() -> UserConfig:
    return UserConfig(
        name="alice", id="a1a1a1a1",
        auto_book=AutoBookConfig(
            enabled=True, plaza_username="alice", plaza_password="pw",
        ),
    )


# ──────────────────────────────────────────────────────────────────
# Registry / lookup
# ──────────────────────────────────────────────────────────────────


class TestRegistry:
    def test_plaza_registered(self):
        assert "plaza" in BOOKER_REGISTRY
        assert BOOKER_REGISTRY["plaza"] is PlazaBooker

    def test_codomo_has_no_booker(self):
        """Codomo（原 Holland2Stay）改版后旧下单接口已失效，不应注册 booker。"""
        assert "codomo" not in BOOKER_REGISTRY
        assert "holland2stay" not in BOOKER_REGISTRY

    def test_xior_and_ourdomain_registered(self):
        assert "xior" in BOOKER_REGISTRY
        assert "ourdomain" in BOOKER_REGISTRY
        from bookers.rentcafe import XiorBooker, OurDomainBooker
        assert BOOKER_REGISTRY["xior"] is XiorBooker
        assert BOOKER_REGISTRY["ourdomain"] is OurDomainBooker

    def test_get_booker_plaza_returns_instance(self):
        booker = get_booker("plaza")
        assert isinstance(booker, PlazaBooker)
        assert isinstance(booker, AbstractBooker)

    def test_get_booker_unregistered_returns_none(self):
        assert get_booker("nonsense") is None
        assert get_booker("") is None

    def test_supports_booking(self):
        assert supports_booking("plaza") is True
        assert supports_booking("codomo") is False
        assert supports_booking("xior") is True
        assert supports_booking("ourdomain") is True
        assert supports_booking("") is False
        assert supports_booking("nonsense") is False


# ──────────────────────────────────────────────────────────────────
# dispatch_book routing
# ──────────────────────────────────────────────────────────────────


class TestDispatchBook:
    def test_plaza_listing_calls_plaza_booker(self):
        listing = _make_listing("pz-1", source="plaza")
        user = _make_user()
        expected = RawBookingResult(listing, success=True, message="ok",
                                    pay_url="http://pay", phase="success")
        with patch("bookers.plaza.PlazaBooker.book", return_value=expected) as mock:
            result = dispatch_book(
                BookingRequest(listing=listing, user=user, dry_run=False)
            )
        mock.assert_called_once()
        (request,), _ = mock.call_args
        assert request.listing is listing
        assert request.dry_run is False
        # 返回值完整透传
        assert result is expected
        assert result.phase == "success"

    def test_ourdomain_routes_to_rentcafe_booker(self):
        """OurDomain 现在有 RENTCafe booker，应路由到 OurDomainBooker.book()"""
        listing = _make_listing("od-1", source="ourdomain")
        user = _make_user()
        with patch("bookers.rentcafe.OurDomainBooker.book", return_value=RawBookingResult(listing, success=True, message="ok")) as mock:
            result = dispatch_book(
                BookingRequest(listing=listing, user=user)
            )
        mock.assert_called_once()
        assert result.success is True

    def test_xior_routes_to_rentcafe_booker(self):
        listing = _make_listing("xr-1", source="xior")
        user = _make_user()
        with patch("bookers.rentcafe.XiorBooker.book", return_value=RawBookingResult(listing, success=True, message="ok")) as mock:
            result = dispatch_book(
                BookingRequest(listing=listing, user=user)
            )
        mock.assert_called_once()
        assert result.success is True

    def test_unknown_source_returns_unsupported(self):
        listing = _make_listing("x-1", source="parariusxxx")
        user = _make_user()
        result = dispatch_book(BookingRequest(listing=listing, user=user))
        assert result.phase == "unsupported"
        assert result.success is False

    def test_empty_source_falls_through_unsupported(self):
        listing = _make_listing("y-1", source="")
        user = _make_user()
        result = dispatch_book(BookingRequest(listing=listing, user=user))
        # 空 source → 视为未知，phase=unsupported
        assert result.phase == "unsupported"


# ──────────────────────────────────────────────────────────────────
# book_with_fallback 的候选过滤行为
# ──────────────────────────────────────────────────────────────────


class TestBookWithFallbackFiltering:
    def test_only_unsupported_candidates_short_circuit(self):
        """全部候选都是未知 source → book_with_fallback 立即返回 None。"""
        candidates = [
            _make_listing("xx-1", source="pararius"),
            _make_listing("xx-2", source="pararius"),
        ]
        user = _make_user()
        with patch("bookers.plaza.PlazaBooker.book") as mock:
            result = book_with_fallback(candidates, user, deadline=float("inf"))
        mock.assert_not_called()
        assert result is None

    def test_mixed_supported_and_unsupported(self):
        """Plaza + 未知 source 混合：未知 source 跳过，Plaza 候选正常尝试。"""
        pz_1 = _make_listing("pz-1", source="plaza")
        xx_1 = _make_listing("xx-1", source="pararius")
        pz_2 = _make_listing("pz-2", source="plaza")
        candidates = [pz_1, xx_1, pz_2]
        user = _make_user()

        call_listings = []
        def fake_book(request):
            call_listings.append(request.listing)
            return RawBookingResult(request.listing, success=True, message="ok", phase="success")

        with patch("bookers.plaza.PlazaBooker.book", side_effect=fake_book):
            result = book_with_fallback(candidates, user, deadline=float("inf"))

        assert len(call_listings) == 1
        assert call_listings[0] is pz_1
        assert result.success is True

    def test_mixed_race_lost_skips_unsupported(self):
        """pz_1 race_lost → 跳过未知 source → 试 pz_2。"""
        pz_1 = _make_listing("pz-1", source="plaza")
        xx_1 = _make_listing("xx-1", source="pararius")
        pz_2 = _make_listing("pz-2", source="plaza")
        candidates = [pz_1, xx_1, pz_2]
        user = _make_user()

        call_count = [0]
        def fake_book(request):
            call_count[0] += 1
            if call_count[0] == 1:
                return RawBookingResult(request.listing, success=False, message="raced", phase="race_lost")
            return RawBookingResult(request.listing, success=True, message="ok", phase="success")

        with patch("bookers.plaza.PlazaBooker.book", side_effect=fake_book):
            result = book_with_fallback(candidates, user, deadline=float("inf"))

        assert call_count[0] == 2
        assert result.success is True
