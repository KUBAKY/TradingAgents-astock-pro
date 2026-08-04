"""股票代码输入组件：名称/代码模糊搜索下拉 + 手输兜底。

数据源 `a_stock._build_name_code_map()`（mootdx 沪深全市场，进程内缓存）。
mootdx 不可达时自动降级为普通 text_input，页面不阻塞。

用法::

    ticker = ticker_input("股票代码", key="my_key")

返回 6 位代码或空串；key 用于 widget 状态持久化（页面内唯一）。
"""

from __future__ import annotations

import streamlit as st

from tradingagents.dataflows.a_stock import _build_name_code_map


def _extract_code(option: str) -> str:
    """从下拉选项 '名称 代码' 中取出 6 位代码（末位 token，容错首尾空格）。"""
    return option.strip().split(" ")[-1]


def ticker_input(
    label: str = "股票代码",
    *,
    key: str,
    placeholder: str = "搜索名称或代码，如 京东方 / 000725",
) -> str:
    """渲染可搜索股票输入框，返回 6 位代码（空 = 未输入）。

    下拉命中返回代码；未命中下拉但手输 → 返回手输原文（交给 resolve_ticker）。
    """
    cache_key = f"{key}_universe"
    if cache_key not in st.session_state:
        try:
            n2c, _ = _build_name_code_map()
            st.session_state[cache_key] = sorted(
                f"{name} {code}" for name, code in n2c.items()
            )
        except Exception:
            st.session_state[cache_key] = None

    options = st.session_state.get(cache_key)
    if options is None:
        st.caption("⚠️ 名称库加载失败（mootdx 不可达），已降级为直接输入 6 位代码")
        return st.text_input(label, key=f"{key}_fallback", placeholder=placeholder)

    sel = st.multiselect(
        label,
        options=options,
        max_selections=1,
        key=f"{key}_ms",
        placeholder=placeholder,
    )
    if sel:
        return _extract_code(sel[0])

    return st.text_input(
        "或直接输入代码",
        key=f"{key}_manual",
        placeholder="6位代码，如 000725",
    ).strip()
