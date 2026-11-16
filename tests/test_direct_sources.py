"""SCRAPE_DIRECT_SOURCES：指定平台永远直连，不走代理池。"""
from __future__ import annotations

import pytest

import config
from mcore.dispatch import apply_proxyless_gate

P1 = "http://u:p@commercial:80"
HOME = "socks5://172.19.0.1:1080"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", HOME)
    monkeypatch.setenv("SCRAPE_PROXIES_PERSONAL", HOME)
    monkeypatch.setenv("SCRAPE_DIRECT_SOURCES", " Magis ,studentexperience")
    config._proxy_cooldown_until.clear()
    config._proxy_failure_marks.clear()
    yield
    config._proxy_cooldown_until.clear()
    config._proxy_failure_marks.clear()


class _T:
    def __init__(self, source):
        self.source = source
        self.city_key = self.city_display = "X"


def test_listed_sources_go_direct_others_use_pool():
    assert config.get_proxy_url("magis") == ""
    assert config.get_proxy_url("studentexperience") == ""
    assert config.get_proxy_url("plaza") == HOME
    assert config.get_proxy_url("codomo") == HOME
    # 不带 source 的调用（monitor 判断「有没有代理 / 是不是家里线路」）不受影响
    assert config.get_proxy_url() == HOME
    assert config.is_personal_proxy_active()


def test_unset_means_everything_uses_pool(monkeypatch):
    monkeypatch.delenv("SCRAPE_DIRECT_SOURCES")
    assert config.get_proxy_url("magis") == HOME


def test_direct_sources_survive_proxy_outage(monkeypatch):
    monkeypatch.setattr(
        "config.is_proxy_native_fallback_active", lambda: True, raising=False)
    kept = apply_proxyless_gate([_T("magis"), _T("xior"), _T("studentexperience")])
    assert [t.source for t in kept] == ["magis", "studentexperience"]


def test_direct_source_failure_does_not_cool_the_proxy():
    from mcore.scrape_round import report_source_proxy_failure

    for _ in range(3):
        report_source_proxy_failure("magis", RuntimeError("Proxy CONNECT aborted"))
    assert not config.is_proxy_in_cooldown(HOME)
    assert config.get_proxy_url("plaza") == HOME
