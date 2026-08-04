"""AI 成本定价表：DeepSeek 官方中文定价页自动刷新（单位：人民币 元/百万 tokens）。

存储: ~/.tradingagents/cost/pricing.json（可用环境变量 TRADINGAGENTS_COST_DIR 覆盖目录）
自动刷新: 缓存超过 PRICE_TTL_DAYS 天时，get_price 触发一次后台拉取（每进程最多一次；
          失败静默回退旧缓存/内置默认，绝不阻塞 LLM 调用）。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# DeepSeek 官方中文定价页（直接给人民币价格，无需汇率换算）
PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"
PRICE_TTL_DAYS = 7
_ARCHIVE_TTL_DAYS = 90

_AUTO_REFRESH_ATTEMPTED = False  # 每进程最多自动刷新一次


class ParseError(ValueError):
    """定价页解析失败。"""


def _load_default() -> dict:
    path = Path(__file__).parent / "deepseek_pricing_default.json"
    return json.loads(path.read_text(encoding="utf-8"))


# 内置兜底价格表（与官方页一致；页面刷新成功后会覆盖）
DEFAULT_PRICING = _load_default()


def cost_dir() -> Path:
    env = os.environ.get("TRADINGAGENTS_COST_DIR")
    if env:
        return Path(env)
    return Path.home() / ".tradingagents" / "cost"


def pricing_path() -> Path:
    return cost_dir() / "pricing.json"


def _http_get(url: str, timeout: int = 20) -> str:
    import requests
    r = requests.get(url, timeout=timeout, headers={
        "User-Agent": "Mozilla/5.0 (TradingAgents-astock cost refresh)",
    })
    r.raise_for_status()
    # 服务端 Content-Type 无 charset（默认 ISO-8859-1）→ 按 apparent 解码，防中文乱码
    enc = r.apparent_encoding or "utf-8"
    return r.content.decode(enc, errors="replace")


def _save(parsed: dict, meta: dict) -> None:
    path = pricing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"pricing": parsed, **meta}, ensure_ascii=False,
                              indent=1), encoding="utf-8")
    tmp.replace(path)


def parse_pricing_page(html: str) -> dict:
    """解析中文定价页 HTML → {model: {input_hit, input_miss, output}}。

    页面为列式表格：每行类别（缓存命中/未命中/输出）+ 每模型一列的价格。
    按模型名首现顺序与类别行后的数字序列对齐取值。
    """
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    models: list[str] = []
    for m in re.finditer(r"deepseek-v4[a-z0-9.-]*", text):
        if m.group(0) not in models:
            models.append(m.group(0))
    if not models:
        raise ParseError("定价页未找到 deepseek-v4 模型名")

    categories = {
        "input_hit": "百万tokens输入（缓存命中）",
        "input_miss": "百万tokens输入（缓存未命中）",
        "output": "百万tokens输出",
    }
    result = {m_: {} for m_ in models}
    for key, label in categories.items():
        pos = text.find(label)
        if pos < 0:
            raise ParseError(f"定价页未找到价格类别: {label}")
        nums = re.findall(r"(\d+(?:\.\d+)?)\s*(?:元|¥)", text[pos:])
        if len(nums) < len(models):
            raise ParseError(f"价格类别 {label} 数值不足（{len(nums)} < {len(models)}）")
        for m_, v in zip(models, nums[:len(models)]):
            result[m_][key] = float(v)
    for m_, v in result.items():
        if len(v) != len(categories):
            raise ParseError(f"模型 {m_} 价格不完整: {v}")
    return result


def load_pricing() -> tuple[dict, dict]:
    """读取缓存价格表 + meta。无缓存/损坏 → 内置默认（stale=True）。"""
    path = pricing_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw.get("pricing"), dict) and raw["pricing"]:
                meta = {
                    "fetched_at": raw.get("fetched_at"),
                    "source": raw.get("source", str(path)),
                    "stale": _is_stale(raw.get("fetched_at")),
                }
                return raw["pricing"], meta
        except (OSError, json.JSONDecodeError):
            pass
    return dict(DEFAULT_PRICING), {"fetched_at": None, "source": "内置默认表", "stale": True}


def _is_stale(fetched_at: Optional[str]) -> bool:
    if not fetched_at:
        return True
    try:
        ts = datetime.fromisoformat(fetched_at)
    except ValueError:
        return True
    return datetime.now() - ts > timedelta(days=PRICE_TTL_DAYS)


def pricing_info() -> dict:
    """供 UI 展示：价格表日期/是否过期/来源。"""
    _, meta = load_pricing()
    return meta


def _match(pricing_data: dict, model: str) -> Optional[dict]:
    if not model:
        return None
    if model in pricing_data:
        return dict(pricing_data[model])
    # 前缀模糊：deepseek-v4-flash-0731 → deepseek-v4-flash
    for key in sorted(pricing_data, key=len, reverse=True):
        if model.startswith(key + "-") or model.startswith(key + ".") or model.startswith(key + "@"):
            return dict(pricing_data[key])
    return None


def refresh_pricing(url: Optional[str] = None) -> tuple[dict, dict]:
    """抓取官方定价页并写入缓存。网络/解析失败时抛异常（由调用方决定回退）。"""
    url = url or PRICING_URL
    html = _http_get(url)
    parsed = parse_pricing_page(html)
    meta = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": url,
        "stale": False,
    }
    _save(parsed, meta)
    return parsed, meta


def get_price(model: str) -> Optional[dict]:
    """查询模型价格（元/百万 tokens）。缓存过期时自动刷新一次（失败静默回退）。"""
    global _AUTO_REFRESH_ATTEMPTED
    pricing_data, meta = load_pricing()
    if not _AUTO_REFRESH_ATTEMPTED and meta.get("stale"):
        _AUTO_REFRESH_ATTEMPTED = True
        try:
            refresh_pricing()
            pricing_data, _ = load_pricing()
        except Exception:
            pass  # 自动刷新失败：用旧缓存/内置默认，不阻塞调用
    return _match(pricing_data, model)
