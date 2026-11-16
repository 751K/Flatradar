"""
bookers/base.py — 自动下单层抽象（source-agnostic）
====================================================

每个 source 的下单实现一个 ``AbstractBooker`` 子类，由 ``bookers/__init__.py``
里的 ``BOOKER_REGISTRY`` 路由。

为什么独立成包（而不是寄居在 ``scrapers/`` 里）
------------------------------------------------
- 概念分离：scrape = 只读，book = 写操作 + 用户身份 + 支付 + 法律责任，
  混在一个类里会让 single-responsibility 失守
- 部分 source 只读不能下单（OurDomain 当前；HousingAnywhere 未来）；
  让 scraper 强行实现一个 no-op booker 反而模糊语义
- 单独包让"下单能力矩阵"在代码里一目了然：grep ``bookers/`` 看到哪些 source
  支持自动下单

约定
----
- 所有 booker 返回同一个 ``BookingResult``——``notifier.py`` / ``mcore.booking_round``
  只读 ``result.pay_url`` / ``result.phase``，不关心是哪个平台
- 不支持下单的 source 走 ``phase="unsupported"`` 分支，``book_with_fallback``
  会过滤掉这些候选，不会产生误报"失败"通知
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from models import Listing
    from users import UserConfig


# ── 跨平台共用的结果类型 ────────────────────────────────────────────

BookingPhase = Literal[
    "", "dry_run", "success", "race_lost",
    "reserved_conflict", "unknown_error",
    "blocked",
    "unsupported",
    # 半自动预订：申请已起草并存在用户账号下，但**没有占住房**——
    # 还差用户自己上传证件和付款。与 "success" 必须分开：把它当成功报给
    # 用户，用户会以为房到手了、慢悠悠去传证件，结果被别人抢走。
    "draft_saved",
    # 前置校验没过（缺凭据 / 档案不完整），压根没触网。
    "not_configured",
    # 凭据被平台拒了。和 not_configured 分开：那个是"没填"，这个是"填了但不对"，
    # 用户看到的提示不一样；也和 blocked 分开——重试和换 IP 都救不回来。
    "auth_failed",
    # 凭据对，但账号开了两步验证，无法全自动（需人工输验证码）。和 auth_failed
    # 分开：不是账号密码的问题，提示用户去关 2FA 或改半自动。
    "auth_2fa",
    # 走到了最后一步但服务端拒绝保存。**绝不能报成 draft_saved**——那条消息
    # 让用户以为表单已填好、安心去传证件，而实际上什么都没存下。
    "save_rejected",
    # 变更请求已经发出，但没拿到确定结果（断线 / 超时 / 异常）。不能当失败去换
    # 下一套备选，见 ``mcore.booking.book_with_fallback``。
    "uncertain",
]


@dataclass
class BookingResult:
    listing: Listing
    success: bool
    message: str
    dry_run: bool = False
    pay_url: str = ""
    contract_start_date: str = ""
    phase: BookingPhase = ""


@dataclass
class BookingRequest:
    """
    跨 source 的自动下单请求。

    Fields
    ------
    listing       目标房源。``listing.source`` 决定路由到哪个 Booker
    user          用户配置（凭据、自动下单偏好）。Booker 自取 ``user.auto_book.*``
                  里它需要的字段；不同 source 用不同子集（Plaza 用
                  plaza_username/plaza_password，Xior 用按楼栋的 xior_accounts）
    dry_run       仅完成认证 / 资格检查，不真正提交订单
    """
    listing: "Listing"
    user: "UserConfig"
    dry_run: bool = False


class AbstractBooker(ABC):
    """
    每个支持自动下单的 source 实现一个子类，注册到 ``BOOKER_REGISTRY``。

    子类约定
    --------
    - 必须设 ``source: str`` 类属性（与对应 scraper 的 ``source`` + ``listing.source`` 一致）
    - 必须实现 ``book(request) -> BookingResult`` 同步方法
    - 失败时 **不抛异常**，返回 ``BookingResult(success=False, phase="...")``
      （由 ``book_with_fallback`` 决定要不要重试 / 通知）
    - ``BookingResult.phase`` 必须是 ``BookingPhase`` Literal 中的合法值

    线程模型
    --------
    book() 在 monitor 的 executor 线程里跑。Booker 实例可能被多个线程并发
    使用——内部状态自行加锁或每次新建（现有 booker 都是每次新建 Session）。
    """

    # 子类必须覆盖。例：``source = "plaza"``
    source: str = ""

    @abstractmethod
    def book(self, request: BookingRequest) -> BookingResult:
        """
        对单个 listing 执行下单。

        Returns
        -------
        BookingResult，含 success / phase / message / pay_url 等字段。
        ``phase`` 是给上层 (``book_with_fallback``) 用的判别标签——
        ``"race_lost"`` 时上层会换备选房源继续尝试；其他 phase 立即返回。
        """
