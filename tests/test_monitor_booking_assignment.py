"""monitor 自动预订候选分配测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from bookers.base import BookingResult
from config import AutoBookConfig, Config, TargetFilter
from models import Listing
from monitor import _assign_auto_book_candidates, run_once
from notifier import BaseNotifier, WebNotifier
from storage import Storage
from users import UserConfig


def _listing(lid: str = "L-1", source: str = "plaza") -> Listing:
    return Listing(
        id=lid,
        name=f"Listing {lid}",
        status="Available to book",
        price_raw="€700",
        available_from="2030-01-01",
        features=[],
        url=f"https://example.test/{lid}",
        city="E",
        source=source,
        sku=f"SKU-{lid}",
        contract_id=42,
        contract_start_date="2030-01-01",
    )


def _user(uid: str, name: str, username: str) -> UserConfig:
    auto_book = AutoBookConfig(
        enabled=True,
        dry_run=False,
        plaza_enabled=True,
        plaza_username=username,
        plaza_password="pw",
    )
    return UserConfig(
        id=uid,
        name=name,
        enabled=True,
        notifications_enabled=True,
        notification_channels=[],
        auto_book=auto_book,
    )


class _Notifier(BaseNotifier):
    has_channels = True

    def __init__(self) -> None:
        self.booking_success: list[str] = []
        self.new_listings: list[str] = []

    async def _send(self, text):
        return True

    async def send_new_listing(self, listing):
        self.new_listings.append(listing.id)
        return True

    async def send_booking_success(self, listing, msg, pay_url="", contract_start_date=""):
        self.booking_success.append(listing.id)
        return True

    async def close(self):
        pass


def _cfg(tmp_path) -> Config:
    return Config(
        check_interval=300,
        sources=["plaza"],
        plaza_cities=[TargetFilter(name="E", key="E")],
        db_path=Path(tmp_path) / "test.db",
        log_level="WARNING",
    )


def test_assign_auto_book_candidates_dedupes_same_listing():
    u1 = _user("u1", "Alice", "alice")
    u2 = _user("u2", "Bob", "bob")
    notifier_pairs = [(u1, _Notifier()), (u2, _Notifier())]
    listing = _listing("same")

    assigned = _assign_auto_book_candidates(
        {"u1": [listing], "u2": [listing]},
        notifier_pairs,
    )

    assert sum(len(v) for v in assigned.values()) == 1
    assert assigned["u1"] == [listing]
    assert assigned["u2"] == []


def test_assign_auto_book_candidates_balances_multiple_listings():
    u1 = _user("u1", "Alice", "alice")
    u2 = _user("u2", "Bob", "bob")
    notifier_pairs = [(u1, _Notifier()), (u2, _Notifier())]
    listings = [_listing("L1"), _listing("L2"), _listing("L3")]

    assigned = _assign_auto_book_candidates(
        {"u1": listings, "u2": listings},
        notifier_pairs,
    )

    assert [l.id for l in assigned["u1"]] == ["L1", "L3"]
    assert [l.id for l in assigned["u2"]] == ["L2"]


def test_run_once_books_same_listing_once_for_multiple_matching_users(tmp_path):
    u1 = _user("u1", "Alice", "alice")
    u2 = _user("u2", "Bob", "bob")
    n1 = _Notifier()
    n2 = _Notifier()
    storage = Storage(tmp_path / "test.db", timezone_str="UTC")
    called_users: list[str] = []

    def fake_dispatch(request):
        called_users.append(request.user.auto_book.plaza_username)
        return BookingResult(
            request.listing,
            success=True,
            message="ok",
            pay_url="https://pay.example.test",
            phase="success",
        )

    async def go():
        with patch("mcore.scrape_round.dispatch_scrape_tasks", return_value=[_listing("shared")]), \
             patch("mcore.booking.dispatch_book", side_effect=fake_dispatch):
            await run_once(_cfg(tmp_path), storage, [(u1, n1), (u2, n2)], dry_run=False)

    try:
        asyncio.run(go())
    finally:
        storage.close()

    assert called_users == ["alice"]
    assert n1.booking_success == ["shared"]
    assert n2.booking_success == []
    assert n1.new_listings == ["shared"]
    assert n2.new_listings == ["shared"]


def test_run_once_web_booking_notification_is_scoped_to_assigned_user(tmp_path):
    u1 = _user("u1", "Alice", "alice")
    u2 = _user("u2", "Bob", "bob")
    storage = Storage(tmp_path / "test.db", timezone_str="UTC")

    def fake_dispatch(request):
        return BookingResult(
            request.listing,
            success=True,
            message="ok",
            pay_url="https://pay.example.test",
            phase="success",
        )

    async def go():
        with patch("mcore.scrape_round.dispatch_scrape_tasks", return_value=[_listing("shared")]), \
             patch("mcore.booking.dispatch_book", side_effect=fake_dispatch):
            await run_once(
                _cfg(tmp_path),
                storage,
                [(u1, _Notifier()), (u2, _Notifier())],
                web_notifier=WebNotifier(storage),
                dry_run=False,
            )

    try:
        asyncio.run(go())
        rows = storage.get_notifications(limit=20)
    finally:
        storage.close()

    booking_rows = [r for r in rows if r["type"] == "booking"]
    assert len(booking_rows) == 1
    assert booking_rows[0]["user_id"] == "u1"


def test_run_once_plaza_application_does_not_mark_listing_reserved(tmp_path):
    user = _user("u1", "Alice", "alice")
    notifier = _Notifier()
    storage = Storage(tmp_path / "test.db", timezone_str="UTC")
    listing = _listing("plaza-application", source="plaza")

    def fake_dispatch(request):
        return BookingResult(
            request.listing,
            success=True,
            message="已成功应征这条房源。",
            pay_url=request.listing.url,
            phase="success",
        )

    async def go():
        with patch("mcore.scrape_round.dispatch_scrape_tasks", return_value=[listing]), \
             patch("mcore.booking.dispatch_book", side_effect=fake_dispatch):
            await run_once(_cfg(tmp_path), storage, [(user, notifier)], dry_run=False)

    try:
        asyncio.run(go())
        row = storage.get_listing(listing.id)
    finally:
        storage.close()

    assert row["status"] == "Available to book"
    assert row["status_is_inferred"] == 0


def test_run_once_plaza_applies_to_all_matching_listings(tmp_path):
    user = _user("u1", "Alice", "alice")
    notifier = _Notifier()
    storage = Storage(tmp_path / "test.db", timezone_str="UTC")
    listings = [
        _listing("plaza-1", source="plaza"),
        _listing("plaza-2", source="plaza"),
    ]
    dispatched: list[str] = []

    def fake_dispatch(request):
        dispatched.append(request.listing.id)
        return BookingResult(
            request.listing,
            success=True,
            message="已成功应征这条房源。",
            pay_url=request.listing.url,
            phase="success",
        )

    async def go():
        with patch("mcore.scrape_round.dispatch_scrape_tasks", return_value=listings), \
             patch("mcore.booking.dispatch_book", side_effect=fake_dispatch):
            await run_once(_cfg(tmp_path), storage, [(user, notifier)], dry_run=False)

    try:
        asyncio.run(go())
    finally:
        storage.close()

    assert set(dispatched) == {"plaza-1", "plaza-2"}
    assert len(dispatched) == 2
    assert set(notifier.booking_success) == {"plaza-1", "plaza-2"}
    assert len(notifier.booking_success) == 2
