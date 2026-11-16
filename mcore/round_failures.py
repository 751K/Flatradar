"""抓取阶段的失败处置。

从 monitor.run_once 的 except 分支提出来。控制流保持原样：多数分支在告警/冷却
之后重抛（让 main_loop 做退避），只有未分类异常返回 ``{}`` 走正常间隔。
日志通道仍用 ``monitor``。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from config import (
    get_proxy_url,
    is_proxy_in_cooldown,
    is_proxy_native_fallback_active,
    proxy_failure_mark_count,
    report_proxy_failure,
)
from mcore.outage import notify_admin_only
from mcore.throttles import (
    should_notify_block,
    should_notify_internal,
    should_notify_maintenance,
    should_notify_proxy,
)
from scrapers import (
    BlockedError,
    ProxyError,
    RateLimitError,
    ScrapeNetworkError,
    UpstreamMaintenanceError,
    is_proxy_account_error,
    is_proxy_service_error,
)

logger = logging.getLogger("monitor")


def _task_labels(tasks) -> list[str]:
    return [f"{t.source}:{t.city_display}" for t in tasks]


async def handle_scrape_failure(
    exc: BaseException,
    *,
    storage,
    web_notifier,
    scrape_tasks,
    user_notifiers,
    dry_run: bool,
) -> dict[str, bool]:
    """处置抓取阶段异常。

    多数分支在告警/冷却之后 ``raise``（重抛当前异常，交给 main_loop 退避）；
    只有未分类异常返回 ``{}``，走正常间隔。
    """
    if isinstance(exc, BlockedError):
        # 403 = Cloudflare WAF 屏蔽，等待无法恢复，必须换代理/重启。
        proxy_on = bool(get_proxy_url())
        logger.error(
            "🚫 抓取被屏蔽 (HTTP 403) cities=%d users=%d proxy=%s: %s",
            len(scrape_tasks), len(user_notifiers),
            "yes" if proxy_on else "no", exc,
        )
        if not dry_run and should_notify_block():
            err_msg = (
                f"🚫 抓取被 403 屏蔽\n\n{exc}\n\n"
                f"代理状态: {'已启用' if proxy_on else '未启用'}\n"
                f"30 分钟内不会重复通知。"
            )
            await notify_admin_only(
                storage, web_notifier, err_msg, kind="scrape_blocked",
            )
        raise exc

    if isinstance(exc, RateLimitError):
        logger.warning(
            "⚠️  抓取被限流 cities=%d users=%d proxy=%s: %s",
            len(scrape_tasks), len(user_notifiers),
            "yes" if get_proxy_url() else "no",
            exc,
        )
        if not dry_run:
            err_msg = f"⚠️ 抓取被限流（429）\n{exc}\n监控将暂停 5 分钟后继续。"
            await notify_admin_only(
                storage, web_notifier, err_msg, kind="scrape_rate_limited",
            )
        raise exc

    if isinstance(exc, UpstreamMaintenanceError):
        # 平台维护：自己会恢复，不给普通用户发告警；给 admin 面板发一条。
        logger.info(
            "🔧 上游平台维护中，本轮跳过 cities=%d users=%d: %s",
            len(scrape_tasks), len(user_notifiers), exc,
        )
        if not dry_run:
            now_iso = datetime.now(timezone.utc).isoformat()
            first_detect = not storage.get_meta("upstream_maintenance_seen_at", default="")
            if first_detect:
                storage.set_meta("upstream_maintenance_seen_at", now_iso)
            storage.set_meta("upstream_maintenance_last_at", now_iso)

            if web_notifier and should_notify_maintenance():
                hint = (
                    "🔧 上游平台计划维护中\n\n"
                    f"{exc}\n\n"
                    "监控已暂停轮询，平台恢复后会自动继续。"
                    "无需操作；状态会在 dashboard 顶部 banner 显示。"
                )
                await web_notifier.send_error(hint)
        raise exc

    if isinstance(exc, ProxyError):
        # 抓取代理失效。ProxyError 是 ScrapeNetworkError 子类——控制流仍走
        # 网络失败冷却，但要把失败的代理标记进 cooldown 并告警 admin。
        old_proxy = get_proxy_url()
        service_error = is_proxy_service_error(exc)
        account_level = is_proxy_account_error(exc)
        new_proxy = report_proxy_failure(
            service_error_confirmed=service_error, account_level=account_level,
        )
        confirmed_down = is_proxy_in_cooldown(old_proxy)
        native_fallback = is_proxy_native_fallback_active()
        switched_proxy = bool(new_proxy and new_proxy != old_proxy)
        logger.error(
            "🛰️ 抓取代理故障%s: %s",
            (
                "，下一轮切换到备用代理"
                if switched_proxy
                else "，下一轮降级为服务器原生 IP 直连"
                if native_fallback
                else f"，服务端异常待确认（{proxy_failure_mark_count(old_proxy)}/2）"
                if old_proxy and not confirmed_down and service_error
                else "，未确认代理服务端异常，按普通网络失败处理"
                if old_proxy and not service_error
                else "（无备用代理可切）"
            ),
            exc,
        )
        should_alert = switched_proxy or native_fallback or not old_proxy
        if not dry_run and should_alert and should_notify_proxy():
            if switched_proxy:
                tail = "已自动切换到备用代理，下一轮重试。请尽快修复主代理。"
            elif native_fallback:
                tail = (
                    "所有已配置代理都在冷却中，监控将临时降级为服务器原生 IP 直连。"
                    "降级期间抓取频率最多 10 分钟一次，代理冷却结束后会自动恢复。"
                )
            else:
                tail = (
                    "监控已暂停抓取（代理不通时无法绕过 Cloudflare）。"
                    "请检查 HTTPS_PROXY，或配置 SCRAPE_PROXIES_FALLBACK 备用代理。"
                )
            msg = f"抓取代理失效\n\n{exc}\n\n{tail}\n30 分钟内不重复通知。"
            if web_notifier:
                await web_notifier.send_error(msg)
            from mcore import push as _push
            try:
                await _push.dispatch_admin(storage, msg, kind="proxy")
            except Exception:
                logger.debug("代理失效 admin push 失败（已忽略）", exc_info=True)
        raise exc

    if isinstance(exc, ScrapeNetworkError):
        logger.error(
            "抓取全部网络失败 cities=%s users=%d proxy=%s: %s",
            _task_labels(scrape_tasks), len(user_notifiers),
            "yes" if get_proxy_url() else "no",
            exc,
        )
        raise exc

    # 未分类异常：只告警 admin 并加 30 min 节流，然后走正常间隔。
    logger.error(
        "抓取阶段未分类错误 cities=%s users=%d: %s",
        _task_labels(scrape_tasks), len(user_notifiers), exc,
        exc_info=True,
    )
    if not dry_run and should_notify_internal():
        await notify_admin_only(
            storage, web_notifier,
            f"抓取阶段未分类错误\n\n{type(exc).__name__}: {exc}\n\n"
            f"这是一条未被归类的内部异常，请查看服务器日志排查。"
            f"30 分钟内不重复通知。",
            kind="error",
        )
    return {}
