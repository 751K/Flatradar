"""
scrapers/codomo_parse.py — Codomo（原 Holland2Stay）列表页 HTML 解析
=====================================================================

2026-11 起 www.holland2stay.com 整站 301 到 https://codomo.eu，旧的
GraphQL / 加密信封接口随之失效。新站是 Next.js 服务端渲染，**没有可用的
JSON 接口**（前端只调 ``/api/units/count`` 这类计数接口），房源只存在于
``/homes/{market}/{locale}?page=N`` 的 HTML 里，每页 25 条。

本模块只做「HTML → 结构化字段」，不发请求、不认识 Listing 之外的业务概念，
方便直接拿线上存档页做回归测试（tests/fixtures/codomo/）。

页面结构（2026-11-16 实测）
--------------------------
每套房是一个 ``<article>``，标题 ``<h3><a href="/homes/nl/en/{city}/{building}/{slug}">``。
卡片里依次是：

- 可选徽标（``Student only`` / ``Premium inventory`` / ``Parking included`` …）
- 规格 ``<ul><li>``：卧室数（``Studio`` / ``1 bedroom``）、面积、人数、装修、
  楼层、楼盘名
- ``<p><span>Nov 18, 2026</span><span>Available</span></p>``：入住日与状态
- ``Length of stay`` / ``Min 12 months``
- ``€1,522`` ``/month*``，以及 ``*Excl. €252/month for service costs …``
- 行动按钮：``Book directly``（直接预订）或 ``Show interest`` +
  ``42 people have shown interest in this home``（登记意向，相当于旧站的抽签）

**流式渲染**：部分卡片的规格、价格是 React Suspense 占位符
``<template id="P:N"></template>``，真正内容在文档末尾的
``<div hidden id="S:N">…</div>``，靠内联脚本 ``$RS("S:N","P:N")`` 换进去。
curl 拿到的是换之前的原文，所以这里要自己做一次替换，否则这些卡片会缺价格。
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Optional

# ── 极简 DOM ────────────────────────────────────────────────────────────

_VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})
#: 内容不参与文本提取的元素。svg 是图标，script/style 是代码。
_SKIP_TEXT = frozenset({"script", "style", "svg", "template", "noscript"})


@dataclass(eq=False)
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list["_Node | str"] = field(default_factory=list)
    parent: Optional["_Node"] = None

    def iter(self, tag: Optional[str] = None):
        for c in self.children:
            if isinstance(c, _Node):
                if tag is None or c.tag == tag:
                    yield c
                yield from c.iter(tag)

    def find(self, tag: str) -> Optional["_Node"]:
        return next(self.iter(tag), None)

    def text_tokens(self) -> list[str]:
        """按文本节点切分的可见文本，空白折叠、空串丢弃。"""
        out: list[str] = []

        def walk(n: "_Node") -> None:
            for c in n.children:
                if isinstance(c, str):
                    t = " ".join(c.split())
                    if t:
                        out.append(t)
                elif c.tag not in _SKIP_TEXT:
                    if c.tag == "sup":
                        # "49.49 m<sup>2</sup>" → 并进前一个 token，免得 "2" 单独成一项
                        sup = "".join(x for x in c.children if isinstance(x, str)).strip()
                        if out and sup == "2":
                            out[-1] = out[-1] + "²"
                            continue
                    walk(c)

        walk(self)
        return out

    def text(self) -> str:
        return " ".join(self.text_tokens())


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self._cur = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v or "") for k, v in attrs}, parent=self._cur)
        self._cur.children.append(node)
        if tag not in _VOID:
            self._cur = node

    def handle_startendtag(self, tag, attrs):
        node = _Node(tag, {k: (v or "") for k, v in attrs}, parent=self._cur)
        self._cur.children.append(node)

    def handle_endtag(self, tag):
        # 容错：向上找最近的同名元素再闭合；找不到就忽略这个结束标签
        n = self._cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self._cur = n.parent

    def handle_data(self, data):
        if data:
            self._cur.children.append(data)


def _parse_tree(html_text: str) -> _Node:
    b = _TreeBuilder()
    b.feed(html_text)
    b.close()
    return b.root


def _resolve_suspense(root: _Node) -> int:
    """把 ``<template id="P:N">`` 换成 ``<div hidden id="S:N">`` 的子节点。

    返回替换的个数。内容里可能还嵌着别的占位符，所以循环到不再有可换的为止。
    """
    segments: dict[str, _Node] = {}
    for div in root.iter("div"):
        sid = div.attrs.get("id", "")
        if sid.startswith("S:") and "hidden" in div.attrs:
            segments[sid[2:]] = div
    replaced = 0
    for _ in range(8):  # 嵌套深度上限，防御异常输入
        progress = False
        for tpl in list(root.iter("template")):
            tid = tpl.attrs.get("id", "")
            if not tid.startswith("P:"):
                continue
            seg = segments.get(tid[2:])
            parent = tpl.parent
            if seg is None or parent is None:
                continue
            idx = parent.children.index(tpl)
            moved = list(seg.children)
            for c in moved:
                if isinstance(c, _Node):
                    c.parent = parent
            parent.children[idx:idx + 1] = moved
            seg.children = []
            replaced += 1
            progress = True
        if not progress:
            break
    return replaced


# ── 卡片字段 ────────────────────────────────────────────────────────────

#: 登记意向卡片的按钮文字。出现它就是「意向」类房源（旧站的抽签）。
INTEREST_CTA = "Show interest"
DIRECT_CTA = "Book directly"

_UNIT_HREF = re.compile(r"^/homes/([a-z]{2})/[a-z]{2}/([a-z0-9-]+)/([a-z0-9-]+)/([a-z0-9-]+)/?$")
_MONEY = re.compile(r"€\s*([\d.,]+)")
_INTEREST_COUNT = re.compile(r"^(\d+) (?:people have|person has) shown interest")
_RESULTS = re.compile(r"\b(\d+) residences?\b")
_DATE_FMTS = ("%b %d, %Y", "%B %d, %Y")


def _money(s: str) -> Optional[int]:
    """"€1,522" → 1522。英文站千分位用逗号；带两位小数（"€1,522.50"）时舍去小数。"""
    m = _MONEY.search(s or "")
    if not m:
        return None
    num = re.sub(r"[.,]\d{1,2}$", "", m.group(1).rstrip(".,"))
    digits = re.sub(r"\D", "", num)
    return int(digits) if digits else None


def _iso_date(s: str) -> Optional[str]:
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


@dataclass
class CodomoCard:
    """一张房源卡片里能读到的全部字段。缺失的就是 None / 空。"""

    slug: str
    url_path: str
    country: str
    city_slug: str
    building_slug: str
    title: str
    status: str = ""
    available_from: Optional[str] = None
    min_stay: Optional[str] = None
    rent: Optional[int] = None
    service_costs: Optional[int] = None
    booking_type: str = ""          # "direct" | "interest" | ""
    interest_count: Optional[int] = None
    bedrooms: Optional[str] = None  # "Studio" / "1 bedroom" / "2 bedrooms"
    area: Optional[str] = None      # "49.49"
    persons: Optional[str] = None   # "2"
    finishing: Optional[str] = None
    floor: Optional[str] = None     # "4"
    building: Optional[str] = None
    badges: list[str] = field(default_factory=list)

    @property
    def address(self) -> str:
        """标题是 "G.H. Betzweg 207, Rotterdam"，去掉末尾城市。"""
        head, sep, _tail = self.title.rpartition(", ")
        return head if sep else self.title

    @property
    def city_name(self) -> str:
        _head, sep, tail = self.title.rpartition(", ")
        return tail if sep else ""

    @property
    def total_rent(self) -> Optional[int]:
        """含服务费的月租——与旧站 price_range 同口径，见 scrapers/holland2stay.py。"""
        if self.rent is None:
            return None
        return self.rent + (self.service_costs or 0)


_FINISHING_WORDS = ("furnished", "unfurnished", "upholstered", "shell")


def _classify_spec(tok: str, card: CodomoCard) -> bool:
    """把一个规格 token 填进对应字段。认得返回 True。"""
    t = tok.strip()
    low = t.lower()
    if low == "studio" or re.fullmatch(r"\d+ bedrooms?", low):
        card.bedrooms = t
        return True
    m = re.fullmatch(r"([\d.,]+)\s*m²?", t)
    if m:
        card.area = m.group(1).replace(",", ".")
        return True
    m = re.fullmatch(r"(\d+) persons?", low)
    if m:
        card.persons = m.group(1)
        return True
    m = re.fullmatch(r"(-?\d+)\. floor", low)
    if m:
        card.floor = m.group(1)
        return True
    if low in ("ground floor", "begane grond"):
        card.floor = "0"
        return True
    if any(w in low for w in _FINISHING_WORDS) and len(t) <= 30:
        card.finishing = t
        return True
    return False


def _parse_card(article: _Node) -> Optional[CodomoCard]:
    h3 = article.find("h3")
    a = h3.find("a") if h3 is not None else None
    href = a.attrs.get("href", "") if a is not None else ""
    m = _UNIT_HREF.match(href)
    if not m:
        return None
    country, city_slug, building_slug, slug = m.groups()
    card = CodomoCard(
        slug=slug,
        url_path=href,
        country=country,
        city_slug=city_slug,
        building_slug=building_slug,
        title=a.text(),
    )

    # 规格：卡片里有两份同样的规格 <ul>（窄屏那份折叠在 <details> 里），徽标
    # （Student only 等）是标题前另一个 <ul>。所有 <li> 合并去重后逐个归类，
    # 认不出的留给下面按位置分徽标 / 楼盘。
    spec_items: list[str] = []
    for ul in article.iter("ul"):
        for li in ul.iter("li"):
            t = li.text()
            if t and t not in spec_items:
                spec_items.append(t)
    unclassified: list[str] = []
    for tok in spec_items:
        if not _classify_spec(tok, card):
            unclassified.append(tok)

    # 卡片顶部（标题之前）的徽标，以及标题之后、规格里认不出的项
    tokens = article.text_tokens()
    try:
        title_idx = tokens.index(card.title)
    except ValueError:
        title_idx = 0
    pre = [t for t in tokens[:title_idx]]
    card.badges = list(dict.fromkeys(pre))
    # 规格列表里楼盘名排在最后；徽标与楼盘都是「认不出」的项，徽标已在 pre 里出现
    rest = [t for t in unclassified if t not in card.badges]
    if rest:
        card.building = rest[-1]
        extra = rest[:-1]
        card.badges.extend(x for x in extra if x not in card.badges)

    # 入住日 / 状态 / 最短租期
    try:
        li = tokens.index("Length of stay")
    except ValueError:
        li = -1
    if li >= 2:
        card.status = tokens[li - 1]
        card.available_from = _iso_date(tokens[li - 2])
        if card.available_from is None:
            # 没有日期时（"Available now" 之类）状态就紧挨着
            card.status = tokens[li - 1]
    if 0 <= li < len(tokens) - 1:
        card.min_stay = tokens[li + 1]

    # 价格：第一个 "€x" 后紧跟 "/month"；服务费在 "*Excl. €y/month …" 里
    for i, t in enumerate(tokens):
        if t.startswith("€") and i + 1 < len(tokens) and tokens[i + 1].startswith("/month"):
            card.rent = _money(t)
            break
    for t in tokens:
        if t.startswith("*Excl.") or t.lower().startswith("*excl"):
            card.service_costs = _money(t)
            break

    # 预订方式
    if INTEREST_CTA in tokens:
        card.booking_type = "interest"
        for t in tokens:
            mm = _INTEREST_COUNT.match(t)
            if mm:
                card.interest_count = int(mm.group(1))
                break
    elif DIRECT_CTA in tokens:
        card.booking_type = "direct"
    return card


@dataclass
class CodomoPage:
    cards: list[CodomoCard]
    total: Optional[int]        # "142 residences"；读不到为 None
    max_page: Optional[int]     # 分页导航里最大的页码；没有分页为 1
    unresolved_placeholders: int


def parse_listing_page(html_text: str) -> CodomoPage:
    """解析一页 ``/homes/{market}/{locale}?page=N``。"""
    root = _parse_tree(html_text)
    _resolve_suspense(root)

    cards: list[CodomoCard] = []
    seen: set[str] = set()
    for art in root.iter("article"):
        card = _parse_card(art)
        if card is None or card.slug in seen:
            continue
        seen.add(card.slug)
        cards.append(card)

    total: Optional[int] = None
    m = _RESULTS.search(_html.unescape(re.sub(r"<[^>]+>", " ", html_text)))
    if m:
        total = int(m.group(1))

    pages = [1]
    for a in root.iter("a"):
        mm = re.search(r"[?&]page=(\d+)", a.attrs.get("href", ""))
        if mm:
            pages.append(int(mm.group(1)))
    unresolved = sum(
        1 for t in root.iter("template") if t.attrs.get("id", "").startswith("P:")
    )
    return CodomoPage(cards=cards, total=total, max_page=max(pages), unresolved_placeholders=unresolved)
