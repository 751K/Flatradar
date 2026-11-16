"""预订路径上「失败没说清楚」的回归保护。

预订是这个项目里后果最重的代码：它替用户做出真实承诺。共同点是
**把一个有后果的中间状态报成了一个无后果的结论**。
"""
from __future__ import annotations

import inspect
import re


def _code(fn) -> str:
    """源码，剥掉注释——断言里引用的坏写法常常也出现在解释性注释里。"""
    return "\n".join(re.sub(r"#.*$", "", ln)
                     for ln in inspect.getsource(fn).split("\n"))


class TestSentinelYear:
    """`01-01-2050` 是「没有下一个合同起始日」的哨兵，不是入住日。"""

    def test_sentinel_year_is_the_judge_not_an_exact_date(self):
        """哨兵换个写法（2099、2050-12-31）时不该原样透出去。"""
        from models import is_sentinel_available_from as sent

        assert sent("2050-01-01") and sent("2050-12-31") and sent("2099-06-01")
        assert not sent("2026-09-02") and not sent("") and not sent(None)


class TestRentCafeConstructorInsideTry:
    def test_session_is_constructed_inside_try(self):
        """构造在 try 外面的话，异常会穿到 monitor 的 `await future`，
        打断**后续所有用户**的预订结果处理。"""
        from bookers import rentcafe

        whole = _code(rentcafe)
        i = whole.index("RentCafeSession(self._api_key")
        before = whole[:i]
        # 往回找最近的 try / def，try 必须更近
        assert before.rindex("try:") > before.rindex("def "), (
            "RentCafeSession(...) 在 try 之外")


class TestPerUserBookingIsolation:
    def test_await_future_is_guarded(self):
        """一个用户的异常不该带走所有人——这个循环还负责发通知、更新重试队列。"""
        from mcore import booking_round

        assert "_process_single_booking(" in _code(booking_round.process_booking_results)
        src = _code(booking_round._process_single_booking)
        # 必须带墙钟上限：线程卡死时裸 await 会让整轮（以及后面排队的用户）
        # 永远等下去。异常隔离仍然要在这层 try 里。
        i = src.index("result = await asyncio.wait_for(")
        assert src.rindex("try:", 0, i) > src.rindex("def ", 0, i), \
            "wait_for 不在 try 里"
        window = src[i:i + 1200]
        assert "except asyncio.TimeoutError:" in window
        assert "except Exception:" in window and "return" in window
