"""Web 加固：严格 CSP（nonce）、退出登录清缓存、session cookie 的 Secure 默认值、
通知里的房源链接过滤。

这些都是「坏了也不会报错」的防线：CSP 回退成 'unsafe-inline'、模板里又写回
onclick=、退出后 bfcache 还留着上一个人的页面——页面照常能用，只是保护没了。
所以只能靠测试守住。
"""
from __future__ import annotations

import pathlib
import re

import pytest
from flask import url_for

from models import Listing

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "templates"

# 管理员能打开的页面，覆盖 base.html 及各页自带的内联脚本
ADMIN_PAGES = ["/", "/system", "/logs", "/crashes", "/announcements", "/monitoring",
               "/settings", "/users", "/stats", "/map", "/calendar"]
PUBLIC_PAGES = ["/login"]


def _script_policy(resp) -> str:
    csp = resp.headers["Content-Security-Policy"]
    return next(d.strip() for d in csp.split(";") if d.strip().startswith("script-src"))


class TestStrictCsp:

    def test_script_src_uses_nonce_and_strict_dynamic(self, client):
        policy = _script_policy(client.get("/login"))
        assert re.search(r"'nonce-[A-Za-z0-9_-]{16,}'", policy)
        assert "'strict-dynamic'" in policy
        assert "object-src 'none'" in client.get("/login").headers["Content-Security-Policy"]

    def test_nonce_changes_per_request(self, client):
        assert _script_policy(client.get("/login")) != _script_policy(client.get("/login"))

    @pytest.mark.parametrize("path", PUBLIC_PAGES + ADMIN_PAGES)
    def test_every_script_tag_carries_the_response_nonce(self, admin_client, client, path):
        c = client if path in PUBLIC_PAGES else admin_client
        resp = c.get(path)
        if resp.status_code != 200:
            pytest.skip(f"{path} -> {resp.status_code}")
        nonce = re.search(r"'nonce-([^']+)'", _script_policy(resp)).group(1)
        html = resp.get_data(as_text=True)
        for tag in re.findall(r"<script\b[^>]*>", html):
            if 'type="application/json"' in tag:
                continue  # 数据块，不执行
            assert f'nonce="{nonce}"' in tag, f"{path}: 没带 nonce 的脚本会被 CSP 拦掉: {tag}"

    def test_templates_have_no_inline_event_handlers(self):
        """nonce 策略下 onclick= 之类的属性不会执行；写回去等于按钮失灵。"""
        offenders = []
        pattern = re.compile(r"<[^>]*\son[a-z]+\s*=", re.I)
        for path in TEMPLATES.rglob("*.html"):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.name}:{n}: {line.strip()[:100]}")
        assert not offenders, "改用 addEventListener / data-* 绑定：\n" + "\n".join(offenders)


class TestLogout:

    def test_logout_clears_site_cache(self, admin_client, test_app):
        with test_app.test_request_context():
            path = url_for("logout")
        resp = admin_client.post(path, data={"csrf_token": "test_csrf"},
                                 headers={"X-CSRF-Token": "test_csrf"})
        assert resp.status_code in (302, 303)
        # 只清 cache：cookies/storage 会顺带删掉语言和主题偏好
        assert resp.headers.get("Clear-Site-Data") == '"cache"'

    def test_html_is_revalidated_and_not_shared(self, client):
        cc = client.get("/login").headers.get("Cache-Control", "")
        assert "no-cache" in cc and "private" in cc
        assert "no-store" not in cc  # no-store 会禁用 bfcache


class TestSessionCookieSecure:

    @pytest.fixture
    def resolve(self, test_app, monkeypatch):
        from web import _session_cookie_secure

        def run(public_base_url, explicit):
            monkeypatch.setenv("PUBLIC_BASE_URL", public_base_url)
            if explicit is None:
                monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
            else:
                monkeypatch.setenv("SESSION_COOKIE_SECURE", explicit)
            return _session_cookie_secure()
        return run

    def test_auto_on_for_https_public_url(self, resolve):
        assert resolve("https://flat.example", None) is True
        assert resolve("https://flat.example", "") is True

    def test_auto_off_for_local_use(self, resolve):
        assert resolve("", None) is False
        assert resolve("http://127.0.0.1:8088", None) is False

    def test_explicit_value_wins(self, resolve):
        assert resolve("", "true") is True
        assert resolve("https://flat.example", "false") is False

    def test_explicit_false_behind_https_is_logged(self, resolve, caplog):
        with caplog.at_level("WARNING"):
            resolve("https://flat.example", "false")
        assert "SESSION_COOKIE_SECURE" in caplog.text


class TestNotificationLinks:

    def _listing(self, url):
        return Listing(id="x1", name="Studio", status="Available to book", price_raw="€700",
                       available_from="2026-12-01", features=["Area: 26 m²"], url=url,
                       city="Eindhoven", source="xior")

    def test_new_listing_message_drops_script_url(self):
        from notifier import _format_new
        assert "javascript:" not in _format_new(self._listing("javascript:alert(1)"))

    def test_new_listing_message_keeps_https_url(self):
        from notifier import _format_new
        assert "https://example.com/x1" in _format_new(self._listing("https://example.com/x1"))
