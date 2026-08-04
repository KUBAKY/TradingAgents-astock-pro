"""统一分析结果注册表（全方式关联索引 + 对比 + 验证 + 误差反馈）。"""

from tradingagents.analysis_registry.registry import (
    KINDS,
    backfill_index,
    direction_from_rating,
    get_stock_timeline,
    query,
    record_id,
    register,
    register_deep_review_record,
    register_mainline_record,
    register_screen_record,
    register_stock_record,
    registry_dir,
    update_record,
)

__all__ = [
    "KINDS",
    "backfill_index",
    "direction_from_rating",
    "get_stock_timeline",
    "query",
    "record_id",
    "register",
    "register_deep_review_record",
    "register_mainline_record",
    "register_screen_record",
    "register_stock_record",
    "registry_dir",
    "update_record",
]
