"""一次性迁移：平台 key holland2stay → codomo（mstorage/_codomo_rename.py）。"""
from __future__ import annotations

import json
import sqlite3

from mstorage._codomo_rename import MIGRATION_KEY, rename_holland2stay


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE listings (id TEXT PRIMARY KEY, source TEXT);
        CREATE TABLE round_stats (id INTEGER PRIMARY KEY, source TEXT);
        CREATE TABLE user_configs (id TEXT PRIMARY KEY, listing_filter_json TEXT,
                                   auto_book_json TEXT);
        CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE booking_attempts (id INTEGER PRIMARY KEY);
        CREATE TABLE plaza_attempts (id INTEGER PRIMARY KEY);
    """)
    conn.executemany("INSERT INTO listings VALUES (?, ?)", [
        ("a", "holland2stay"), ("b", ""), ("c", None), ("d", "xior")])
    conn.executemany("INSERT INTO round_stats(source) VALUES (?)",
                     [("holland2stay",), ("plaza",)])
    conn.execute("INSERT INTO user_configs VALUES (?, ?, ?)", (
        "u1",
        json.dumps({"allowed_sources": ["holland2stay", "xior"]}),
        json.dumps({"enabled": True, "email": "x",
                    "listing_filter": {"allowed_sources": ["Holland2Stay"]}}),
    ))
    conn.executemany("INSERT INTO app_settings VALUES (?, ?)", [
        ("SOURCES", "holland2stay,xior"),
        ("SHARD_SIZES", "xior:4,holland2stay:2"),
        ("CITIES", "Eindhoven,29|Amsterdam,24"),
        ("AVAILABILITY_FILTERS", "Available to book,179"),
        ("H2S_REST_ENABLED", "true"),
        ("CHECK_INTERVAL", "300"),
    ])
    conn.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("pacing_mult:holland2stay", "2.0"),
        ("registry_targets_seen", json.dumps({"CITIES": ["24", "29"], "XIOR_CITIES": ["p1"]})),
        ("other", "holland2stay stays in values"),
    ])
    return conn


def _one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()


def test_listings_and_round_stats():
    conn = _db()
    stats = rename_holland2stay(conn)
    rows = dict(conn.execute("SELECT id, source FROM listings").fetchall())
    assert rows == {"a": "codomo", "b": "codomo", "c": "codomo", "d": "xior"}
    assert [r[0] for r in conn.execute("SELECT source FROM round_stats ORDER BY id")] \
        == ["codomo", "plaza"]
    assert stats["listings"] == 3


def test_user_filters():
    conn = _db()
    rename_holland2stay(conn)
    lf, ab = _one(conn, "SELECT listing_filter_json, auto_book_json FROM user_configs")
    assert json.loads(lf)["allowed_sources"] == ["codomo", "xior"]
    ab = json.loads(ab)
    assert ab["listing_filter"]["allowed_sources"] == ["codomo"]
    assert "email" not in ab          # 旧 H2S 下单凭据清掉
    assert ab["enabled"] is True      # 其余字段原样保留


def test_settings():
    conn = _db()
    rename_holland2stay(conn)
    s = dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
    assert s["SOURCES"] == "codomo,xior"
    assert s["SHARD_SIZES"] == "xior:4,codomo:2"
    assert s["CODOMO_CITIES"] == "Eindhoven,29|Amsterdam,24"
    assert "CITIES" not in s
    assert "AVAILABILITY_FILTERS" not in s and "H2S_REST_ENABLED" not in s
    assert s["CHECK_INTERVAL"] == "300"


def test_existing_codomo_cities_wins():
    conn = _db()
    conn.execute("INSERT INTO app_settings VALUES ('CODOMO_CITIES', 'Utrecht,27')")
    rename_holland2stay(conn)
    s = dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
    assert s["CODOMO_CITIES"] == "Utrecht,27" and "CITIES" not in s


def test_meta():
    conn = _db()
    rename_holland2stay(conn)
    m = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert m["pacing_mult:codomo"] == "2.0" and "pacing_mult:holland2stay" not in m
    seen = json.loads(m["registry_targets_seen"])
    assert seen["CODOMO_CITIES"] == ["24", "29"] and "CITIES" not in seen
    assert m["other"] == "holland2stay stays in values"


def test_old_h2s_guard_table_dropped():
    conn = _db()
    rename_holland2stay(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "booking_attempts" not in tables
    assert "plaza_attempts" in tables


def test_idempotent():
    conn = _db()
    rename_holland2stay(conn)
    snap = list(conn.iterdump())
    rename_holland2stay(conn)
    assert list(conn.iterdump()) == snap


def test_storage_runs_it_once(tmp_path):
    from storage import Storage

    db = tmp_path / "t.db"
    st = Storage(db)
    assert st.get_meta(MIGRATION_KEY, default="")
    st.conn.execute(
        "INSERT INTO listings(id, name, status, source) VALUES ('x','x','s','holland2stay')")
    st.conn.commit()
    st.close()
    # 已打标：重开不再改写（新数据不会再出现 holland2stay，这里只验证只跑一次）
    st = Storage(db)
    assert _one(st.conn, "SELECT source FROM listings WHERE id='x'")[0] == "holland2stay"
    st.close()


def test_v2_purges_underscore_joined_h2s_state():
    """v1 的整词匹配认不出 ``_holland2stay`` 这类下划线拼接的键。"""
    from mstorage._codomo_rename import purge_h2s_runtime_state

    conn = _db()
    rename_holland2stay(conn)
    # 生产库里 v1 之后仍留着这些键（v1 认不出，或是旧进程退出前又写回来的）
    conn.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("pacing_mult_holland2stay", "1.0"),
        ("backoff:circuit_holland2stay:until", "1"),
        ("backoff:h2s_login_block:until", "1"),
        ("backoff:throttle_h2s_long_block:streak", "0"),
        ("watchdog_fired:stale_full_scan:holland2stay", ""),
        ("backoff:circuit_codomo:until", "1"),
    ])
    assert purge_h2s_runtime_state(conn) == 5
    keys = {k for (k,) in conn.execute("SELECT key FROM meta")}
    assert "backoff:circuit_codomo:until" in keys
    assert "pacing_mult:codomo" in keys and "other" in keys
    assert not any("holland2stay" in k or "h2s_" in k for k in keys)


def test_storage_marks_cleanup(tmp_path):
    from mstorage._codomo_rename import CLEANUP_KEY
    from storage import Storage

    st = Storage(tmp_path / "t.db")
    assert st.get_meta(CLEANUP_KEY, default="")
    st.close()
