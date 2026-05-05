"""Discord Auto Bot — 模組化 package。

子套件：
- bot.slot   — Slot embed 解析（parsers）+ 累計分析 + 持久化（analysis）
- bot.web    — localhost / LAN dashboard

main.py 是 entry point，會 from bot.slot.parsers / bot.slot.analysis / bot.web.dashboard import 所需項目。
"""
