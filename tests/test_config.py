"""
config.py 单元测试：解析默认值 + 坏配置矩阵。
"""
import pytest

import config


@pytest.fixture
def clean_env(monkeypatch):
    """清掉可能来自开发机 .env 的结构化配置，让每条用例只看自己设的值。"""
    for key in (
        "SOURCES", "SHADOW_SOURCES", "CITIES", "CODOMO_CITIES", "OURDOMAIN_CITIES",
        "CANVAS_CITIES", "XIOR_CITIES", "MAGIS_CITIES",
        "STUDENTEXPERIENCE_CITIES", "PLAZA_CITIES", "AVAILABILITY_FILTERS",
        "SHARD_SIZES", "SOURCE_MIN_INTERVALS", "SOURCE_PEAK_MIN_INTERVALS",
        "CHECK_INTERVAL", "PEAK_INTERVAL", "MIN_INTERVAL",
        "HEARTBEAT_INTERVAL_MINUTES", "JITTER_RATIO", "TIMEZONE",
        "PEAK_WEEKDAYS_ONLY",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


class TestGetImpersonate:
    def test_consecutive_never_returns_same(self, monkeypatch):
        """连续两次调用不应返回同一指纹（池大小 > 1 时）。"""
        # 固定随机种子，避免偶发失败
        import random
        monkeypatch.setattr(random, "choices", lambda pool, weights, k: [pool[0]])
        first = config.get_impersonate()
        # 第二次调用：上次选中的值应被排除，所以返回 pool 中下一个
        second = config.get_impersonate()
        assert first != second, f"连续两次返回了相同指纹: {first}"


class TestScrapeTasksV2:
    def test_default_sources_is_codomo(self, clean_env):
        cfg = config.load_config()
        tasks = cfg.scrape_tasks_v2()
        assert cfg.sources == ["codomo"]
        assert tasks
        assert {t.source for t in tasks} == {"codomo"}
        # 默认订阅 Eindhoven（沿用旧 H2S 的数字 city ID 作 key）
        assert [(t.city_display, t.city_key) for t in tasks] == [("Eindhoven", "29")]

    def test_ourdomain_source_adds_ourdomain_tasks(self, clean_env):
        clean_env.setenv("SOURCES", "codomo,ourdomain")
        clean_env.setenv("CODOMO_CITIES", "Eindhoven,29")
        clean_env.setenv("OURDOMAIN_CITIES", "Amsterdam Diemen,diemen")

        cfg = config.load_config()
        tasks = cfg.scrape_tasks_v2()

        assert [t.source for t in tasks] == ["codomo", "ourdomain"]
        assert tasks[0].city_display == "Eindhoven"
        assert tasks[0].city_key == "29"
        assert tasks[1].city_key == "diemen"
        assert tasks[1].city_display == "Amsterdam Diemen"

    def test_ourdomain_only_uses_default_city(self, monkeypatch):
        monkeypatch.setenv("SOURCES", "ourdomain")
        monkeypatch.delenv("OURDOMAIN_CITIES", raising=False)

        cfg = config.load_config()
        tasks = cfg.scrape_tasks_v2()

        assert cfg.sources == ["ourdomain"]
        assert len(tasks) == 1
        assert tasks[0].source == "ourdomain"
        assert tasks[0].city_key == "diemen"


# ── 坏配置矩阵 ──────────────────────────────────────────────────────
#
# 格式错误拒绝启动；实体未知（新平台/新城市）只 WARNING，继续加载。

class TestFatalStructuredConfig:
    @pytest.mark.parametrize("sources,key,raw,match", [
        ("codomo", "CODOMO_CITIES", "Eindhoven", "缺少逗号或 key"),
        ("codomo", "CODOMO_CITIES", ",29", "显示名或 key 为空"),
        ("codomo", "SHARD_SIZES", "xior:abc", "不是整数"),
        ("codomo", "SOURCE_MIN_INTERVALS", "xior:-1", "不能是负数"),
        ("magis", "MAGIS_CITIES", "Eindhoven", "缺少逗号或 key"),
        ("xior", "XIOR_CITIES", "Eindhoven", "缺少逗号或 key"),
    ])
    def test_format_errors_reject_startup(self, clean_env, sources, key, raw, match):
        clean_env.setenv("SOURCES", sources)
        clean_env.setenv(key, raw)
        with pytest.raises(ValueError, match=match):
            config.load_config()

    @pytest.mark.parametrize("key,raw,match", [
        ("CHECK_INTERVAL", "abc", "不是整数"),
        ("PEAK_INTERVAL", "abc", "不是整数"),
        ("MIN_INTERVAL", "abc", "不是整数"),
        ("HEARTBEAT_INTERVAL_MINUTES", "abc", "不是整数"),
        ("TIMEZONE", "Mars/Olympus_Mons", "无效的 IANA"),
    ])
    def test_scalar_errors_reject_startup(self, clean_env, key, raw, match):
        clean_env.setenv(key, raw)
        with pytest.raises(ValueError, match=match):
            config.load_config()


class TestNonFatalConfig:
    def test_unknown_platform_warns_and_is_dropped(self, clean_env, caplog):
        clean_env.setenv("SOURCES", "codomo,xiorr")
        cfg = config.load_config()
        assert cfg.sources == ["codomo"]
        assert "xiorr" in caplog.text

    def test_unknown_target_key_warns_but_is_kept(self, clean_env, caplog):
        clean_env.setenv("SOURCES", "magis")
        clean_env.setenv("MAGIS_CITIES", "Eindhoven,eindhoven|Nowhere,nowhere")
        cfg = config.load_config()
        assert {c.key for c in cfg.magis_cities} == {"eindhoven", "nowhere"}
        assert "nowhere" in caplog.text

    def test_unknown_city_id_warns_but_is_kept(self, clean_env, caplog):
        clean_env.setenv("CODOMO_CITIES", "Eindhoven,29|Nowhere,999999")
        cfg = config.load_config()
        assert {c.name for c in cfg.codomo_cities} == {"Eindhoven", "Nowhere"}
        assert "999999" in caplog.text

    def test_legacy_cities_key_is_still_read(self, clean_env):
        """改名前 .env 里的 CITIES 仍要生效，否则升级后悄悄回落到默认城市。"""
        clean_env.setenv("CITIES", "Amsterdam,24")
        cfg = config.load_config()
        assert [(c.name, c.key) for c in cfg.codomo_cities] == [("Amsterdam", "24")]

    def test_new_key_wins_over_legacy_cities(self, clean_env):
        clean_env.setenv("CITIES", "Amsterdam,24")
        clean_env.setenv("CODOMO_CITIES", "Delft,26")
        cfg = config.load_config()
        assert [c.key for c in cfg.codomo_cities] == ["26"]


class TestScalarClamping:
    def test_min_interval_floor(self, clean_env):
        clean_env.setenv("MIN_INTERVAL", "1")
        assert config.load_config().min_interval == 5

    @pytest.mark.parametrize("raw,want", [("9", 0.5), ("-1", 0.0), ("0.1", 0.1)])
    def test_jitter_ratio_range(self, clean_env, raw, want):
        clean_env.setenv("JITTER_RATIO", raw)
        assert config.load_config().jitter_ratio == want

    def test_heartbeat_cannot_be_negative(self, clean_env):
        clean_env.setenv("HEARTBEAT_INTERVAL_MINUTES", "-5")
        assert config.load_config().heartbeat_interval_minutes == 0

    @pytest.mark.parametrize("raw,want", [
        (" false ", False), ("FALSE", False), ("0", False),
        (" true ", True), ("TRUE", True), ("1", True),
    ])
    def test_bool_strips_and_ignores_case(self, clean_env, raw, want):
        clean_env.setenv("PEAK_WEEKDAYS_ONLY", raw)
        assert config.load_config().peak_weekdays_only is want
