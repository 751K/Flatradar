"""Canvas scraper 测试。

Canvas 与 OurDomain 同属 Greystar、同一套 RentCafe/SecureRC 后端，所以
``CanvasScraper`` 继承 ``OurDomainScraper``，只覆盖取单元表的请求形状。
本文件锁住的就是「继承了什么」和「改了什么」两件事。

fixtures 是 2026-08-03 从生产站点真实抓的：
- ``canvas_floorplans.html``            floorplans.aspx 里含 FP 锚点的片段
- ``canvas_availableunits_empty.html``  零可订时 availableunits 的真实响应

**没有「有房」的样本**——接入时该楼零可订。所以单元行解析走的是 OurDomain 的
实现，赌两边同模板；依据是两边空响应的结构指纹一致。真实有房时需人工核对一次。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import scrapers
from scrapers.canvas import CanvasScraper
from scrapers.ourdomain import (
    OurDomainScraper,
    _extract_floorplan_ids,
    _extract_floorplan_names,
    _extract_units,
    _looks_like_availability_panel,
)
from scrapers.base import ScrapeTask


_FIX = Path(__file__).parent / "fixtures"
_FP_HTML = (_FIX / "canvas_floorplans.html").read_text(encoding="utf-8")
_EMPTY_UNITS = (_FIX / "canvas_availableunits_empty.html").read_text(encoding="utf-8")
_LOTTERY_UNITS = (_FIX / "canvas_availableunits_lottery.html").read_text(encoding="utf-8")


# ── 注册与配置 ──────────────────────────────────────────────────────

class TestRegistration:
    def test_registered_under_own_source(self):
        assert scrapers.SCRAPER_REGISTRY["canvas"] is CanvasScraper
        assert CanvasScraper.source == "canvas"

    def test_inherits_ourdomain(self):
        """继承是刻意的——指纹池/冷却/403 重试/单元解析全部复用。"""
        assert issubclass(CanvasScraper, OurDomainScraper)

    def test_config_expands_task(self, monkeypatch):
        monkeypatch.setenv("SOURCES", "canvas")
        monkeypatch.setenv("CANVAS_CITIES", "Canvas Amsterdam Diemen,diemen")
        import config
        tasks = config.load_config().scrape_tasks_v2()
        assert [(t.source, t.city_key) for t in tasks] == [("canvas", "diemen")]

    def test_id_prefix_differs_from_ourdomain(self):
        """不同 RentCafe property 的 unit id 是否全局唯一没有保证；
        撞车会让两条房源在 storage.diff() 里合并成同一行、互相覆盖。"""
        assert CanvasScraper.ID_PREFIX == "oc_"
        assert OurDomainScraper.ID_PREFIX == "od_"
        assert CanvasScraper.ID_PREFIX != OurDomainScraper.ID_PREFIX

    def test_building_registry(self):
        b = CanvasScraper.BUILDINGS["diemen"]
        # 2026-11 并入 Canvas 后的 RentCafe property（旧 186609 已 403），见 docs/CANVAS.md
        assert b["property_id"] == "186607"
        assert b["slug"] == "shell-ourcampus-amsterdam-diemen-3p"
        assert CanvasScraper.BASE.startswith(
            "https://canvas-student-amsterdam-diemen-3p-rentcafewebsiteuk.")
        # 显示名不随品牌改：订阅和城市筛选按它匹配
        assert b["display"] == "Canvas Amsterdam Diemen"
        # 街道地址供 geocode 用；unit 名不可 geocode
        assert "Dalsteindreef" in b["street_address"]

    def test_unknown_key_names_canvas_not_ourdomain(self):
        """报错要指向本平台，否则配置写错时会把人引到另一个平台的文档。"""
        s = CanvasScraper()
        with pytest.raises(ValueError, match="Canvas"):
            s._building_for_task(ScrapeTask("canvas", "nope", "Nope"))


# ── 请求形状：POST + floorPlans[] ───────────────────────────────────

class TestRequestShape:
    def _capture(self, scraper):
        session = MagicMock()
        resp = MagicMock(status_code=200, ok=True, text=_EMPTY_UNITS)
        resp.raise_for_status = lambda: None
        session.get.return_value = resp
        session.post.return_value = resp
        scraper._fetch_units_html(
            session, base="https://x.test/onlineleasing", fp_id="1112904",
            property_id="186609", move_in_date="2026-09-01",
            floorplans_url="https://x.test/onlineleasing/slug/floorplans.aspx",
        )
        return session

    def test_canvas_posts_floorplans_array(self):
        """照抄它自己前端：jQuery .load(url, {floorPlans: names}) 走 POST。"""
        session = self._capture(CanvasScraper())
        assert session.get.call_count == 0
        assert session.post.call_count == 1
        _, kwargs = session.post.call_args
        assert kwargs["data"] == [("floorPlans[]", "1112904")]

    def test_canvas_url_has_no_moveindate_or_propertyid(self):
        """它自己的调用不带这两个参数，由会话隐含。"""
        session = self._capture(CanvasScraper())
        url = session.post.call_args[0][0]
        assert "contentclass=availableunits" in url
        assert "MoveInDate" not in url
        assert "myolePropertyID" not in url

    def test_ourdomain_still_uses_get(self):
        """基类不受影响——OurDomain 的 host 上 POST 会 403。"""
        session = self._capture(OurDomainScraper())
        assert session.post.call_count == 0
        assert session.get.call_count == 1
        url = session.get.call_args[0][0]
        assert "floorPlans=1112904" in url
        assert "MoveInDate=2026-09-01" in url
        assert "myolePropertyID=186609" in url


# ── 真实 fixture 解析 ───────────────────────────────────────────────

class TestRealFixtures:
    def test_floorplan_ids_extracted(self):
        assert _extract_floorplan_ids(_FP_HTML) == ["1113259", "1112904", "1112905"]

    def test_floorplan_names_extracted(self):
        names = _extract_floorplan_names(_FP_HTML)
        assert names["1112904"] == "Furnished Student Apartment - 1 Person"
        assert names["1112905"] == "Furnished Student Apartment - 2 Person"
        assert names["1113259"] == "Standard+ Studio Apartment - 1 Person"

    def test_empty_response_is_a_valid_panel(self):
        """零可订时的响应仍是一张结构完整的搜索结果页——不能当成故障。"""
        assert _looks_like_availability_panel(_EMPTY_UNITS) is True
        assert _extract_units(_EMPTY_UNITS) == []


# ── 完整性守卫（基类行为，两个 source 共享）─────────────────────────

class TestAvailabilityPanelGuard:
    """「解析不出单元」要能和「这栋楼没房」区分开。

    RentCafe 两种情况都回 HTTP 200，只看解析结果为空就会把「响应结构变了 /
    拿到别的页面」误报成「没有房」，进而让 stale 收敛把存量 listing 清掉。
    见 ARCHITECTURE.md 的房源状态与完整性。
    """

    def test_real_empty_panel_passes(self):
        assert _looks_like_availability_panel(_EMPTY_UNITS)

    @pytest.mark.parametrize("body", [
        "",
        "<html><body>Something else entirely</body></html>",
        "<html><head><title>Rentcafe Error</title></head><body>404</body></html>",
    ])
    def test_non_panel_bodies_rejected(self, body):
        assert _looks_like_availability_panel(body) is False

    def test_marker_is_case_insensitive(self):
        assert _looks_like_availability_panel("<div>apartment search result</div>")

    def test_scrape_marks_incomplete_when_panel_missing(self, monkeypatch):
        """守卫真的会把该轮标记为 incomplete，而不是返回「0 个单元 + 完整」。"""
        scraper = CanvasScraper()
        monkeypatch.setattr(
            "scrapers.ourdomain._get_text",
            lambda session, url, **kw: (
                _FP_HTML if "floorplans.aspx" in url
                else "<html><body>不是单元面板</body></html>"
            ),
        )
        monkeypatch.setattr(
            CanvasScraper, "_fetch_units_html",
            lambda self, session, **kw: "<html><body>不是单元面板</body></html>",
        )
        result = scraper.scrape(ScrapeTask("canvas", "diemen", "Canvas Amsterdam Diemen"))
        assert result.listings == []
        assert result.complete is False, "响应不像单元面板时必须标记不完整"

    def test_scrape_becomes_complete_on_genuine_empty(self, monkeypatch):
        """真的没房时要判完整，否则 stale 收敛永不执行。

        2026-08-04 起需要**连着几轮**才认——这个守卫只能挡住「这压根不是单元
        面板」，挡不住「是面板，可这一次的内容不对」，两者的状态码和结构完全
        一样。所以这里断言的从「单轮就完整」改成「连够 N 轮才完整」，前面几轮
        必须是 incomplete。见 tests/test_empty_result_confirmation.py。
        """
        from scrapers.ourdomain import _ZERO_ROUND_STATE, _zero_rounds_to_confirm

        _ZERO_ROUND_STATE.clear()
        scraper = CanvasScraper()
        monkeypatch.setattr(
            "scrapers.ourdomain._get_text",
            lambda session, url, **kw: _FP_HTML,
        )
        monkeypatch.setattr(
            CanvasScraper, "_fetch_units_html",
            lambda self, session, **kw: _EMPTY_UNITS,
        )
        task = ScrapeTask("canvas", "diemen", "Canvas Amsterdam Diemen")
        try:
            for _ in range(_zero_rounds_to_confirm() - 1):
                assert scraper.scrape(task).complete is False
            result = scraper.scrape(task)
            assert result.listings == []
            assert result.complete is True
        finally:
            _ZERO_ROUND_STATE.clear()


# ── Listing 映射（复用基类，验证品牌字段没串到 OurDomain）─────────────

class TestListingMapping:
    def test_id_prefix_and_source(self, monkeypatch):
        from scrapers.ourdomain import _to_listing
        unit = {"unit_id": "999", "apt": "#A1", "sqft": "22", "rent": "€ 1.500",
                "deposit": "€ 0", "detail": "", "floor": 0, "status": "Available to book",
                "avail_date": "2026-09-01", "fp_ids": ["1112904"]}
        l = _to_listing(
            unit, base_url="https://x.test/fp.aspx",
            city_display="Canvas Amsterdam Diemen", source="canvas",
            building_label="Canvas Diemen", default_type="Studio",
            street_address="Dalsteindreef 6002, 1112 XC Diemen",
            id_prefix=CanvasScraper.ID_PREFIX,
        )
        assert l.id == "oc_999"
        assert l.source == "canvas"
        assert l.name == "Canvas Diemen #A1"
        assert "Address: Dalsteindreef 6002, 1112 XC Diemen" in l.features
        # detail 为空时兜底用 source，不能写死成 "OurDomain"
        assert "Detail: canvas" in l.features
        assert not any("OurDomain" in f for f in l.features)


# ── 抓取留档 ────────────────────────────────────────────────────────

class TestCapture:
    """存在理由：它的单元表 HTML 至今没有真实样本。

    第一次真的有房时要拿原始 markup 核对解析器——只看日志里的「共抓取 N 个
    单元」不够，最危险的情况恰恰是「结构变了但仍是合法面板」，那会静默返回 0。
    """

    @pytest.fixture
    def cap(self, tmp_path, monkeypatch):
        import scrapers.canvas as oc
        path = tmp_path / "cap.txt"
        monkeypatch.setenv("CANVAS_CAPTURE_PATH", str(path))
        # 签名是模块级的：不清空的话，上一个测试写过的同一份响应在这里会被当成重复跳过
        monkeypatch.setattr(oc, "_LAST_SIGNATURE", {})
        return path

    def _read(self, path):
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_empty_response_logs_summary_only(self, cap):
        from scrapers.canvas import _record_capture
        _record_capture("1112904", _EMPTY_UNITS)
        txt = self._read(cap)
        assert "fp=1112904" in txt and "parsed=0" in txt and "panel=yes" in txt
        # 零可订是常态，不能每轮都塞一份 32KB HTML
        assert "完整响应" not in txt
        assert len(txt) < 500

    def test_response_with_units_keeps_full_html(self, cap):
        from scrapers.canvas import _record_capture
        html = _EMPTY_UNITS.replace(
            "Apartment Search Result",
            'Apartment Search Result</div><tr id="unitrow_999" data-selenium-id="urow1">'
            '<th data-selenium-id="Apt1" id="999">#OC1</th>'
            '<td data-selenium-id="SqFt1">24</td></tr>',
        )
        _record_capture("1113259", html)
        txt = self._read(cap)
        assert "parsed=1" in txt and "unitrow=yes" in txt
        assert "完整响应" in txt, "第一份真实样本必须完整留下来"
        assert "unitrow_999" in txt

    def test_unitrow_present_but_parsed_zero_is_kept(self, cap):
        """守卫兜不住的那种情况：仍是合法面板，但解析器对不上。"""
        from scrapers.canvas import _record_capture
        html = _EMPTY_UNITS.replace(
            "Apartment Search Result",
            'Apartment Search Result</div><tr id="unitrow_888" class="brand-new-theme"></tr>',
        )
        _record_capture("1112905", html)
        txt = self._read(cap)
        assert "unitrow=yes" in txt and "parsed=0" in txt
        assert "完整响应" in txt, "解析器可能失配时更要留原始 HTML"

    def test_non_panel_response_is_flagged(self, cap):
        from scrapers.canvas import _record_capture
        _record_capture("1112904", "<html><body>Rentcafe Error</body></html>")
        assert "panel=NO" in self._read(cap)

    def test_appends_across_calls(self, cap):
        from scrapers.canvas import _record_capture
        for fp in ("a", "b", "c"):
            _record_capture(fp, _EMPTY_UNITS)
        heads = [l for l in self._read(cap).splitlines() if l.startswith("=== ")]
        assert len(heads) == 3

    def test_size_cap_rotates_instead_of_stopping(self, cap, monkeypatch):
        """原来满了就停写。2026-09-15 09:07 撞上限，13:53 那批五套房的 HTML
        一个字节都没留下——证据恰好在最需要的时候断掉。"""
        import scrapers.canvas as oc
        monkeypatch.setattr(oc, "_CAPTURE_MAX_BYTES", 10)  # 一行摘要就超
        oc._record_capture("a", _EMPTY_UNITS)
        first = self._read(cap)
        oc._record_capture("b", _EMPTY_UNITS)
        assert "fp=b" in self._read(cap), "超过上限后必须继续写"
        assert "fp=a" not in self._read(cap)
        rotated = cap.with_name(cap.name + ".1")
        assert rotated.read_text(encoding="utf-8") == first, "旧内容轮转到 .1，不是丢掉"

    def test_identical_response_is_written_once(self, cap):
        """36 天 49235 行摘要、有房时每轮三份 35 KB——8 MB 就是这么满的。"""
        from scrapers.canvas import _record_capture
        for _ in range(50):
            _record_capture("1113259", _LOTTERY_UNITS)
        heads = [l for l in self._read(cap).splitlines() if l.startswith("=== ")]
        assert len(heads) == 1
        assert self._read(cap).count("完整响应") == 1

    def test_button_label_change_is_written(self, cap):
        """同一套房、同一个价，只有按钮从 Book Now 变成 Join Lottery——
        这正是要留档核对的那种变化，不能被当成重复吞掉。"""
        from scrapers.canvas import _record_capture
        _record_capture("1113259", _LOTTERY_UNITS.replace("Join Lottery", "Book Now"))
        _record_capture("1113259", _LOTTERY_UNITS)
        txt = self._read(cap)
        assert txt.count("完整响应") == 2
        assert "value ='Book Now'" in txt and "value ='Join Lottery'" in txt

    def test_unknown_label_with_unchanged_status_is_written(self, cap):
        """新文字按可订放行时状态不变——签名里若只有状态，这次变化就会被吞掉，
        而「放行并告警」的那条告警恰恰需要这份 HTML 去核对。"""
        from scrapers.canvas import _record_capture
        book = _LOTTERY_UNITS.replace("Join Lottery", "Book Now")
        _record_capture("1113259", book)
        _record_capture("1113259", book.replace("Book Now", "Reserve Your Spot"))
        assert self._read(cap).count("完整响应") == 2

    def test_units_disappearing_is_written(self, cap):
        from scrapers.canvas import _record_capture
        _record_capture("1113259", _LOTTERY_UNITS)
        _record_capture("1113259", _EMPTY_UNITS)
        heads = [l for l in self._read(cap).splitlines() if l.startswith("=== ")]
        assert [("parsed=1" in h) for h in heads] == [True, False]

    def test_signatures_are_per_floorplan(self, cap):
        """三个 FP 返回同一个单元是常态——按 FP 分开记，否则只会留下第一个 FP 的。"""
        from scrapers.canvas import _record_capture
        for fp in ("1113259", "1112904", "1112905"):
            _record_capture(fp, _LOTTERY_UNITS)
        heads = [l for l in self._read(cap).splitlines() if l.startswith("=== ")]
        assert len(heads) == 3

    def test_failed_write_is_retried_next_round(self, cap, monkeypatch):
        """写失败时不能先记下签名，否则下一轮同样的响应会被当成已记过。"""
        import scrapers.canvas as oc
        real = oc._capture_path
        monkeypatch.setattr(oc, "_capture_path", lambda: (_ for _ in ()).throw(OSError("boom")))
        oc._record_capture("1113259", _LOTTERY_UNITS)
        monkeypatch.setattr(oc, "_capture_path", real)
        oc._record_capture("1113259", _LOTTERY_UNITS)
        assert "完整响应" in self._read(cap)

    def test_failure_never_breaks_scraping(self, cap, monkeypatch):
        """留档是排查辅助，绝不能因为它抓取失败。"""
        import scrapers.canvas as oc
        monkeypatch.setattr(oc, "_capture_path", lambda: (_ for _ in ()).throw(OSError("boom")))
        oc._record_capture("a", _EMPTY_UNITS)  # 不抛异常即通过

    def test_hooked_into_fetch(self, cap):
        """真实调用路径上确实会写——不是只有直接调 _record_capture 才写。"""
        session = MagicMock()
        resp = MagicMock(status_code=200, ok=True, text=_EMPTY_UNITS)
        resp.raise_for_status = lambda: None
        session.post.return_value = resp
        CanvasScraper()._fetch_units_html(
            session, base="https://x.test/onlineleasing", fp_id="1112904",
            property_id="186609", move_in_date="2026-09-01",
            floorplans_url="https://x.test/onlineleasing/s/floorplans.aspx",
        )
        assert "fp=1112904" in self._read(cap)


def test_canvas_shares_the_rentcafe_404_diagnosis():
    """Canvas 覆盖了 ``_fetch_units_html``（POST + floorPlans[]），但取文本
    仍然走基类的 ``_get_text``——404 的诊断因此是共享的。

    这条守卫存在的意义：如果哪天 Canvas 自己发请求了，那句「别往代理方向查」
    就会静默地只剩 OurDomain 有，而 RentCafe 的 404 恰恰最容易发生在标识改动上。
    """
    import inspect

    import scrapers.canvas as oc

    src = inspect.getsource(oc.CanvasScraper._fetch_units_html)
    assert "_get_text(" in src, (
        "Canvas 不再走共享的 _get_text，404 的诊断会只剩 OurDomain 有"
    )


# ── Canvas 改版：Contact 按钮 + 租约页费用（2026-11-20 真实样本）──────

_CONTACT_UNITS = (_FIX / "canvas_availableunits_contact.html").read_text(encoding="utf-8")
_RENTAL_OPTIONS = (_FIX / "canvas_rentaloptions.html").read_text(encoding="utf-8")


class TestCanvasContact:
    def test_contact_button_is_lottery(self):
        """31 套全是 Contact + Call for Details；租约页写明提交即进抽签。
        以前没见过的按钮文字会按可订放行，上线就是 31 条误报。"""
        units = _extract_units(_CONTACT_UNITS)
        assert len(units) == 1
        assert units[0]["status"] == "Available in lottery"
        assert units[0]["rent"] == "Call for Details"

    def test_rental_options_parsed(self):
        from scrapers.canvas import _parse_rental_options
        info = _parse_rental_options(_RENTAL_OPTIONS)
        assert info == {
            "rent": 823.02,
            # SC Other 144,98 + Electricity 53,98 + Heating 39,27 + Admin 6,25 + Water 10,00
            "charges": 254.48,
            "lottery": True,
            "available": "2026-11-30",
        }

    def test_rental_options_unknown_page_is_none(self):
        from scrapers.canvas import _parse_rental_options
        assert _parse_rental_options("<html>maintenance</html>") is None


class TestCanvasEnrichment:
    def _scrape(self, monkeypatch, rental_html):
        import scrapers.canvas as oc

        oc._RENTAL_CACHE.clear()
        calls = []

        def fake_base(self, **kw):
            units = {}
            for u in _extract_units(_CONTACT_UNITS):
                u["fp_ids"] = ["1112903"]
                units[u["unit_id"]] = u
            return units, True, {}

        def fake_get_text(session, url, **kw):
            calls.append(url)
            return rental_html

        monkeypatch.setattr(OurDomainScraper, "_scrape_once", fake_base)
        monkeypatch.setattr(oc, "_get_text", fake_get_text)
        monkeypatch.setattr(oc.req, "Session", MagicMock())
        monkeypatch.setattr("scrapers.ourdomain._confirm_empty_result", lambda *a: True)
        monkeypatch.setattr("scrapers.ourdomain.get_proxy_url", lambda *a, **k: "")
        r = CanvasScraper().scrape(ScrapeTask("canvas", "diemen", "Canvas Amsterdam Diemen"))
        return r, calls

    def test_listing_gets_all_in_price_and_breakdown(self, monkeypatch):
        r, calls = self._scrape(monkeypatch, _RENTAL_OPTIONS)
        (l,) = r.listings
        assert l.status == "Available in lottery"
        assert l.price_value == pytest.approx(1077.50)
        assert l.available_from == "2026-11-30"
        assert "Base rent: €823.02" in l.features
        assert "Service costs amount: €254.48" in l.features
        # 已是到手价，不能再标「另有服务费区间」
        assert not any(f.startswith("Service costs:") for f in l.features)
        assert l.rent_basis_note == ""
        (url,) = calls
        assert "stepname=RentalOptions" in url
        assert "myOlePropertyId=186607" in url and "FloorPlanID=1112903" in url

    def test_cached_per_unit(self, monkeypatch):
        """费用按 unit 缓存：第二轮不再为同一单元请求租约页。"""
        import scrapers.canvas as oc
        self._scrape(monkeypatch, _RENTAL_OPTIONS)
        calls2 = []
        monkeypatch.setattr(oc, "_get_text", lambda *a, **k: calls2.append(1) or "")
        r = CanvasScraper().scrape(ScrapeTask("canvas", "diemen", "Canvas Amsterdam Diemen"))
        assert calls2 == []
        assert r.listings[0].price_value == pytest.approx(1077.50)

    def test_failed_lookup_drops_placeholder_price(self, monkeypatch):
        """取不到费用时 'Call for Details' 不能当价格存下来。"""
        r, _ = self._scrape(monkeypatch, "<html>oops</html>")
        (l,) = r.listings
        assert l.price_raw is None
        assert l.status == "Available in lottery"  # 按钮本身已判为抽签
