"""
安全工具：URL 重定向校验 + 外部链接 scheme 校验 + .env 注入防护
==========================================

无业务逻辑，可独立单元测试（safe_next_url 需要 Flask app context 才能调用 url_for，
但函数本身行为是纯的）。
"""
from __future__ import annotations

from urllib.parse import urlsplit

from flask import url_for


def safe_next_url(candidate: str) -> str:
    """
    校验重定向目标，防止开放重定向（Open Redirect）攻击。

    规则
    ----
    只允许同源相对路径：必须以 "/" 开头，且不以 "//" 开头。
    - "/dashboard"         → ✅ 合法相对路径
    - "//evil.com/phish"   → ❌ 协议相对 URL，仍指向外部域名
    - "https://evil.com"   → ❌ 绝对 URL，指向外部域名
    - "javascript:alert()" → ❌ 非路径，不以 "/" 开头

    login_required 装饰器通过 next=request.path 注入，request.path
    始终是纯路径（以 "/" 开头，不含 host），是安全来源；本函数用于
    校验来自表单/查询参数（不可信来源）的 next 值。

    Parameters
    ----------
    candidate : 从请求参数中读取的原始 next 字符串

    Returns
    -------
    校验通过的路径原样返回；不通过时返回 "/" 首页路径
    """
    if candidate and candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return url_for("index")


def sanitize_dotenv(value: str) -> str:
    """剥离换行符，防止 .env 注入攻击（\\n 可伪造新键）。"""
    return value.replace("\r", "").replace("\n", "")


# 浏览器解析 href 时会去掉首尾的 C0 控制字符和空格，并删掉中间所有的 tab / 换行，
# 所以 " javascript:..." 和 "java\tscript:..." 在浏览器里都是 javascript: 链接。
# 这里按同样的规则先规范化再判断 scheme，避免被这类写法绕过。
_C0_AND_SPACE = "".join(chr(c) for c in range(0x21))
_TAB_NEWLINE = str.maketrans("", "", "\t\n\r")


def safe_external_url(candidate: object) -> str:
    """
    外部链接（抓取到的房源 URL 等）只放行 http / https，其余一律返回空串。

    房源 URL 来自第三方平台（例如 Xior 接口的 applyOnlineURL），属于不可信
    输入。Jinja 转义挡不住 ``javascript:`` 这类 scheme，一旦进了 href 或
    window.open，点击就在本站源上执行脚本。所有把房源 URL 交给浏览器或
    客户端的出口都应经过这里（serialize_listing 已统一处理）。
    """
    if not isinstance(candidate, str) or not candidate:
        return ""
    url = candidate.strip(_C0_AND_SPACE).translate(_TAB_NEWLINE)
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return ""
    return url if scheme in ("http", "https") else ""
