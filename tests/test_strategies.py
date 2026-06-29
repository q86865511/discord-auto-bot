"""下注策略純函式測試(risk management 計算)。"""

from bot.slot.strategies import (
    compute_drawdown_pct,
    rolling_ev,
    rolling_multiplier,
)


class TestRollingEv:
    def test_loss_only_gives_zero_payout_ratio(self):
        # 押 10 全輸 → 賠付率 0
        assert rolling_ev([{"bet": 10, "change": -10}], 1) == 0.0

    def test_mixed_window(self):
        # (10+5)+(10-2)=23 wagered 20 → 1.15
        hist = [{"bet": 10, "change": 5}, {"bet": 10, "change": -2}]
        assert rolling_ev(hist, 2) == 1.15

    def test_insufficient_samples_returns_none(self):
        assert rolling_ev([{"bet": 10, "change": 5}], 3) is None

    def test_non_positive_window_returns_none(self):
        assert rolling_ev([{"bet": 10, "change": 5}], 0) is None

    def test_zero_wagered_returns_none(self):
        assert rolling_ev([{"bet": 0, "change": 0}], 1) is None


class TestRollingMultiplier:
    def test_none_ev_is_neutral(self):
        assert rolling_multiplier(None, 0.9, 1.1, 0.5, 1.5) == 1.0

    def test_low_ev_reduces_bet(self):
        assert rolling_multiplier(0.8, 0.9, 1.1, 0.5, 1.5) == 0.5

    def test_high_ev_raises_bet(self):
        assert rolling_multiplier(1.2, 0.9, 1.1, 0.5, 1.5) == 1.5

    def test_mid_band_is_neutral(self):
        assert rolling_multiplier(1.0, 0.9, 1.1, 0.5, 1.5) == 1.0


class TestComputeDrawdownPct:
    def test_drawdown_from_peak(self):
        # 累計淨收 100 後回到 70 → drawdown 30%
        dd, peak, current = compute_drawdown_pct([{"change": 100}, {"change": -30}])
        assert (dd, peak, current) == (30.0, 100, 70)

    def test_never_profitable_does_not_trigger(self):
        # 從未進入正收益 → drawdown 0%(trailing stop 不該觸發)
        assert compute_drawdown_pct([{"change": -50}]) == (0.0, 0, -50)

    def test_empty_history(self):
        assert compute_drawdown_pct([]) == (0.0, 0, 0)
