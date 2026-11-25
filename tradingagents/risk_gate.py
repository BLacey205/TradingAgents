"""Deterministic risk gate for defined-risk credit-spread income trading.

Encodes the entry and exit rules from docs/options-income-strategy.md as plain
functions with no LLM or network dependency, so they can be unit-tested and
reused by an agent, a backtest, or a manual checklist.

Rules (all configurable through ``RiskConfig``):

Entry
- one position's max loss <= ``max_trade_risk_pct`` of the account
- combined max loss of all open positions <= ``max_total_risk_pct``
- at most ``max_open_positions`` open, and ``max_per_sector`` per sector
- days to expiration within [``min_dte``, ``max_dte``]
- IV rank >= ``min_iv_rank`` and credit >= ``min_credit`` per share
- no earnings date between today and expiration

Exit (checked in this order)
- stop loss: spread value >= ``loss_stop_multiple`` x credit
- take profit: spread value <= (1 - ``profit_take_pct``) x credit
- earnings: earnings within ``earnings_exit_days`` days
- expiration: at or inside ``min_exit_dte`` days
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Sequence

CONTRACT_MULTIPLIER = 100
_EPS = 1e-9  # tolerance so exact-boundary prices trigger despite float error


@dataclass(frozen=True)
class RiskConfig:
    account_size: float = 1000.0
    max_trade_risk_pct: float = 0.10
    max_total_risk_pct: float = 0.20
    max_open_positions: int = 2
    max_per_sector: int = 1
    min_dte: int = 30
    max_dte: int = 45
    min_iv_rank: float = 50.0
    min_credit: float = 0.20
    max_bid_ask: float = 0.05
    profit_take_pct: float = 0.80
    loss_stop_multiple: float = 2.0
    earnings_exit_days: int = 2
    min_exit_dte: int = 5


@dataclass(frozen=True)
class CreditSpread:
    underlying: str
    side: Literal["put", "call"]  # bull put spread / bear call spread
    short_strike: float
    long_strike: float
    credit: float  # per share, received at entry
    expiration: date
    contracts: int = 1
    sector: str = ""
    iv_rank: float | None = None
    bid_ask: float | None = None  # width of the spread's bid/ask, per share
    earnings_date: date | None = None

    def __post_init__(self):
        if self.contracts < 1:
            raise ValueError("contracts must be >= 1")
        if self.credit <= 0:
            raise ValueError("credit must be positive")
        if self.side == "put" and not self.short_strike > self.long_strike:
            raise ValueError("bull put spread needs short strike above long strike")
        if self.side == "call" and not self.short_strike < self.long_strike:
            raise ValueError("bear call spread needs short strike below long strike")
        if self.credit >= self.width:
            raise ValueError("credit must be less than the spread width")

    @property
    def width(self) -> float:
        return abs(self.short_strike - self.long_strike)

    @property
    def max_loss_per_contract(self) -> float:
        return (self.width - self.credit) * CONTRACT_MULTIPLIER

    @property
    def max_loss(self) -> float:
        return self.max_loss_per_contract * self.contracts


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    contracts: int
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExitSignal:
    action: Literal["hold", "take_profit", "stop_loss", "close_before_earnings", "close_expiring"]
    reason: str


def total_open_risk(open_positions: Sequence[CreditSpread]) -> float:
    return sum(p.max_loss for p in open_positions)


def evaluate_entry(
    spread: CreditSpread,
    open_positions: Sequence[CreditSpread],
    cfg: RiskConfig,
    today: date,
) -> EntryDecision:
    """Decide whether to open ``spread`` and how many contracts fit the caps.

    The spread's own ``contracts`` is treated as a request; the returned
    ``contracts`` is the largest count that satisfies the risk caps (never more
    than requested). Any failed rule rejects the trade with a reason.
    """
    reasons: list[str] = []

    if len(open_positions) >= cfg.max_open_positions:
        reasons.append(f"already {len(open_positions)} open positions (max {cfg.max_open_positions})")

    if spread.sector and sum(1 for p in open_positions if p.sector == spread.sector) >= cfg.max_per_sector:
        reasons.append(f"sector '{spread.sector}' already at its limit of {cfg.max_per_sector}")

    dte = (spread.expiration - today).days
    if not cfg.min_dte <= dte <= cfg.max_dte:
        reasons.append(f"{dte} DTE is outside {cfg.min_dte}-{cfg.max_dte}")

    if spread.iv_rank is None:
        reasons.append("IV rank is unknown")
    elif spread.iv_rank < cfg.min_iv_rank:
        reasons.append(f"IV rank {spread.iv_rank:g} is below {cfg.min_iv_rank:g}")

    if spread.credit < cfg.min_credit:
        reasons.append(f"credit {spread.credit:.2f} is below minimum {cfg.min_credit:.2f}")

    if spread.bid_ask is not None and spread.bid_ask > cfg.max_bid_ask:
        reasons.append(f"bid/ask {spread.bid_ask:.2f} is wider than {cfg.max_bid_ask:.2f}")

    if spread.earnings_date is not None and today <= spread.earnings_date <= spread.expiration:
        reasons.append(f"earnings on {spread.earnings_date} fall before expiration")

    trade_cap = cfg.account_size * cfg.max_trade_risk_pct
    total_cap = cfg.account_size * cfg.max_total_risk_pct
    room = min(trade_cap, total_cap - total_open_risk(open_positions))
    per_contract = spread.max_loss_per_contract
    fit = max(0, math.floor(round(room, 6) / per_contract)) if room > 0 else 0
    contracts = min(spread.contracts, fit)
    if contracts < 1:
        reasons.append(
            f"max loss ${per_contract:.2f}/contract does not fit the risk caps "
            f"(room ${max(room, 0):.2f})"
        )

    return EntryDecision(allowed=not reasons, contracts=contracts if not reasons else 0, reasons=reasons)


def evaluate_exit(
    spread: CreditSpread,
    current_value: float,
    cfg: RiskConfig,
    today: date,
) -> ExitSignal:
    """Decide whether to close an open spread.

    ``current_value`` is what it costs per share to buy the spread back now.
    """
    if current_value >= spread.credit * cfg.loss_stop_multiple - _EPS:
        return ExitSignal("stop_loss", f"value {current_value:.2f} reached {cfg.loss_stop_multiple:g}x credit")

    if current_value <= spread.credit * (1 - cfg.profit_take_pct) + _EPS:
        return ExitSignal("take_profit", f"captured {cfg.profit_take_pct:.0%} of the credit")

    if spread.earnings_date is not None:
        days = (spread.earnings_date - today).days
        if 0 <= days <= cfg.earnings_exit_days:
            return ExitSignal("close_before_earnings", f"earnings in {days} day(s)")

    if (spread.expiration - today).days <= cfg.min_exit_dte:
        return ExitSignal("close_expiring", f"within {cfg.min_exit_dte} days of expiration")

    return ExitSignal("hold", "no exit rule triggered")
