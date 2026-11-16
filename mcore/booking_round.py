"""自动预订的提交与结果处理。

从 monitor.py 拆出。提交仍走 ``run_in_executor``，结果仍按用户逐个隔离；
依赖的重试队列与通知节流都从共享运行时状态取。日志通道仍用 ``monitor``。
"""
from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace
from dataclasses import dataclass
from typing import NamedTuple

from models import BOOKING_HOLD_SOURCES, Listing, is_application_only
from notifier import BaseNotifier, WebNotifier
from storage import Storage
from users import UserConfig

from mcore.booking import area_key, book_with_fallback
from mcore.booking_guard import attempt_context, process_alive
from mcore.plaza_guard import PlazaGuard
from mcore.booking_candidates import listing_source
from mcore.notify_round import push_booking_failed
from mcore.state import runtime
from mcore.throttles import should_notify_block

logger = logging.getLogger("monitor")

# 类型别名：每个用户与其对应通知器的配对列表。
UserNotifiers = list[tuple[UserConfig, BaseNotifier]]

#: 单个用户预订结果的墙钟上限。见 ``process_booking_results``。
#:
#: ``booking_deadline`` 只决定「还要不要再试下一套备选」，它管不了一次调用
#: 本身卡死：DNS 黑洞、TCP 无响应、CF 挑战页不返回，线程会一直停在那里。
#: 而结果处理是**串行** await 的——一个用户卡住，排在他后面的所有用户都不再
#: 处理，run_once 也不返回；更糟的是默认线程池是抓取、预订共用的，
#: 卡死线程攒几个就能把整个监控拖停。
#:
#: 与 ``SOURCE_DISPATCH_TIMEOUT_SEC`` 同一个量级：默认 600 秒足够覆盖「登录 +
#: 多套备选」的正常耗时，只在病理情况下才会触发。超时**杀不掉底层线程**
#: （executor 的 future 取消不了已在运行的调用），这里保证的是本轮继续走完，
#: Plaza 保留应征级保护并提示用户核实，不把超时当成预订失败。
_BOOKING_RESULT_TIMEOUT_SEC = float(
    os.environ.get("BOOKING_RESULT_TIMEOUT_SEC") or "600")


def _observe_late_booking(future):
    """Consume a late worker exception after the timed-out waiter has moved on."""
    try:
        future.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("超时后的预订任务最终异常退出")


def _plaza_uncertain_text(user, listing_name):
    if getattr(user, "language", "zh") == "en":
        title = "Check your Plaza application"
        body = (f"The application for {listing_name} may have been submitted. "
                "Sign in to Plaza to check your applications, then confirm the review in user "
                "settings. Automatic applications for this listing are paused until then.")
    else:
        title = "Plaza 应征结果需要核实"
        body = (f"{listing_name} 的应征结果不确定，可能已经提交。请登录 Plaza "
                "检查我的应征，并在用户设置中确认核实；确认前不会自动重复应征该房源。")
    return title, body


async def _notify_plaza_uncertain(storage, push, notifier, user, listing, *, listing_name=None, save_web=True):
    title, body = _plaza_uncertain_text(user, listing_name or listing.name)
    if save_web:
        try:
            storage.add_web_notification(type="booking", title=title, body=body,
                                         user_id=user.id, url=f"/users/{user.id}")
        except Exception:
            logger.exception("[%s] 保存 Plaza 核实通知失败", user.name)
    try:
        await notifier.send_booking_uncertain(title, body)
    except Exception:
        logger.exception("[%s] 发送 Plaza 核实通知失败", user.name)
    try:
        await push.dispatch_announcement_to_user(storage, user.id, title, body)
    except Exception:
        logger.exception("[%s] 推送 Plaza 核实通知失败", user.name)


async def _alert_plaza_attempt(storage, push, notifier, user, listing, guard, attempt):
    try:
        row = guard.get_attempt(attempt)
        if row and row['timed_out']:
            return
        listing_name = row['listing'] if row else listing.name
        title, body = _plaza_uncertain_text(user, listing_name)
        if row:
            if guard.claim_notice(attempt, title=title, body=body, user_id=user.id):
                await _notify_plaza_uncertain(storage, push, notifier, user, listing,
                                              listing_name=listing_name, save_web=False)
            return
    except Exception:
        logger.exception("[%s] 无法持久化 Plaza 核实通知", user.name)
    # The durable alert may be unavailable; still attempt the independent channels.
    await _notify_plaza_uncertain(storage, push, notifier, user, listing)


def notify_interrupted_plaza(storage, user_notifiers, push):
    """Warn owners after a restart even if the listing is no longer available."""
    owners = {user.id: (user, notifier) for user, notifier in user_notifiers}
    try:
        guard = PlazaGuard(storage)
        rows = guard.get_unnotified()
    except Exception:
        logger.exception("无法读取 Plaza 待核实应征")
        return
    loop = asyncio.get_running_loop()
    for row in rows:
        pair = owners.get(row['user_id'])
        if not pair or (row['state'] == 'running' and process_alive(row['pid'])):
            continue
        try:
            user, notifier = pair
            title, body = _plaza_uncertain_text(user, row['listing'])
            if guard.claim_notice(row['attempt'], title=title, body=body, user_id=user.id):
                loop.create_task(_notify_plaza_uncertain(
                    storage, push, notifier, user, SimpleNamespace(name=row['listing']), save_web=False))
        except Exception:
            logger.exception("无法恢复 Plaza 待核实通知")


class BookingJob(NamedTuple):
    """A submitted booking and the state required to process its result."""
    user: UserConfig
    notifier: BaseNotifier
    candidates: list[Listing]
    future: asyncio.Future


@dataclass
class BookingSubmissionContext:
    loop: asyncio.AbstractEventLoop
    status_transition: dict[str, tuple[str, str]]
    booking_deadline: float
    storage: Storage | None
    submitted: list[BookingJob]


def _submit_plaza_bookings(user, notifier, plaza_candidates, context: BookingSubmissionContext) -> None:
    """Submit each Plaza application independently, retaining durable guards."""
    from settings_store import context_job
    loop, storage = context.loop, context.storage
    status_transition, booking_deadline = context.status_transition, context.booking_deadline
    ab_futures = context.submitted
    for plaza_listing in plaza_candidates:
        if plaza_listing.id in status_transition:
            old_s, new_s = status_transition[plaza_listing.id]
            logger.info(
                "[%s] 🚀 Plaza 快速注册 (%s → %s)，提交到 executor: %s",
                user.name, old_s, new_s, plaza_listing.name,
            )
        else:
            logger.info(
                "[%s] 🚀 Plaza 自动注册（全部符合条件的房源），提交到 executor: %s",
                user.name, plaza_listing.name,
            )
        plaza_guard = plaza_attempt = None
        if not user.auto_book.dry_run:
            try:
                plaza_guard = PlazaGuard(storage)
                plaza_attempt = plaza_guard.acquire(user.auto_book.plaza_username, user.id,
                                                    plaza_listing.id, plaza_listing.name)
            except Exception:
                logger.exception("[%s] 无法保存 Plaza 应征保护，已跳过该房源", user.name)
            if not plaza_attempt:
                if plaza_guard:
                    try:
                        old = plaza_guard.get_listing(user.auto_book.plaza_username, plaza_listing.id)
                        if (old and old["user_id"] == user.id and not old["timed_out"]
                                and (old["state"] == "finished" or not process_alive(old["pid"]))):
                            title, body = _plaza_uncertain_text(user, old["listing"])
                            if plaza_guard.claim_notice(old["attempt"], title=title, body=body,
                                                        user_id=user.id):
                                from mcore import push as push_module
                                loop.create_task(_notify_plaza_uncertain(
                                    storage, push_module, notifier, user, plaza_listing,
                                    listing_name=old["listing"], save_web=False))
                    except Exception:
                        logger.exception("[%s] 恢复 Plaza 核实通知失败", user.name)
                logger.warning("[%s] Plaza 应征待核实或保护不可用，跳过 %s", user.name, plaza_listing.name)
                continue

        def run_plaza(c=plaza_listing, u=user, g=plaza_guard, a=plaza_attempt):
            if g is None:
                return book_with_fallback([c], u, booking_deadline)
            result = None
            try:
                with attempt_context(g, a):
                    result = book_with_fallback([c], u, booking_deadline)
                return result
            finally:
                try:
                    g.update(a, state="finished", phase=getattr(result, "phase", "exception") or "exception")
                except Exception:
                    logger.exception("[%s] 无法保存 Plaza 应征状态，保护记录保留", u.name)

        try:
            f = loop.run_in_executor(None, context_job(run_plaza))
        except Exception:
            if plaza_guard:
                plaza_guard.update(plaza_attempt, state="finished")
                plaza_guard.clear_completed(plaza_attempt)
            raise
        if plaza_guard:
            f.plaza_guard = (plaza_guard, plaza_attempt)
        ab_futures.append(BookingJob(user, notifier, [plaza_listing], f))



def _submit_standard_bookings(user, notifier, candidates, context: BookingSubmissionContext) -> None:
    """把 Plaza 之外的候选按「主候选 + race_lost 回退」整体提交一次。

    目前 ``AUTO_BOOK_SOURCES`` 里只有 plaza，这条路径在线上不会被走到；保留它是
    因为 Xior / OurDomain / OurCampus 的 booker 已注册，开放其中任何一个时不必
    再补提交逻辑。它们的成功结果止于存草稿 / 开始申请，不需要 Plaza 那种持久化
    应征保护。
    """
    from settings_store import context_job
    loop = context.loop
    status_transition, booking_deadline = context.status_transition, context.booking_deadline
    sequential_candidates = sorted(
        [c for c in candidates if listing_source(c) != "plaza"],
        key=area_key,
        reverse=True,
    )
    if not sequential_candidates:
        return

    primary = sequential_candidates[0]
    if len(sequential_candidates) > 1:
        logger.info(
            "[%s] 自动预订候选 %d 套（含 %d 套备选），优先面积最大: %s (%.1f m²)",
            user.name, len(sequential_candidates), len(sequential_candidates) - 1,
            primary.name, area_key(primary),
        )
    if primary.id in status_transition:
        old_s, new_s = status_transition[primary.id]
        logger.info(
            "[%s] 🚀 快速预订通道 (%s → %s)，立即提交到 executor: %s",
            user.name, old_s, new_s, primary.name,
        )
    else:
        logger.info(
            "[%s] 🚀 自动预订（新上线可预订），立即提交到 executor: %s",
            user.name, primary.name,
        )

    def run_booking(cs=sequential_candidates, u=user):
        return book_with_fallback(cs, u, booking_deadline)

    f = loop.run_in_executor(None, context_job(run_booking))
    context.submitted.append(BookingJob(user, notifier, sequential_candidates, f))


def submit_bookings(
    loop: asyncio.AbstractEventLoop,
    ab_candidates: dict[str, list["Listing"]],
    user_notifiers: "UserNotifiers",
    status_transition: dict[str, tuple[str, str]],
    booking_deadline: float,
    storage: "Storage | None" = None,
    *, submitted: list[BookingJob] | None = None,
) -> list[BookingJob]:
    """把每个用户的候选立即提交线程池（快速下单通道）。

    Plaza 每个候选都单独提交一次，因为 Plaza 的成功是「应征已提交」，不代表
    房源已被抢走；其他平台保持「面积最大优先，只有 race_lost 才回退」的语义。
    预订/应征请求在发出通知之前就进入对应平台（节省 1-3 秒）。
    ``submitted`` 可由调用方提供；每次提交成功立即追加，以便后续提交出错时
    调用方仍能处理之前已发出的请求。该列表不要在执行中并发修改。

    Returns
    -------
    ab_futures: [BookingJob(user, notifier, sorted_candidates, future), ...]
      非 Plaza 的 sorted_candidates 按面积降序；Plaza 每个 job 只包含一套房源。
    """
    ab_futures: list[BookingJob] = submitted if submitted is not None else []

    context = BookingSubmissionContext(
        loop, status_transition, booking_deadline, storage, ab_futures,
    )
    for user, notifier in user_notifiers:
        candidates = ab_candidates.get(user.id, [])
        if not (user.auto_book.enabled and candidates):
            continue
        plaza_candidates = sorted(
            (c for c in candidates if listing_source(c) == "plaza"),
            key=area_key, reverse=True,
        )
        _submit_plaza_bookings(user, notifier, plaza_candidates, context)
        _submit_standard_bookings(user, notifier, candidates, context)
    return ab_futures


async def _deliver_booking_success(result, user, notifier, web_notifier, storage, push, protected):
    """Persist the hold and deliver a successful result across independent channels."""
    booked_listing = result.listing
    guard, attempt = protected if protected else (None, None)
    # BOOKING_HOLD_SOURCES 里平台的成功结果表示进入占房窗口；
    # Plaza 的成功结果只表示应征已提交/已存在，平台仍要走筛选和分配流程。
    if (booked_listing.source or "").strip().lower() in BOOKING_HOLD_SOURCES:
        try:
            if storage.mark_listing_reserved_after_booking(booked_listing.id):
                logger.info(
                    "[%s] 已将房源本地状态标记为 Reserved（booking hold）: %s",
                    user.name, booked_listing.name,
                )
        except Exception:
            logger.exception("[%s] 更新预订房源本地状态失败，继续发送结果", user.name)
    try:
        sent = await notifier.send_booking_success(
            booked_listing, result.message, result.pay_url, result.contract_start_date
        )
    except Exception:
        logger.exception("[%s] 预订成功通知渠道异常", user.name)
        sent = False
    if web_notifier:
        try:
            await web_notifier.send_booking_success(
                booked_listing,
                result.message,
                result.pay_url,
                result.contract_start_date,
                user_id=user.id,
            )
        except Exception:
            logger.exception("[%s] 保存预订成功的面板通知失败", user.name)
    # App 推送：预订结果原来**根本不推**——push.dispatch(kind="booked")
    # 有定义没人调。eda434e 让「只用 App」的用户也能开自动预订（推送算
    # 可达），结果他们订到了却只收到一条新房源推送，锁屏上没有「订到了、
    # 去付款」。直接 await 而不是丢进 push_tasks：下面要用它的结果判断
    # 到底有没有送达。
    try:
        pushed = await push.dispatch(storage, user, booked_listing, kind="booked")
    except Exception:
        logger.exception("[%s] 预订结果推送异常", user.name)
        pushed = 0
    # 渠道或推送任一送达就算送达。原来只看渠道：只用 App 的用户渠道列表
    # 为空、send_booking_success 恒为 False，每次成功都刷一条假 CRITICAL，
    # 真的送不到时反而被淹掉（2026-10-01 一天 5 条，全是误报）。
    delivered = bool(sent) or pushed > 0
    if protected and delivered:
        guard.clear_completed(attempt)
    if not delivered and is_application_only(booked_listing):
        # 注册类没有付款时限，不需要 CRITICAL 去吵醒人——注册已经在平台上了
        logger.warning(
            "[%s] 注册已提交但通知发送失败: %s", user.name, booked_listing.name,
        )
    elif not delivered:
        # 渠道和推送都没送达，付款链接必须保留在日志中
        # 使用 CRITICAL 级别确保即使 LOG_LEVEL=WARNING 也能被看到
        logger.critical(
            "❌ [%s] 自动预订成功但通知发送失败，付款链接已记录于此，请立即操作：\n"
            "  房源：%s\n"
            "  付款：%s",
            user.name, booked_listing.name, result.pay_url,
        )


async def _deliver_booking_failure(result, user, notifier, web_notifier, storage, push, push_tasks):
    """Deliver an unsuccessful result without letting one channel block the others."""
    booked_listing = result.listing
    try:
        await notifier.send_booking_failed(booked_listing, result.message)
    except Exception:
        logger.exception("[%s] 预订失败通知渠道异常", user.name)
    if web_notifier:
        try:
            await web_notifier.send_booking_failed(
                booked_listing,
                result.message,
                user_id=user.id,
            )
        except Exception:
            logger.exception("[%s] 保存预订失败的面板通知失败", user.name)
    # 只用 App 的用户原来收不到「没订上」：渠道为空，这里又不推。
    push_tasks.append(push_booking_failed(push, storage, user, booked_listing))


async def _process_single_booking(
    user, notifier, sorted_cands, future, *, storage, web_notifier, push,
    push_tasks, blocked_in_round,
):
    """Handle one submitted result; failures cannot discard other users' results."""
    result = None
    try:
        # 逐个用户兜底。**没有它的话一个用户的异常会带走所有人**：这个循环负责
        # 发预订成功/失败通知、更新重试队列、聚合屏蔽通知，异常从这里穿出去，
        # 后面排队的用户就什么都没有了——而且是静默的。
        #
        # 2026-09-02 的实例：bookers/rentcafe.py 把 RentCafeSession(...) 写在
        # try 外面，构造一抛就是这个形状。根因已修，但**爆炸半径不该由被调用方
        # 来保证**。
        try:
            result = await asyncio.wait_for(
                asyncio.shield(future), timeout=_BOOKING_RESULT_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            future.add_done_callback(_observe_late_booking)
            logger.error("[%s] 预订任务超过等待时间，后台任务可能仍在运行", user.name)
            plaza_protected = getattr(future, "plaza_guard", None)
            if plaza_protected:
                await _alert_plaza_attempt(storage, push, notifier, user, sorted_cands[0],
                                           *plaza_protected)
            return
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            if getattr(future, "plaza_guard", None):
                await _alert_plaza_attempt(storage, push, notifier, user, sorted_cands[0],
                                           *future.plaza_guard)
            logger.error(
                "[%s] 预订任务抛出未捕获异常，已隔离该用户（其余用户照常处理）",
                user.name, exc_info=True,
            )
            return
        protected = getattr(future, "plaza_guard", None)
        if protected:
            guard, attempt = protected
            try:
                row = guard.get_attempt(attempt)
            except Exception:
                logger.exception("[%s] 无法读取 Plaza 应征保护，保留记录并通知用户", user.name)
                await _notify_plaza_uncertain(storage, push, notifier, user,
                                              result.listing if result else sorted_cands[0])
                return
            confirmed_plaza_rejection = result is not None and result.phase == "save_rejected"
            if (row and row["mutated"] and (result is None or not result.success)
                    and not confirmed_plaza_rejection):
                await _alert_plaza_attempt(storage, push, notifier, user,
                                           result.listing if result else sorted_cands[0], guard, attempt)
                return
            if row and row["timed_out"]:
                return
            if result is None or not result.success:
                guard.clear_completed(attempt)
        if result is None:
            return
        # result.listing 是实际被尝试预订的那套房源（fallback 后可能不是 sorted_cands[0]）
        booked_listing = result.listing

        # 更新重试队列（dry_run 不改变队列状态，避免污染正式运行时的数据）
        if not result.dry_run:
            if result.phase == "race_lost":
                runtime.retry_queue.add(user.id, {c.id for c in sorted_cands})
                logger.info(
                    "[%s] 📝 %d 套候选加入重试队列（下次扫描仍可预订时将重试）",
                    user.name, len(sorted_cands),
                )
            elif result.phase == "blocked":
                # blocked 是 IP/指纹级，不是房源级问题；保留 runtime.retry_queue 状态不动，
                # 等下轮换指纹后再决定是否重试。
                pass
            else:
                for c in sorted_cands:
                    runtime.retry_queue.discard(user.id, c.id)

        if result.dry_run:
            logger.info("[%s] [DRY RUN] 自动预订跳过: %s", user.name, booked_listing.name)
        elif result.phase == "blocked":
            # 累积起来：admin 那条聚合成一份，用户那条按房源各发各的
            blocked_in_round.append((user, notifier, result.message, booked_listing))
        elif result.success:
            await _deliver_booking_success(result, user, notifier, web_notifier, storage, push, protected)
        else:
            await _deliver_booking_failure(result, user, notifier, web_notifier, storage, push, push_tasks)

    except Exception:
        logger.exception("[%s] 处理预订结果失败，继续处理其他用户", user.name)
        if result is not None and getattr(result, "success", False):
            logger.critical("[%s] 预订成功但处理结果时发生异常，房源：%s，付款链接：%s",
                            user.name, getattr(getattr(result, "listing", None), "name", "未知"),
                            getattr(result, "pay_url", ""))
        # Existing durable guards remain in place for later verification.
        return


async def process_booking_results(
    ab_futures: list[BookingJob],
    web_notifier: "WebNotifier | None",
    storage: Storage,
    push,
    *, task_sink: list | None = None,
) -> list:
    """await 预订 Future，发送成功/失败通知，并聚合本轮屏蔽通知。

    Returns
    -------
    push_tasks: 预订失败给用户推的、屏蔽聚合时给 admin 推的 asyncio.Task（可能为空）。
    提供 task_sink 时，创建的任务立即写入该列表，异常后仍可由调用方等待。
    """
    push_tasks: list = task_sink if task_sink is not None else []
    # 本轮被屏蔽的用户（含 notifier），所有候选 await 完后聚合发一条节流通知，
    # 避免每个用户/每个候选都发一次"预订失败"刷屏。
    blocked_in_round: list[tuple[UserConfig, BaseNotifier, str, "Listing"]] = []

    for user, notifier, sorted_cands, future in ab_futures:
        await _process_single_booking(
            user, notifier, sorted_cands, future, storage=storage,
            web_notifier=web_notifier, push=push, push_tasks=push_tasks,
            blocked_in_round=blocked_in_round,
        )

    # ── 聚合屏蔽通知（共享 scrape 的 30 min 节流，避免双重打扰）────── #
    if blocked_in_round and should_notify_block():
        names = sorted({u.name for u, _, _, _ in blocked_in_round})
        # 取第一条 msg 作为详情（所有候选的 message 通常相同 —— 都是同一 CF 屏蔽）
        detail = blocked_in_round[0][2]
        agg_msg = (
            f"🚫 自动预订被 403 屏蔽（{len(blocked_in_round)} 套候选 / "
            f"{len(names)} 个用户）\n\n{detail}\n\n"
            f"影响用户: {', '.join(names)}\n"
            f"30 分钟内不会重复通知。"
        )
        # 用户侧：走房源通知那条路，各发各的。
        #
        # 这条对用户是**该发**的——他开了自动预订，结果没订上，得知道要手动补。
        # 但上面那段聚合文案是给运维看的，直接发给用户有两个问题：技术细节
        # （403、CF、指纹）他无从处置；而且「影响用户: A, B, C」会把其他人的
        # 名字抄送给每一个人。
        for u, n, _, listing in blocked_in_round:
            try:
                await n.send_booking_failed(
                    listing, "平台暂时拒绝了自动预订请求，请尽快手动预订",
                )
            except Exception:
                logger.exception("[%s] 预订被拒通知渠道异常", u.name)
            push_tasks.append(push_booking_failed(push, storage, u, listing))
        if web_notifier:
            try:
                await web_notifier.send_error(agg_msg)
            except Exception:
                logger.exception("保存预订屏蔽告警失败")
        # admin 也要收到 403 屏蔽通知
        push_tasks.append(asyncio.create_task(
            push.dispatch_admin(storage, agg_msg, kind="blocked"),
        ))
    elif blocked_in_round:
        logger.info(
            "🚫 %d 套候选 / %d 个用户被屏蔽，30 min 节流期内不发通知",
            len(blocked_in_round),
            len({u.id for u, _, _, _ in blocked_in_round}),
        )

    return push_tasks
