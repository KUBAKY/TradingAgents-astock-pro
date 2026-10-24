"""组合风控：把体系 7.5（风险预算仓位）与 7.6（组合约束）落成可执行规则。

核心公式（7.5）：
    仓位% = min(单票上限, R ÷ 止损幅度)

组合约束（7.6）：
    Σ(仓位_i × 止损幅度_i) ≤ 6%    组合风险预算
    单行业 ≤ 30%（情绪/主题 ≤ 20%）
    高分相关持仓合计 ≤ 单票上限

另有两条来自其他章节的折减：
    软否决（2.9）→ 仓位上限 = 基准 × 1/3
    范式不稳定（3.2）→ 仓位 × 0.5
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Signal:
    """单个标的的目标仓位信号。"""

    symbol: str
    weight: float = 0.0          # 策略给出的目标权重（会被风控收紧）
    stop: float | None = None    # 止损幅度，0.08 表示 8%；None = 无止损位
    industry: str = ""
    theme: str = ""
    soft_veto: bool = False      # 软否决：仓位上限降至 1/3
    unstable: bool = False       # 范式不稳定：仓位 × 0.5


@dataclass
class RiskConfig:
    capital: float = 1_000_000
    max_single_weight: float = 0.30         # 单票上限（中型资金默认）
    portfolio_risk_cap: float = 0.06        # 组合风险预算
    industry_cap: float = 0.30
    theme_cap_multiplier: float = 2.0       # 同主题合计 ≤ 单票上限 × 2
    soft_veto_factor: float = 1 / 3
    unstable_factor: float = 0.5
    r_override: float | None = None         # 显式覆盖 R
    notes: list[str] = field(default_factory=list)


def tier_of(capital: float) -> tuple[str, float]:
    """资金分层 → (层级, R)。与体系 7.5 一致。"""
    if capital < 100_000:
        return "微型", 0.020
    if capital < 500_000:
        return "小型", 0.015
    if capital < 2_000_000:
        return "中型", 0.010
    if capital < 10_000_000:
        return "大型", 0.005
    return "超大型", 0.003


def apply_risk(signals: list[Signal], equity: float, cfg: RiskConfig) -> tuple[dict[str, float], list[str]]:
    """把信号转成最终目标权重，并返回生效的约束说明（用于回测报告）。"""
    notes: list[str] = []
    tier, r = tier_of(cfg.capital)
    if cfg.r_override is not None:
        r = cfg.r_override
        notes.append(f"R 显式覆盖为 {r*100:.2f}%")

    weights: dict[str, float] = {}
    meta: dict[str, Signal] = {}
    for s in signals:
        if s.weight <= 0:
            continue
        if not s.stop or s.stop <= 0:
            # 7.5：没有止损位就不允许给仓位
            notes.append(f"{s.symbol} 无止损位 → 仓位 0")
            continue
        w = min(s.weight, cfg.max_single_weight, r / s.stop)
        if s.soft_veto:
            w = min(w, cfg.max_single_weight * cfg.soft_veto_factor)
            notes.append(f"{s.symbol} 软否决 → 上限降至 {cfg.soft_veto_factor:.2f}×")
        if s.unstable:
            w *= cfg.unstable_factor
            notes.append(f"{s.symbol} 范式不稳定 → 仓位 ×{cfg.unstable_factor}")
        weights[s.symbol] = w
        meta[s.symbol] = s

    # 组合风险预算：Σ(w × stop) ≤ cap
    #
    # 注意一个恒等性质：按 w = R / stop 定仓时，每个持仓贡献的风险恰好是 R，
    # 于是 Σ(w×stop) = N × R。**这条约束实际等价于「最多 cap/R 个持仓」**
    # （R=1% → 6 只；微型资金 R=2% → 3 只）。超过时应缩仓而不是拒绝建仓。
    risk = sum(weights[s] * meta[s].stop for s in weights)
    if risk > cfg.portfolio_risk_cap > 0:
        scale = cfg.portfolio_risk_cap / risk
        weights = {s: w * scale for s, w in weights.items()}
        notes.append(f"组合风险 {risk*100:.2f}% > {cfg.portfolio_risk_cap*100:.0f}% → 整体缩放 ×{scale:.3f}")

    # 行业集中度
    for industry in {meta[s].industry for s in weights if meta[s].industry}:
        members = [s for s in weights if meta[s].industry == industry]
        cap = cfg.industry_cap
        total = sum(weights[s] for s in members)
        if total > cap:
            k = cap / total
            for s in members:
                weights[s] *= k
            notes.append(f"行业「{industry}」{total*100:.1f}% > {cap*100:.0f}% → 组内缩放 ×{k:.3f}")

    # 同主题合计 ≤ 单票上限 × multiplier
    for theme in {meta[s].theme for s in weights if meta[s].theme}:
        members = [s for s in weights if meta[s].theme == theme]
        cap = cfg.max_single_weight * cfg.theme_cap_multiplier
        total = sum(weights[s] for s in members)
        if total > cap:
            k = cap / total
            for s in members:
                weights[s] *= k
            notes.append(f"主题「{theme}」{total*100:.1f}% > {cap*100:.0f}% → 组内缩放 ×{k:.3f}")

    return {s: round(w, 6) for s, w in weights.items() if w > 1e-6}, notes
