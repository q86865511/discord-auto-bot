"""排程 loop:hourly / daily / gambling / nekomusume / transfer / digest。

每個 loop 都接收:
- page: Playwright Page
- state: BotState
- config_provider: callable() → BotConfig(讓 loop 每次拿最新設定)
- on_config_save: async callable(BotConfig) → None
"""
