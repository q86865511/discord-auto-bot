"""設定 schema 驗證測試(config validation + dashboard 安全旗標)。"""

from bot.core.config import DashboardConfig, GamblingConfig


class TestGamblingConfigValidate:
    def test_defaults_are_valid(self):
        assert GamblingConfig().validate() == []

    def test_min_bet_below_one_is_rejected(self):
        cfg = GamblingConfig()
        cfg.min_bet = 0
        assert cfg.validate(), "min_bet < 1 應產生錯誤"

    def test_unknown_strategy_is_rejected(self):
        cfg = GamblingConfig()
        cfg.strategy = "bogus"
        assert any("kelly" in e for e in cfg.validate())

    def test_invalid_goal_action_is_rejected(self):
        cfg = GamblingConfig()
        cfg.goal_action = "bogus"
        assert cfg.validate(), "goal_action 非 pause/raise 應產生錯誤"

    def test_negative_loss_floor_is_rejected(self):
        cfg = GamblingConfig()
        cfg.loss_floor = -1
        assert cfg.validate(), "停損點為負應產生錯誤"


class TestDashboardConfig:
    def test_defaults_are_valid(self):
        assert DashboardConfig().validate() == []

    def test_loopback_is_not_lan_open(self):
        assert DashboardConfig().is_lan_open() is False

    def test_bind_all_interfaces_is_lan_open(self):
        cfg = DashboardConfig()
        cfg.host = "0.0.0.0"
        assert cfg.is_lan_open() is True

    def test_out_of_range_port_is_rejected(self):
        cfg = DashboardConfig()
        cfg.port = 99999
        assert cfg.validate(), "port 超範圍應產生錯誤"

    def test_blank_username_is_rejected(self):
        cfg = DashboardConfig()
        cfg.username = "   "
        assert cfg.validate(), "空白帳號應產生錯誤"
