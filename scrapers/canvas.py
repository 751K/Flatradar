"""
scrapers/canvas.py — Canvas（原 OurCampus）scraper
==========================================

2026-11 OurCampus 并入 Greystar 的 Canvas 品牌，平台 key 由 ``ourcampus``
改为 ``canvas``（迁移见 mstorage/_canvas_rename.py，侦察见 docs/CANVAS.md）。
下文 2026-11 之前的观察都是在 OurCampus 门户上做的。

Canvas 是 Greystar 的学生住房品牌，与 OurDomain 同属一家、**同一套
后端**（Webflow 前台 + RentCafe/SecureRC 后台）。因此本 scraper 直接继承
``OurDomainScraper``，复用它的全部机制：

- TLS 指纹池 + 冷却状态机（``_FINGERPRINT_STATE`` 是模块级的，两个 source 共享
  ——它们打的是同一个 SecureRC 集群，某个指纹被烧对两边同时生效）
- 每次尝试换出口 IP（``rotating=True``）
- 同 session 内 403 重试
- floorplans.aspx 发现 + 单元行解析（``data-selenium-id`` / ``data-label`` 双策略）
- 状态映射、Occupancy 反推、Listing 映射

唯一需要覆盖的是**取单元表的请求形状**，见 ``_fetch_units_html``。

规模与预期
----------
只有一栋楼（Amsterdam Diemen，Dalsteindreef 6002），三个房型。官网自述等待期
16–18 个月。**但先到先得和抽签并存**：2026-08-10 至 09-02 挂出的 6 套都是
``Book Now``，09-15 挂出的是 ``Join Lottery``——同一个页面、同一种按钮，只差
按钮上的字。所以状态要看按钮文字，见 ``scrapers/ourdomain._extract_status``。
接入是**产品决策**，不是因为投入产出比划算——文档里对它的评估见
``docs/SCRAPING_RECON.md`` 的 Canvas 历史资料。

验证状态
--------
接入时该楼**零可订单元**，因此单元表的 HTML 结构当时没有真实样本可核对。
当时判断「复用 OurDomain 的解析器」成立的依据是：两边的**空响应结构指纹完全
一致**（``Apartment Search Result`` 面板 + ``innerformdiv`` + ``myOlePropertyId``，
都无 unitrow），说明是同一套 RentCafe 模板。

真实样本已于 2026-08-10 首次出现，并在 2026-08-27 用于核对解析器。核对结果是
模板判断成立，但**状态映射不成立**：feed 并不只列出可订单元，且置灰的日期单元
格表示「自该日起可订」而非「已出租」。该错误曾使整批可订单元被判为 Occupied
且不发任何通知，修正见 CHANGELOG v1.26.0——判据现依据该行是否带有可用的下单
按钮，未识别的样式类放行并告警。

2026-09-15 又证伪了一条：「有按钮」不等于「能直接订」。按钮文字为
``Join Lottery`` 的单元被判成了 Available to book 并按可订推送。判据补上按钮
文字：写着抽签即为 ``Available in lottery``，没见过的文字放行并告警。

结构真的不同时，基类的完整性守卫会兜住：解析不出单元且响应又不像单元面板时
标记 incomplete，而不是误报「没有房」。但如果结构不同却仍是合法面板，仍会静默
返回 0 个单元，所以留档不能停——``data/canvas_capture.txt`` 在响应内容
**发生变化时**记一行摘要，并在**有单元或疑似解析失配时**附完整 HTML。
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import curl_cffi.requests as req

from .base import ScrapeTask
from .ourdomain import (
    OurDomainScraper,
    _get_text,
    _headers_for,
    _looks_like_availability_panel,
)
from models import parse_float

from .rentcafe_parse import _CONTROL_LABEL_RE, _extract_units, _normalize_date

logger = logging.getLogger(__name__)


# ── 抓取留档 ────────────────────────────────────────────────────────
#
# 最初存在的理由是单元表 HTML 没有真实样本（见模块文档）。样本早已有了，它仍然
# 两次抓到判据错误（08-27 muted、09-15 Join Lottery）——只看日志里的「共抓取 N
# 个单元」看不出按钮上写的是什么，也看不出「结构变了但仍是合法面板」。
#
# 所以**只在有看头的时候**才附完整 HTML：
#   - 解析出单元了 → 真实样本，必须留
#   - 响应里有 unitrow 痕迹但解析出 0 个 → 正是解析器对不上的信号，更要留
#
# **只记变化，不记重复**（2026-09-15）。原来每次请求都写摘要、有单元就每轮附
# 35 KB HTML：摘要行 36 天攒了 5 MB，一套房挂着的十几分钟里又每轮三份 HTML，
# 09:07 撞上 8 MB 上限后停写——13:53 那批五套房一个字节都没留下，而那恰恰是
# 需要核对「按钮上写的是 Book Now 还是 Join Lottery」的时候。
#
# 现在按 floorplan 记住上一次写下的内容签名（单元、状态、租金、按钮文字、
# unitrow 痕迹），签名没变就整条不写。满了轮转一代（``.1``），不再停写——
# 停写意味着证据恰好在最需要的时候断掉。
_CAPTURE_PATH_ENV = "CANVAS_CAPTURE_PATH"
_CAPTURE_MAX_BYTES = 8 * 1024 * 1024
_UNITROW_HINT = re.compile(r'id=["\']unitrow_(\d+)', re.IGNORECASE)
_LAST_SIGNATURE: dict[str, tuple] = {}


def _capture_signature(html: str, units: list[dict], panel: bool) -> tuple:
    return (
        panel,
        tuple(sorted(set(_UNITROW_HINT.findall(html or "")))),
        tuple(sorted((u["unit_id"], u["status"], u["rent"]) for u in units)),
        tuple(sorted({m.strip() for m in _CONTROL_LABEL_RE.findall(html or "")})),
    )


def _capture_path() -> Path:
    override = os.environ.get(_CAPTURE_PATH_ENV, "").strip()
    if override:
        return Path(override)
    from config import DATA_DIR  # 延迟导入，避免 config ↔ scrapers 循环
    return DATA_DIR / "canvas_capture.txt"


def _record_capture(fp_id: str, html: str) -> None:
    """把一次 availableunits 响应记进留档文件。任何异常都不许影响抓取。"""
    try:
        units = _extract_units(html)
        parsed = len(units)
        has_rows = bool(_UNITROW_HINT.search(html or ""))
        panel = _looks_like_availability_panel(html)
        interesting = parsed > 0 or has_rows

        signature = _capture_signature(html, units, panel)
        if _LAST_SIGNATURE.get(fp_id) == signature:
            return

        path = _capture_path()
        if path.exists() and path.stat().st_size > _CAPTURE_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))

        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        head = (
            f"=== {stamp}  fp={fp_id}  bytes={len(html or '')}  "
            f"panel={'yes' if panel else 'NO'}  unitrow={'yes' if has_rows else 'no'}  "
            f"parsed={parsed} ===\n"
        )
        body = ""
        if interesting:
            body = (
                "--- 完整响应（单元有变化 / 解析器可能对不上，留作核对）---\n"
                + (html or "") + "\n--- 响应结束 ---\n"
            )
            if parsed == 0:
                logger.warning(
                    "Canvas 响应含 unitrow 但解析出 0 个单元——解析器可能与"
                    "该主题不匹配，原始 HTML 已留档到 %s", path,
                )
            else:
                logger.info(
                    "Canvas 单元有变化（%d 个），原始 HTML 已留档到 %s",
                    parsed, path,
                )

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(head + body)
        _LAST_SIGNATURE[fp_id] = signature  # 写成功才记，写失败下一轮重试
    except Exception:
        logger.debug("Canvas 抓取留档失败（已忽略）", exc_info=True)


# ── 单元租约页：实际费用 + 抽签说明 ────────────────────────────────
#
# Canvas 改版后单元表的租金列一律写 ``Call for Details``，按钮是 ``Contact``
# （2026-11-20，31 套无一例外）。实际费用只在下一步的租约页上：
#
#     oleapplication.aspx?stepname=RentalOptions&myOlePropertyId=…&FloorPlanID=…&UnitID=…
#
# 页面右侧 ``LeaseInfoSummary`` 表逐项列出 Rent、SC Other、SC Electricity、
# SC Heating、Administration fee、Water，加起来就是每月到手价。同一页写着
# “submitting this application enters you into the lottery for this unit”，
# 这是 ``Contact`` 实为抽签的直接证据。
#
# 每个单元一次请求。费用在一个单元挂出期间基本不变，所以按 unit 缓存
# ``_RENTAL_TTL_SEC``，常态下每轮只为新出现的单元发请求。
_RENTAL_TTL_SEC = 12 * 3600
_RENTAL_MAX_PER_ROUND = 60
_RENTAL_CACHE: dict[str, tuple[float, dict]] = {}
_LEASE_SUMMARY_RE = re.compile(
    r"id=[\"']LeaseInfoSummary[\"'](.*?)</table>", re.IGNORECASE | re.DOTALL,
)
_LEASE_ROW_RE = re.compile(
    r"<tr>\s*<td>\s*([^<]+?)\s*</td>\s*<td>(.*?)</td>\s*</tr>",
    re.IGNORECASE | re.DOTALL,
)
_NON_MONEY_ROWS = ("move-in date", "lease term")
_LOTTERY_NOTICE = "into the lottery"
_AVAILABILITY_RE = re.compile(
    r"data-selenium-id\s*=\s*[\"']Availability[\"']\s*>\s*(\d{1,2}-\d{1,2}-\d{4})",
    re.IGNORECASE,
)


def _parse_rental_options(html: str) -> Optional[dict]:
    """从租约页取 ``{"rent", "charges", "lottery", "available"}``；结构对不上返回 None。

    ``rent`` 是基础租金，``charges`` 是其余各项之和（服务费、水电暖、管理费）；
    ``available`` 是可入住日期（ISO），Contact 按钮不像 ApplyNowClick 那样带日期，
    只能从这里拿。
    """
    m = _LEASE_SUMMARY_RE.search(html or "")
    if not m:
        return None
    rent: Optional[float] = None
    charges = 0.0
    for label, cell in _LEASE_ROW_RE.findall(m.group(1)):
        key = " ".join(label.split()).lower()
        if key in _NON_MONEY_ROWS:
            continue
        value = parse_float(re.sub(r"<[^>]+>", " ", cell))
        if value is None:
            continue
        if key == "rent":
            rent = value
        else:
            charges += value
    if rent is None:
        return None
    avail = _AVAILABILITY_RE.search(html)
    return {
        "rent": rent,
        "charges": round(charges, 2),
        "lottery": _LOTTERY_NOTICE in (html or "").lower(),
        "available": _normalize_date(avail.group(1)) if avail else None,
    }


def _has_amount(text: str) -> bool:
    return bool(re.search(r"\d", text or ""))


class CanvasScraper(OurDomainScraper):
    """Unit-level scraper for Canvas (Greystar / RentCafe)."""

    source = "canvas"

    # 独立前缀：Canvas（property 186607）与 OurDomain（184283 / 182801）
    # 是不同的 RentCafe property，unit id 跨 property 是否唯一没有保证。
    ID_PREFIX = "oc_"

    # 2026-11 Canvas 并入 Greystar 的 Canvas 品牌（canvas-world.com），
    # RentCafe 门户随之换了 host、slug 和 property ID（旧 property 186609，
    # 旧 host 已 403）。floorplan ID 每轮从 floorplans.aspx 发现，不写死。
    # 侦察记录见 docs/CANVAS.md。
    BASE = (
        "https://canvas-student-amsterdam-diemen-3p-rentcafewebsiteuk"
        ".securerc.co.uk/onlineleasing"
    )

    BUILDINGS: dict[str, dict[str, str]] = {
        "diemen": {
            "slug": "shell-ourcampus-amsterdam-diemen-3p",
            # 显示名随品牌改为 Canvas（2026-11）。库里旧房源的 city / name、
            # 订阅串和用户筛选里的旧名由 mstorage/_canvas_rename.py 一并改写。
            "display": "Canvas Amsterdam Diemen",
            "short_display": "Canvas Diemen",
            "property_id": "186607",
            "type": "Studio",
            # 建筑级真实街道地址，供 geocode 用（unit 名不可 geocode）。
            # 与 OurDomain South-East（Dalsteindreef 20-40）同街，是隔壁楼。
            "street_address": "Dalsteindreef 6002, 1112 XC Diemen",
            # 纯学生盘，整栋一个值，不按面积切。ourcampus.nl/en/criteria：
            # 「Enrolled at an MBO, HBO, or university-level institution
            # (only universities located in the Netherlands)」+ 在校证明，
            # PhD / 博后明确不符合，且无收入要求。页脚写着「Young
            # professionals book at: [OurDomain]」——它把有收入的人劝去隔壁。
            "tenant_policy": {"default": "student only"},
        },
    }

    def _fetch_units_html(
        self,
        session: req.Session,
        *,
        base: str,
        fp_id: str,
        property_id: str,
        move_in_date: str,
        floorplans_url: str,
    ) -> str:
        """POST + ``floorPlans[]`` 表单体，照抄它自己前端的调用。

        Canvas 的 floorplans.aspx 里是这么发的::

            $('#FloorPlanContainer').parent().load(
                '/onlineleasing/rcLoadContent.ashx?contentclass=availableunits&t=' + Math.random(),
                {'floorPlans': names})

        jQuery 的 ``.load(url, obj)`` 在 data 是对象时走 **POST**，数组序列化成
        ``floorPlans[]``；``MoveInDate`` / ``myolePropertyID`` 都不传，由会话隐含。

        为什么不沿用基类的 GET：接入时该楼零可订，GET 和 POST 都只能拿到空面板，
        **无法用响应区分两者对错**。所以选了「和它自己前端一致」这条——那是唯一
        有证据支持的形状。基类的 GET 形状对 OurDomain 是实测有效的，不动。

        另注：OurDomain 的 host 上 POST 会触发 403，Canvas 的 host 不会
        （实测 HTTP 200）。同一套 RentCafe，WAF 策略按 host 配。
        """
        url = f"{base}/rcLoadContent.ashx?contentclass=availableunits"
        html = _get_text(
            session, url,
            headers=_headers_for(url, referer=floorplans_url, ajax=True),
            data=[("floorPlans[]", str(fp_id))],
        )
        _record_capture(str(fp_id), html)
        return html

    def _scrape_once(self, **kwargs):
        """在基类结果上补实际费用：单元表不再给租金时去租约页取。"""
        all_units, complete, fp_names_by_id = super()._scrape_once(**kwargs)
        missing = [u for u in all_units.values() if not _has_amount(u.get("rent", ""))]
        if missing:
            self._fill_rental_options(missing, **kwargs)
        return all_units, complete, fp_names_by_id

    def _fill_rental_options(
        self,
        units: list[dict],
        *,
        display: str,
        base: str,
        floorplans_url: str,
        property_id: str,
        proxies: dict[str, str],
        impersonate: str,
        **_ignored,
    ) -> None:
        now = time.monotonic()
        fetched = failed = 0
        slug_url = floorplans_url.rsplit("/", 1)[0]
        with req.Session(impersonate=impersonate, proxies=proxies) as session:
            for unit in units:
                uid = unit["unit_id"]
                cached = _RENTAL_CACHE.get(uid)
                info = cached[1] if cached and now - cached[0] < _RENTAL_TTL_SEC else None
                if info is None and unit.get("fp_ids") and fetched < _RENTAL_MAX_PER_ROUND:
                    url = (
                        f"{slug_url}/oleapplication.aspx?stepname=RentalOptions"
                        f"&myOlePropertyId={property_id}"
                        f"&FloorPlanID={unit['fp_ids'][0]}&UnitID={uid}&header=1"
                    )
                    fetched += 1
                    try:
                        info = _parse_rental_options(_get_text(
                            session, url, headers=_headers_for(url, referer=floorplans_url),
                        ))
                    except Exception as e:  # 取不到费用不影响单元本身
                        logger.debug("[%s] 租约页失败 unit=%s: %s", display, uid, e)
                        info = None
                    if info is None:
                        failed += 1
                    else:
                        _RENTAL_CACHE[uid] = (now, info)
                if info is None:
                    # "Call for Details" 之类不是价格，留着只会让展示层显示一串字
                    unit["rent"] = ""
                    continue
                total = info["rent"] + info["charges"]
                unit["rent"] = f"€{total:.2f}"
                unit["base_rent"] = f"€{info['rent']:.2f}"
                unit["service_charges"] = f"€{info['charges']:.2f}"
                if info.get("available") and not unit.get("avail_date"):
                    unit["avail_date"] = info["available"]
                if info["lottery"]:
                    unit["status"] = "Available in lottery"
                elif unit.get("status") == "Available in lottery":
                    logger.warning(
                        "[%s] Canvas 单元 %s 按钮判为抽签，但租约页没有抽签说明",
                        display, uid,
                    )
        if fetched:
            logger.info(
                "[%s] Canvas 租约页取费用 %d 个（失败 %d）", display, fetched, failed,
            )

    def _building_for_task(self, task: ScrapeTask) -> dict[str, str]:
        # 基类的报错文案写死了 "OurDomain"，这里换成本 source 的名字，
        # 免得配置写错时把人指向另一个平台的文档。
        key = (task.city_key or "").strip().lower()
        if key not in self.BUILDINGS and not (
            task.extra.get("slug") and task.extra.get("property_id")
        ):
            raise ValueError(
                f"Unknown Canvas city_key={task.city_key!r}；"
                f"已知 key: {sorted(self.BUILDINGS)}，"
                "或提供 extra.slug + extra.property_id"
            )
        return super()._building_for_task(task)
