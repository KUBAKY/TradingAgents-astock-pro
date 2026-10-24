"""巨潮公告（cninfo）接入。

用途：
1. 补 Ch0 的公告扫描（`AS-C0-02`）——本机东财被拦，巨潮是可用替代；
2. 给黑名单补「审计非标 / 立案调查 / 退市风险 / 重大重组」四类硬条件。

实现要点（实测 2026-10-24）：
- 查询公告需要 `orgId`，由 `topSearch/query` 用代码换取（首次查询后缓存）；
- `column` 按市场取 szse / sse；北交所暂无独立列，先用 szse 兜底；
- 标题分类是**关键词规则**，只做初筛，命中后仍需人工/正文确认。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0"}
TOP_SEARCH = "http://www.cninfo.com.cn/new/information/topSearch/query"
QUERY = "http://www.cninfo.com.cn/new/hisAnnouncement/query"

# 关键词 → 黑名单/黄灯类别
RULES = {
    "审计非标": ["非标", "保留意见", "无法表示意见", "否定意见"],
    "立案调查": ["立案", "调查通知书"],
    "退市风险": ["退市风险警示", "终止上市", "面值退市"],
    "重大重组": ["重大资产重组", "发行股份购买资产"],
    "业绩预告": ["业绩预告", "业绩快报"],
}


def _column(symbol: str) -> str:
    s = str(symbol)
    if s.startswith("6"):
        return "sse"
    return "szse"       # 深市 / 创业板 / 北交所先用 szse 兜底


def search_org_id(symbol: str, cache: Path | None = None) -> str | None:
    """6 位代码 → cninfo orgId；结果缓存，避免重复请求。"""
    if cache and cache.exists():
        m = json.loads(cache.read_text(encoding="utf-8"))
        if str(symbol) in m:
            return m[str(symbol)]
    try:
        r = requests.post(TOP_SEARCH, data={"keyWord": str(symbol), "maxNum": 5},
                          headers=UA, timeout=15)
        rows = r.json()
    except Exception:
        return None
    hit = next((x for x in rows if x.get("code") == str(symbol)), None)
    org = hit.get("orgId") if hit else None
    if cache and org:
        m = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
        m[str(symbol)] = org
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return org


def fetch_announcements(symbol: str, start: str, end: str, page_size: int = 30,
                        org_cache: Path | None = None) -> list[dict]:
    """取区间公告，返回 [{title, date, url}]，按时间倒序。"""
    org = search_org_id(symbol, org_cache)
    if not org:
        return []
    data = {
        "pageNum": 1, "pageSize": page_size, "column": _column(symbol),
        "tabName": "fulltext", "stock": f"{symbol},{org}",
        "seDate": f"{start}~{end}", "isHLtitle": "true",
    }
    try:
        r = requests.post(QUERY, data=data, headers=UA, timeout=20)
        rows = (r.json().get("announcements") or [])
    except Exception:
        return []
    out = []
    for a in rows:
        url = a.get("adjunctUrl") or ""
        out.append({
            "title": re.sub(r"<[^>]+>", "", a.get("announcementTitle", "")),
            "date": time.strftime("%Y-%m-%d", time.localtime(
                (a.get("announcementTime") or 0) / 1000)) if a.get("announcementTime") else "",
            "url": f"http://static.cninfo.com.cn/{url}" if url else "",
        })
    return out


def classify(titles: list[str]) -> dict[str, list[str]]:
    """标题关键词分类 → {类别: [命中的标题]}。只做初筛，不替代公告正文。"""
    out: dict[str, list[str]] = {}
    for cat, kws in RULES.items():
        hits = [t for t in titles if any(k in t for k in kws)]
        if hits:
            out[cat] = hits
    return out
