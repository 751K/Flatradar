"""一次性迁移：平台 key ourcampus → canvas（mstorage/_canvas_rename.py）。"""
from __future__ import annotations

import json
import sqlite3

from mstorage._canvas_rename import MIGRATION_KEY, rename_ourcampus


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE listings (id TEXT PRIMARY KEY, source TEXT, city TEXT,
                               name TEXT, features TEXT, url TEXT);
        CREATE TABLE round_stats (id INTEGER PRIMARY KEY, source TEXT);
        CREATE TABLE user_configs (id TEXT PRIMARY KEY, listing_filter_json TEXT,
                                   auto_book_json TEXT);
        CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    old_url = ("https://new-ourcampus-amsterdam-diemen-rentcafewebsiteuk.securerc.co.uk"
               "/onlineleasing/new-ourcampus-amsterdam-diemen/floorplans.aspx")
    conn.executemany("INSERT INTO listings VALUES (?, ?, ?, ?, ?, ?)", [
        ("oc_1", "ourcampus", "OurCampus Amsterdam Diemen", "OurCampus Diemen #3164",
         json.dumps(["Building: OurCampus Amsterdam Diemen"]), old_url),
        ("od_1", "ourdomain", "Amsterdam Diemen", "Diemen #6045", "[]", "u"),
    ])
    conn.executemany("INSERT INTO round_stats(source) VALUES (?)",
                     [("ourcampus",), ("plaza",)])
    conn.execute("INSERT INTO user_configs VALUES (?, ?, ?)", (
        "u1",
        json.dumps({"allowed_sources": ["ourcampus", "canvas", "xior"],
                    "allowed_cities": ["OurCampus Amsterdam Diemen", "Amsterdam"]}),
        json.dumps({"enabled": True, "ourcampus_email": "a@b.nl",
                    "ourcampus_password": "gAAAA-cipher",
                    "listing_filter": {"allowed_sources": ["OurCampus"]}}),
    ))
    conn.execute("INSERT INTO user_configs VALUES (?, ?, ?)",
                 ("u2", json.dumps({"allowed_sources": ["plaza"]}), "{}"))
    conn.executemany("INSERT INTO app_settings VALUES (?, ?)", [
        ("SOURCES", "codomo,ourcampus,plaza"),
        ("SHADOW_SOURCES", "ourcampus"),
        ("OURCAMPUS_CITIES", "OurCampus Amsterdam Diemen,diemen"),
        ("OURDOMAIN_CITIES", "Amsterdam Diemen,diemen"),
    ])
    conn.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("backoff:circuit_ourcampus:until", "123"),
        ("watchdog_fired:source_down:ourcampus", "1"),
        ("backoff:circuit_ourdomain:until", "9"),
        ("registry_targets_seen",
         json.dumps({"OURCAMPUS_CITIES": ["OurCampus Amsterdam Diemen,diemen"],
                     "PLAZA_CITIES": ["x"]})),
    ])
    return conn


def test_listings_move_to_canvas_with_new_names():
    conn = _db()
    stats = rename_ourcampus(conn)
    assert stats["listings"] == 1
    row = conn.execute(
        "SELECT source, city, name, features, url FROM listings WHERE id='oc_1'").fetchone()
    assert row[:3] == ("canvas", "Canvas Amsterdam Diemen", "Canvas Diemen #3164")
    assert json.loads(row[3]) == ["Building: Canvas Amsterdam Diemen"]
    assert "new-ourcampus" in row[4], "url 是当时的真实地址，不改"
    assert conn.execute("SELECT source, city FROM listings WHERE id='od_1'").fetchone() == (
        "ourdomain", "Amsterdam Diemen")


def test_id_prefix_is_kept():
    """改前缀会让全部房源下一轮被当成新房源推送。"""
    conn = _db()
    rename_ourcampus(conn)
    assert [r[0] for r in conn.execute("SELECT id FROM listings WHERE source='canvas'")] == ["oc_1"]


def test_round_stats():
    conn = _db()
    rename_ourcampus(conn)
    assert sorted(r[0] for r in conn.execute("SELECT source FROM round_stats")) == [
        "canvas", "plaza"]


def test_user_filters_and_credentials():
    conn = _db()
    rename_ourcampus(conn)
    lf, ab = conn.execute(
        "SELECT listing_filter_json, auto_book_json FROM user_configs WHERE id='u1'").fetchone()
    lf, ab = json.loads(lf), json.loads(ab)
    assert lf["allowed_sources"] == ["canvas", "xior"]  # 去重
    assert lf["allowed_cities"] == ["Canvas Amsterdam Diemen", "Amsterdam"]
    assert ab["listing_filter"]["allowed_sources"] == ["canvas"]
    assert ab["canvas_email"] == "a@b.nl"
    assert ab["canvas_password"] == "gAAAA-cipher", "密文原样搬"
    assert "ourcampus_email" not in ab and "ourcampus_password" not in ab
    assert json.loads(conn.execute(
        "SELECT listing_filter_json FROM user_configs WHERE id='u2'").fetchone()[0]) == {
        "allowed_sources": ["plaza"]}


def test_settings():
    conn = _db()
    rename_ourcampus(conn)
    got = dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
    assert got["SOURCES"] == "codomo,canvas,plaza"
    assert got["SHADOW_SOURCES"] == "canvas"
    assert got["CANVAS_CITIES"] == "Canvas Amsterdam Diemen,diemen"
    assert "OURCAMPUS_CITIES" not in got
    assert got["OURDOMAIN_CITIES"] == "Amsterdam Diemen,diemen"


def test_existing_new_key_wins():
    conn = _db()
    conn.execute("INSERT INTO app_settings VALUES ('CANVAS_CITIES', 'keep')")
    rename_ourcampus(conn)
    got = dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
    assert got["CANVAS_CITIES"] == "keep" and "OURCAMPUS_CITIES" not in got


def test_old_portal_runtime_state_is_dropped():
    conn = _db()
    rename_ourcampus(conn)
    keys = {k for (k,) in conn.execute("SELECT key FROM meta")}
    assert "backoff:circuit_ourcampus:until" not in keys
    assert "watchdog_fired:source_down:ourcampus" not in keys
    assert not any("canvas" in k for k in keys - {"registry_targets_seen"})
    assert "backoff:circuit_ourdomain:until" in keys
    seen = json.loads(conn.execute(
        "SELECT value FROM meta WHERE key='registry_targets_seen'").fetchone()[0])
    assert seen == {"CANVAS_CITIES": ["Canvas Amsterdam Diemen,diemen"], "PLAZA_CITIES": ["x"]}


def test_storage_runs_it_once(tmp_path):
    from storage import Storage

    s = Storage(tmp_path / "t.db")
    try:
        assert s.get_meta(MIGRATION_KEY, default="")
    finally:
        s.close()


def test_users_loader_reads_legacy_credential_keys():
    """迁移前写入的配置在过渡期也不能读成空。"""
    import users

    cfg = users._ab_from_dict({"ourcampus_email": "x@y.nl"})
    assert cfg.canvas_email == "x@y.nl"
    assert users._ab_from_dict({"canvas_email": "n@y.nl", "ourcampus_email": "o@y.nl"}).canvas_email == "n@y.nl"
