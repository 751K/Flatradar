"""一次性迁移：平台 key ``ourcampus`` → ``canvas``。

2026-11 OurCampus 并入 Greystar 的 Canvas 品牌（ourcampus.nl 302 到
canvas-world.com），RentCafe 门户也换了地址。平台 key 与用户可见的楼盘名随之改名，
做法与 ``_codomo_rename`` 相同：库里凡是存了平台名或旧楼盘名的地方都改掉，否则
旧房源和用户设置会挂在一个已经不存在的平台下。

改写范围
--------
- ``listings``：``source``；``city`` / ``name`` / ``features`` 里的旧楼盘名
  （``OurCampus Amsterdam Diemen`` / ``OurCampus Diemen``）。``url`` 不动：那是
  房源当时的真实地址。
- ``round_stats.source``
- ``user_configs``：筛选与自动预订筛选里的平台和楼盘名；自动预订凭据键
  ``ourcampus_email`` / ``ourcampus_password`` 改名为 ``canvas_*``（密文原样搬）
- ``app_settings``：``SOURCES`` 等平台清单；订阅键 ``OURCAMPUS_CITIES`` 改名
  ``CANVAS_CITIES``，值里的旧楼盘名一并改写（城市 key ``diemen`` 不变）
- ``meta``：注册表快照里的 ``OURCAMPUS_CITIES``；旧门户的熔断 / 告警状态
  （``backoff:circuit_ourcampus:*``、``watchdog_fired:*:ourcampus`` 等）直接删除。
  它们描述的是已经下线的旧门户，搬给新门户只会让它一上线就带着旧的熔断与告警。

房源 id 不改：前缀仍是 ``oc_``。改前缀会让全部房源在下一轮被当成新房源推送。
``web_notifications`` 等历史文本不改：那是当时发出去的原文。

只跑一次，靠 meta 标记 ``canvas_rename_v1``。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3

logger = logging.getLogger(__name__)

OLD = "ourcampus"
NEW = "canvas"
MIGRATION_KEY = "canvas_rename_v1"

#: 用户可见名的改写，按长到短排，避免短的先替换掉长的一部分。
_NAME_SWAPS = (
    ("OurCampus Amsterdam Diemen", "Canvas Amsterdam Diemen"),
    ("OurCampus Diemen", "Canvas Diemen"),
)

_SOURCE_LIST_SETTINGS = (
    "SOURCES", "SHADOW_SOURCES", "SHARD_SIZES",
    "SOURCE_MIN_INTERVALS", "SOURCE_PEAK_MIN_INTERVALS",
)

_CRED_KEYS = (("ourcampus_email", "canvas_email"), ("ourcampus_password", "canvas_password"))

_TOKEN = re.compile(rf"(?<![A-Za-z0-9_]){OLD}(?![A-Za-z0-9_])", re.IGNORECASE)
#: 旧门户的运行状态。``_`` 也算分隔，所以 ``circuit_ourcampus`` 能认出来。
_META_PATTERN = re.compile(rf"(?<![a-z0-9]){OLD}(?![a-z0-9])", re.IGNORECASE)


def _swap_names(text: str) -> str:
    for old, new in _NAME_SWAPS:
        text = text.replace(old, new)
    return text


def _swap_value(v):
    """平台 key 整值替换，楼盘名子串替换。非字符串原样返回。"""
    if not isinstance(v, str):
        return v
    if v.strip().lower() == OLD:
        return NEW
    return _swap_names(v)


def _rewrite_lists(obj) -> bool:
    """递归改写 filter dict 里所有字符串列表。返回是否有改动。"""
    changed = False
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list):
                out = []
                for x in v:
                    y = _swap_value(x)
                    changed |= y != x
                    if y not in out:
                        out.append(y)
                if len(out) != len(v):
                    changed = True
                obj[k] = out
            elif isinstance(v, dict):
                changed |= _rewrite_lists(v)
    return changed


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _load(raw):
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def rename_ourcampus(conn: sqlite3.Connection) -> dict[str, int]:
    """执行改写，返回各部分改了多少行。调用方负责打 meta 标记。"""
    stats: dict[str, int] = {}
    with conn:
        if _table_exists(conn, "listings"):
            # 只做楼盘名的文本替换，面积 / 能耗等派生排序列（area_value 等）
            # 不受影响，所以不重算它们。
            n = conn.execute(
                "UPDATE listings SET source=? WHERE lower(source)=?", (NEW, OLD)).rowcount
            for old_name, new_name in _NAME_SWAPS:
                conn.execute(
                    "UPDATE listings SET city=REPLACE(city, ?, ?), "
                    "name=REPLACE(name, ?, ?), features=REPLACE(features, ?, ?) "
                    "WHERE source=?",
                    (old_name, new_name) * 3 + (NEW,))
            stats["listings"] = n
        if _table_exists(conn, "round_stats"):
            cur = conn.execute(
                "UPDATE round_stats SET source=? WHERE lower(source)=?", (NEW, OLD))
            stats["round_stats"] = cur.rowcount

        if _table_exists(conn, "user_configs"):
            n = 0
            for uid, lf_raw, ab_raw in conn.execute(
                    "SELECT id, listing_filter_json, auto_book_json FROM user_configs"
            ).fetchall():
                updates = {}
                lf = _load(lf_raw)
                if _rewrite_lists(lf):
                    updates["listing_filter_json"] = json.dumps(lf, ensure_ascii=False)
                ab = _load(ab_raw)
                if isinstance(ab, dict):
                    changed = _rewrite_lists(ab.get("listing_filter"))
                    for old_k, new_k in _CRED_KEYS:
                        if old_k in ab:
                            value = ab.pop(old_k)
                            if value and not ab.get(new_k):
                                ab[new_k] = value
                            changed = True
                    if changed:
                        updates["auto_book_json"] = json.dumps(ab, ensure_ascii=False)
                if updates:
                    sets = ", ".join(f"{k}=?" for k in updates)
                    conn.execute(f"UPDATE user_configs SET {sets} WHERE id=?",
                                 (*updates.values(), uid))
                    n += 1
            stats["user_configs"] = n

        if _table_exists(conn, "app_settings"):
            n = 0
            for key in _SOURCE_LIST_SETTINGS:
                row = conn.execute(
                    "SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
                if row and _TOKEN.search(row[0] or ""):
                    conn.execute("UPDATE app_settings SET value=? WHERE key=?",
                                 (_TOKEN.sub(NEW, row[0]), key))
                    n += 1
            old = conn.execute(
                "SELECT value FROM app_settings WHERE key='OURCAMPUS_CITIES'").fetchone()
            if old is not None:
                has_new = conn.execute(
                    "SELECT 1 FROM app_settings WHERE key='CANVAS_CITIES'").fetchone()
                if not has_new:
                    conn.execute(
                        "UPDATE app_settings SET key='CANVAS_CITIES', value=? "
                        "WHERE key='OURCAMPUS_CITIES'", (_swap_names(old[0] or ""),))
                else:
                    conn.execute("DELETE FROM app_settings WHERE key='OURCAMPUS_CITIES'")
                n += 1
            stats["app_settings"] = n

        if _table_exists(conn, "meta"):
            keys = [k for (k,) in conn.execute("SELECT key FROM meta").fetchall()
                    if _META_PATTERN.search(k or "")]
            conn.executemany("DELETE FROM meta WHERE key=?", [(k,) for k in keys])
            n = len(keys)
            row = conn.execute(
                "SELECT value FROM meta WHERE key='registry_targets_seen'").fetchone()
            seen = _load(row[0]) if row else None
            if isinstance(seen, dict) and "OURCAMPUS_CITIES" in seen:
                seen.setdefault("CANVAS_CITIES", seen.pop("OURCAMPUS_CITIES"))
                seen.pop("OURCAMPUS_CITIES", None)
                conn.execute(
                    "UPDATE meta SET value=? WHERE key='registry_targets_seen'",
                    (_swap_names(json.dumps(seen, sort_keys=True)),))
                n += 1
            stats["meta"] = n
    return stats
