from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AssetConfig:
    symbol: str
    tier: str
    default_enabled: bool
    notes: str


@dataclass(frozen=True)
class TimeframeConfig:
    name: str
    interval: str
    role: str


@dataclass(frozen=True)
class IndicatorConfig:
    key: str
    hfd_name: str
    english_name: str
    family: str
    status: str
    role: str
    internal_aliases: tuple[str, ...] = ()


ASSETS: dict[str, AssetConfig] = {
    "BTC": AssetConfig("BTC", "core", True, "market benchmark"),
    "ETH": AssetConfig("ETH", "core", True, "second benchmark"),
    "SOL": AssetConfig("SOL", "mainstream", True, "trend and sweep candidate"),
    "BNB": AssetConfig("BNB", "mainstream", True, "relatively stable mainstream asset"),
    "LINK": AssetConfig("LINK", "mainstream", True, "often clear mid-term structure"),
    "TON": AssetConfig("TON", "mainstream", True, "watch liquidity and slippage"),
    "DOGE": AssetConfig("DOGE", "high_volatility", True, "sweep-prone, reduce size"),
    "HYPE": AssetConfig("HYPE", "high_volatility", True, "paper trading first"),
    "ZEC": AssetConfig("ZEC", "high_volatility", True, "explosive moves, avoid chasing"),
}

TIMEFRAMES: dict[str, TimeframeConfig] = {
    "short": TimeframeConfig("short", "30m", "trigger and entry"),
    "mid": TimeframeConfig("mid", "1h", "position and structure"),
    "long": TimeframeConfig("long", "4h", "direction and risk filter"),
}

CORE_INDICATORS: tuple[str, ...] = (
    "smart_money_cost",
    "trend_price",
    "inst_vwap",
    "liq_heatmap",
    "liquidation_fuel",
    "liquidity_sweep",
    "inst_volume_profile",
    "hvn_nodes",
    "micro_poc",
    "cross_exchange_resonance",
    "imbalance",
    "trend_exhaustion",
)

EXPERIMENT_INDICATORS: tuple[str, ...] = (
    "fair_value_gap",
    "cascade_liquidation_zones",
    "retail_stop_loss",
    "inst_choch",
    "trend_purity",
    "liquidity_vacuum",
)

COLLECTABLE_INDICATORS: tuple[str, ...] = tuple(
    dict.fromkeys((*CORE_INDICATORS, *EXPERIMENT_INDICATORS))
)


HFD_INDICATORS: dict[str, IndicatorConfig] = {
    "smart_money_cost": IndicatorConfig(
        "smart_money_cost",
        "趋势成本带",
        "Smart Money Cost",
        "structure",
        "scoring",
        "direction_and_levels",
        ("长期方向", "中期结构", "短期触发", "中期位置", "短期位置"),
    ),
    "liq_heatmap": IndicatorConfig(
        "liq_heatmap",
        "清算热力图",
        "Liquidation Heatmap",
        "liquidity",
        "scoring",
        "target_and_stop_context",
        ("清算流动性",),
    ),
    "liquidation_fuel": IndicatorConfig(
        "liquidation_fuel",
        "密集博弈",
        "Liquidation Fuel",
        "liquidity",
        "research",
        "squeeze_risk",
        ("清算流动性",),
    ),
    "liquidity_sweep": IndicatorConfig(
        "liquidity_sweep",
        "极限洗盘深度",
        "Liquidity Sweep",
        "liquidity",
        "research",
        "sweep_filter",
        ("清算流动性",),
    ),
    "cross_exchange_resonance": IndicatorConfig(
        "cross_exchange_resonance",
        "主力大单行动",
        "Cross Exchange Resonance",
        "orderflow",
        "scoring",
        "trigger_quality",
        ("订单流确认",),
    ),
    "imbalance": IndicatorConfig(
        "imbalance",
        "订单簿衰减",
        "Imbalance",
        "orderflow",
        "scoring",
        "orderflow_balance",
        ("订单流确认",),
    ),
    "trend_exhaustion": IndicatorConfig(
        "trend_exhaustion",
        "趋势时间极限",
        "Trend Exhaustion",
        "exhaustion",
        "scoring",
        "late_trend_filter",
        ("趋势耗竭",),
    ),
    "trend_price": IndicatorConfig(
        "trend_price",
        "趋势撑压",
        "Trend Price",
        "structure",
        "research",
        "support_resistance",
    ),
    "inst_vwap": IndicatorConfig(
        "inst_vwap",
        "趋势动态防线",
        "Trailing VWAP",
        "institutional_flow",
        "research",
        "institutional_cost_line",
    ),
    "inst_volume_profile": IndicatorConfig(
        "inst_volume_profile",
        "筹码分布",
        "Volume Profile",
        "volume_profile",
        "research",
        "volume_nodes",
    ),
    "hvn_nodes": IndicatorConfig(
        "hvn_nodes",
        "高成交节点",
        "HVN Nodes",
        "volume_profile",
        "research",
        "volume_nodes",
    ),
    "micro_poc": IndicatorConfig(
        "micro_poc",
        "微观成本线",
        "Micro POC",
        "volume_profile",
        "research",
        "micro_cost_line",
    ),
    "fair_value_gap": IndicatorConfig(
        "fair_value_gap",
        "筹码真空区",
        "Fair Value Gap",
        "inefficiency",
        "experiment",
        "gap_magnet_or_mitigation",
    ),
    "cascade_liquidation_zones": IndicatorConfig(
        "cascade_liquidation_zones",
        "连环爆仓区",
        "Cascade Liquidation Zones",
        "liquidity",
        "experiment",
        "cascade_liquidation_risk",
    ),
    "retail_stop_loss": IndicatorConfig(
        "retail_stop_loss",
        "散户止损点",
        "Retail Stop Loss",
        "liquidity",
        "experiment",
        "retail_stop_cluster",
    ),
    "max_pain": IndicatorConfig(
        "max_pain",
        "极限洗盘深度",
        "Max Pain",
        "liquidity",
        "catalog_only",
        "pain_level",
    ),
    "trend_roi": IndicatorConfig(
        "trend_roi",
        "未来收益预期",
        "Trend ROI",
        "expectancy",
        "catalog_only",
        "forward_expectancy",
    ),
    "max_drawdown_tolerance": IndicatorConfig(
        "max_drawdown_tolerance",
        "涨跌极限",
        "Max Drawdown Tolerance",
        "risk",
        "catalog_only",
        "drawdown_limit",
    ),
    "time_exhaustion": IndicatorConfig(
        "time_exhaustion",
        "趋势时间极限",
        "Time Exhaustion",
        "exhaustion",
        "catalog_only",
        "time_limit",
    ),
    "volume_exhaustion": IndicatorConfig(
        "volume_exhaustion",
        "趋势资金阈值",
        "Volume Exhaustion",
        "exhaustion",
        "catalog_only",
        "volume_limit",
    ),
    "inst_choch": IndicatorConfig(
        "inst_choch",
        "破坏与突破",
        "Inst CHoCH",
        "structure",
        "experiment",
        "structure_break",
    ),
    "ob_decay": IndicatorConfig(
        "ob_decay",
        "订单墙衰减",
        "OB Decay",
        "orderflow",
        "catalog_only",
        "order_block_decay",
    ),
    "trend_purity": IndicatorConfig(
        "trend_purity",
        "趋势筹码纯度",
        "Trend Purity",
        "structure",
        "experiment",
        "trend_cleanliness",
    ),
    "poc_shift": IndicatorConfig(
        "poc_shift",
        "均价重心偏移",
        "POC Shift",
        "volume_profile",
        "catalog_only",
        "poc_migration",
    ),
    "trailing_vwap": IndicatorConfig(
        "trailing_vwap",
        "趋势动态防线",
        "Trailing VWAP",
        "institutional_flow",
        "catalog_only",
        "dynamic_vwap_line",
    ),
    "trend_saturation": IndicatorConfig(
        "trend_saturation",
        "趋势进度条",
        "Trend Saturation",
        "exhaustion",
        "catalog_only",
        "trend_saturation",
    ),
    "liquidity_vacuum": IndicatorConfig(
        "liquidity_vacuum",
        "流动性黑洞预警",
        "Liquidity Vacuum",
        "liquidity",
        "experiment",
        "liquidity_vacuum",
    ),
}

REQUIRED_SCORING_INDICATORS: tuple[str, ...] = (
    "smart_money_cost",
    "liq_heatmap",
    "cross_exchange_resonance",
    "imbalance",
    "trend_exhaustion",
)

RESEARCH_INDICATORS: tuple[str, ...] = tuple(
    indicator for indicator in CORE_INDICATORS if indicator not in REQUIRED_SCORING_INDICATORS
)

CATALOG_ONLY_INDICATORS: tuple[str, ...] = tuple(
    key for key, item in HFD_INDICATORS.items() if item.status == "catalog_only"
)
