"""统一 LLM API Key 管理 — 两个页面共用。

规则：
- key 状态显式展示（已配置/未配置 + 掩码），不再把完整 key 预填进输入框
- 输入新 key 立即生效（os.environ）并自动写入 .env（重启免输）
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

ENV_KEYS = {
    "minimax": "MINIMAX_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
    "ollama": "",
}

# anthropic 走 requests 直连时需要（短线页面）
ANTHROPIC_ALT = "ANTHROPIC_AUTH_TOKEN"


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return key[:2] + "***"
    return f"{key[:4]}…{key[-3:]}"


def key_status(provider: str) -> tuple[str, str | None]:
    """返回 (env变量名, 已配置的key或None)。"""
    env_name = ENV_KEYS.get(provider, "")
    if not env_name:
        return ("(无需 key)", "ollama-local") if provider == "ollama" else ("", None)
    key = os.environ.get(env_name)
    if not key and provider == "anthropic":
        key = os.environ.get(ANTHROPIC_ALT)
    return env_name, key or None


def save_env_key(env_name: str, value: str):
    env_path = _PROJECT_ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    prefix = f"{env_name}="
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{prefix}{value}"
            break
    else:
        lines.append(f"{prefix}{value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def get_pref(name: str, default: str = "") -> str:
    """读取持久化偏好（.env / 环境变量）。"""
    return os.environ.get(name, default)


def set_pref(name: str, value: str):
    """写入偏好：立即生效 + 持久化到 .env。"""
    os.environ[name] = value
    save_env_key(name, value)


def render_api_key_input(provider: str, widget_prefix: str):
    """显式状态 + 空输入框（不回填完整key）+ 变更即生效并写 .env。"""
    env_name, current = key_status(provider)
    if provider == "ollama":
        st.caption("Ollama 本地模型无需 API Key")
        return

    if current:
        st.success(f"✅ {env_name} 已配置（{mask_key(current)}）", icon=None)
    else:
        st.warning(f"❌ {env_name} 未配置，分析会报 401", icon=None)

    new_key = st.text_input(
        f"{'更换' if current else '输入'} {env_name}",
        type="password",
        key=f"{widget_prefix}_apikey_{provider}",
        placeholder="输入后自动生效并保存到 .env",
    )
    if new_key.strip():
        key = new_key.strip()
        os.environ[env_name] = key
        save_env_key(env_name, key)
        st.success(f"已保存 {env_name}（{mask_key(key)}）")
        st.rerun()
