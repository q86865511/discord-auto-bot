"""Slot 餘額解析純函式測試。"""

from bot.slot.parsers import parse_balance_int


class TestParseBalanceInt:
    def test_half_width_comma(self):
        assert parse_balance_int("1,234") == 1234

    def test_full_width_comma(self):
        # 全形逗號(中文輸入常見)也要能解析
        assert parse_balance_int("1，234") == 1234

    def test_plain_integer(self):
        assert parse_balance_int("567") == 567

    def test_non_numeric_returns_none(self):
        assert parse_balance_int("abc") is None

    def test_empty_returns_none(self):
        assert parse_balance_int("") is None
