"""一次性迁移：平台 key ``holland2stay`` → ``codomo``。

2026-11 Holland2Stay 改名 Codomo（www.holland2stay.com 全站 301 到 codomo.eu），
平台 key 随之改名。库里凡是存了平台名的地方都要跟着改，否则旧房源会挂在一个
已经不存在的平台下：筛选按平台白名单精确匹配，面板按平台分组，统计按平台聚合。

改写范围
--------
- ``listings.source``：``holland2stay``，以及早期没有 source 字段时留下的空值
  （那时只有 H2S 一个平台，空值一律表示 H2S）
- ``round_stats.source``
- ``user_configs``：筛选与自动预订里的 ``allowed_sources``；删掉自动预订里旧 H2S
  的下单凭据（``email`` / ``password`` / ``payment_method``）
- 旧 H2S 预订保护表 ``booking_attempts`` 删除
- ``app_settings``：``SOURCES`` 等含平台名的设置；订阅键 ``CITIES`` 改名
  ``CODOMO_CITIES``（值不变，城市 key 沿用旧 ID）；只为旧接口存在的键删除
- ``meta``：以平台名为键的运行状态（节流、分片游标、退避），以及注册表快照里
  的 ``CITIES``

房源 id 不改：仍是 URL 末段 slug，与 Codomo 的 URL 同形，新旧房源自然接上。

只跑一次，靠 meta 标记 ``codomo_rename_v1``。v1 漏掉的旧 H2S 运行状态由
``purge_h2s_runtime_state`` 另行清除（标记 ``codomo_cleanup_v2``）。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3

logger = logging.getLogger(__name__)

OLD = "holland2stay"
NEW = "codomo"
MIGRATION_KEY = "codomo_rename_v1"

#: 值是平台清单 / ``平台:数字`` 映射的设置键。
_SOURCE_LIST_SETTINGS = (
    "SOURCES", "SHADOW_SOURCES", "SHARD_SIZES",
    "SOURCE_MIN_INTERVALS", "SOURCE_PEAK_MIN_INTERVALS",
)

#: 只为旧 H2S 接口（GraphQL 状态筛选、REST 快速通道、预登录）存在的设置，
#: 迁移时一并删除。留着不会出错，但设置页的「未知键」提示会一直报它们。
OBSOLETE_SETTINGS = (
    "AVAILABILITY_FILTERS", "WARM_BROWSER_LANE",
    "H2S_REST_ENABLED", "H2S_REST_INTERVAL", "H2S_REST_START", "H2S_REST_END",
    "H2S_REST_WEEKDAYS_ONLY", "H2S_REST_HOME_INTERVAL",
)

#: auto_book_json 里旧 H2S 下单凭据的键。
_H2S_CRED_KEYS = ("email", "password", "payment_method")

_TOKEN = re.compile(rf"(?<![A-Za-z0-9_]){OLD}(?![A-Za-z0-9_])", re.IGNORECASE)


def _swap_token(text: str) -> str:
    """把逗号 / 竖线 / 冒号分隔串里的 ``holland2stay`` 整词替换成 ``codomo``。"""
    return _TOKEN.sub(NEW, text or "")


def _swap_sources_list(values) -> tuple[list, bool]:
    if not isinstance(values, list):
        return values, False
    out, changed = [], False
    for v in values:
        if isinstance(v, str) and v.strip().lower() == OLD:
            v, changed = NEW, True
        if v not in out:
            out.append(v)
    return out, changed


def _rewrite_filter(obj) -> bool:
    """改写一个 listing filter dict 的 allowed_sources。返回是否有改动。"""
    if not isinstance(obj, dict):
        return False
    new, changed = _swap_sources_list(obj.get("allowed_sources"))
    if changed:
        obj["allowed_sources"] = new
    return changed


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def rename_holland2stay(conn: sqlite3.Connection) -> dict[str, int]:
    """执行改写，返回各部分改了多少行。调用方负责打 meta 标记。"""
    stats: dict[str, int] = {}
    with conn:
        if _table_exists(conn, "listings"):
            cur = conn.execute(
                "UPDATE listings SET source=? "
                "WHERE source IS NULL OR source='' OR lower(source)=?",
                (NEW, OLD))
            stats["listings"] = cur.rowcount
        if _table_exists(conn, "round_stats"):
            cur = conn.execute(
                "UPDATE round_stats SET source=? WHERE lower(source)=?", (NEW, OLD))
            stats["round_stats"] = cur.rowcount

        if _table_exists(conn, "user_configs"):
            n = 0
            rows = conn.execute(
                "SELECT id, listing_filter_json, auto_book_json FROM user_configs"
            ).fetchall()
            for row in rows:
                uid, lf_raw, ab_raw = row[0], row[1], row[2]
                updates = {}
                try:
                    lf = json.loads(lf_raw) if lf_raw else None
                except (TypeError, ValueError):
                    lf = None
                if _rewrite_filter(lf):
                    updates["listing_filter_json"] = json.dumps(lf, ensure_ascii=False)
                try:
                    ab = json.loads(ab_raw) if ab_raw else None
                except (TypeError, ValueError):
                    ab = None
                if isinstance(ab, dict):
                    changed = _rewrite_filter(ab.get("listing_filter"))
                    # 旧 H2S 下单凭据（email / password 已加密 / payment_method）。
                    # 用户表加载时已经忽略它们，这里把残留的密文也从库里清掉。
                    for k in _H2S_CRED_KEYS:
                        if k in ab:
                            ab.pop(k)
                            changed = True
                    if changed:
                        updates["auto_book_json"] = json.dumps(ab, ensure_ascii=False)
                if updates:
                    sets = ", ".join(f"{k}=?" for k in updates)
                    conn.execute(f"UPDATE user_configs SET {sets} WHERE id=?",
                                 (*updates.values(), uid))
                    n += 1
            stats["user_configs"] = n

        # 旧 H2S 预订的账号级保护表（mcore/booking_guard 的 BookingGuard，已删）。
        # Plaza 用自己的 plaza_attempts，不受影响。
        if _table_exists(conn, "booking_attempts"):
            conn.execute("DROP TABLE booking_attempts")
            stats["booking_attempts_dropped"] = 1

        if _table_exists(conn, "app_settings"):
            n = 0
            for key in _SOURCE_LIST_SETTINGS:
                row = conn.execute(
                    "SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
                if row and _TOKEN.search(row[0] or ""):
                    conn.execute("UPDATE app_settings SET value=? WHERE key=?",
                                 (_swap_token(row[0]), key))
                    n += 1
            old_cities = conn.execute(
                "SELECT value FROM app_settings WHERE key='CITIES'").fetchone()
            has_new = conn.execute(
                "SELECT 1 FROM app_settings WHERE key='CODOMO_CITIES'").fetchone()
            if old_cities is not None:
                if not has_new:
                    conn.execute(
                        "UPDATE app_settings SET key='CODOMO_CITIES' WHERE key='CITIES'")
                else:
                    conn.execute("DELETE FROM app_settings WHERE key='CITIES'")
                n += 1
            ph = ",".join("?" * len(OBSOLETE_SETTINGS))
            cur = conn.execute(
                f"DELETE FROM app_settings WHERE key IN ({ph})", OBSOLETE_SETTINGS)
            stats["app_settings"] = n + cur.rowcount

        if _table_exists(conn, "meta"):
            n = 0
            for key, value in conn.execute(
                    "SELECT key, value FROM meta WHERE lower(key) LIKE ?",
                    (f"%{OLD}%",)).fetchall():
                new_key = _TOKEN.sub(NEW, key)
                if new_key == key:
                    continue
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                             (new_key, value))
                conn.execute("DELETE FROM meta WHERE key=?", (key,))
                n += 1
            row = conn.execute(
                "SELECT value FROM meta WHERE key='registry_targets_seen'").fetchone()
            if row:
                try:
                    seen = json.loads(row[0] or "{}")
                except (TypeError, ValueError):
                    seen = None
                if isinstance(seen, dict) and "CITIES" in seen:
                    seen.setdefault("CODOMO_CITIES", seen.pop("CITIES"))
                    seen.pop("CITIES", None)
                    conn.execute(
                        "UPDATE meta SET value=? WHERE key='registry_targets_seen'",
                        (json.dumps(seen, sort_keys=True),))
                    n += 1
            stats["meta"] = n
    return stats


CLEANUP_KEY = "codomo_cleanup_v2"

#: v1 漏掉的旧 H2S 运行状态。v1 的整词匹配把 ``_`` 当成词的一部分，所以
#: ``pacing_mult_holland2stay``、``backoff:circuit_holland2stay:*`` 这类下划线
#: 拼接的键没被认出来；``h2s_login_block`` / ``throttle_h2s_long_block`` 则只
#: 属于旧站的预登录与长封锁。它们描述的是旧站的熔断 / 节流 / 告警状态，搬给
#: Codomo 没有意义（新站是另一套反爬），直接删掉。
_H2S_META_PATTERN = re.compile(r"holland2stay|(?<![a-z0-9])h2s_", re.IGNORECASE)


def purge_h2s_runtime_state(conn: sqlite3.Connection) -> int:
    """删除 meta 里旧 H2S 的运行状态，返回删了几行。调用方负责打标。"""
    if not _table_exists(conn, "meta"):
        return 0
    keys = [k for (k,) in conn.execute("SELECT key FROM meta").fetchall()
            if _H2S_META_PATTERN.search(k or "")]
    with conn:
        conn.executemany("DELETE FROM meta WHERE key=?", [(k,) for k in keys])
    return len(keys)
