"""
資金管理 — 指南書 Step 7

戦略:
  fixed        : 常に固定賭け金
  proportional : 期待値に比例
  quarter_kelly: 1/4 Kelly（推奨 — 破産リスク最小）

上限・停止条件:
  - 1レース投資上限
  - 1日投資上限
  - 連敗停止
  - ドローダウン制限
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from src.utils.logger import get_logger

logger = get_logger(__name__)

Strategy = Literal["fixed", "proportional", "quarter_kelly"]


@dataclass
class BankrollState:
    bankroll: float
    day_invested: float = 0.0
    consecutive_losses: int = 0
    peak_bankroll: float = field(init=False)
    stopped: bool = False
    stop_reason: str = ""

    def __post_init__(self):
        self.peak_bankroll = self.bankroll

    @property
    def drawdown(self) -> float:
        if self.peak_bankroll == 0:
            return 0.0
        return (self.peak_bankroll - self.bankroll) / self.peak_bankroll

    def update_after_bet(self, invested: float, returned: float) -> None:
        self.bankroll += returned - invested
        self.day_invested += invested
        if self.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.bankroll
        if returned == 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def reset_day(self) -> None:
        self.day_invested = 0.0

    def check_stop(self, config: dict) -> bool:
        mm = config["money_management"]
        if self.consecutive_losses >= mm["max_consecutive_losses_stop"]:
            self.stopped = True
            self.stop_reason = f"連敗{self.consecutive_losses}回"
        if self.drawdown >= mm["max_drawdown_stop"]:
            self.stopped = True
            self.stop_reason = f"ドローダウン{self.drawdown*100:.1f}%"
        return self.stopped


class MoneyManager:
    def __init__(self, config: dict):
        mm = config["money_management"]
        self.strategy: Strategy = mm.get("strategy", "quarter_kelly")
        self.fixed_amount: int = int(mm.get("fixed_bet_amount", 200))
        self.max_per_race: int = int(mm.get("max_bet_per_race", 2000))
        self.max_per_day: int = int(mm.get("max_bet_per_day", 10000))
        self.initial_bankroll: float = float(mm.get("initial_bankroll", 100000))
        self.config = config

    def calc_bet_amount(
        self,
        ev: float,
        model_prob: float,
        odds: float,
        state: BankrollState,
    ) -> int:
        """1点あたりの賭け金を計算する（100円単位）。"""
        if state.stopped:
            return 0
        if state.day_invested >= self.max_per_day:
            return 0

        remaining_day = self.max_per_day - state.day_invested

        if self.strategy == "fixed":
            amount = self.fixed_amount

        elif self.strategy == "proportional":
            # EV に比例: EV が高いほど多く賭ける
            amount = self.fixed_amount * max(1.0, ev - 1.0) * 2

        elif self.strategy == "quarter_kelly":
            # Kelly: f = (bp - q) / b  where b = odds - 1, p = model_prob, q = 1 - p
            b = odds - 1.0
            p = model_prob
            q = 1.0 - p
            if b <= 0 or p <= 0:
                return 0
            f_full = (b * p - q) / b
            f_quarter = max(0, f_full / 4)
            amount = state.bankroll * f_quarter
        else:
            amount = self.fixed_amount

        # 上限適用
        amount = min(amount, self.max_per_race, remaining_day, state.bankroll)
        # 100円単位に丸め（最低100円）
        amount = max(100, int(amount // 100) * 100)
        return amount

    def new_state(self) -> BankrollState:
        return BankrollState(bankroll=self.initial_bankroll)
