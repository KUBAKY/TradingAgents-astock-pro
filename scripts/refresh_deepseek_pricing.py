#!/usr/bin/env python3
"""手动刷新 DeepSeek 定价表：抓官方中文页 → 写缓存 → 打印新旧价格 diff。

用法:
    python -m scripts.refresh_deepseek_pricing
    python -m scripts.refresh_deepseek_pricing --url <官方定价页>
"""

from __future__ import annotations

import argparse

from tradingagents.cost import pricing


def main() -> None:
    p = argparse.ArgumentParser(description="刷新 DeepSeek 定价表缓存")
    p.add_argument("--url", default=None, help="定价页 URL（默认官方中文页）")
    p.add_argument("--json", action="store_true", help="输出 JSON（供 cron 解析）")
    args = p.parse_args()

    old, old_meta = pricing.load_pricing()
    try:
        new, meta = pricing.refresh_pricing(args.url)
    except Exception as e:
        print(f"[error] 刷新失败: {e}")
        raise SystemExit(1) from e

    if args.json:
        import json
        print(json.dumps({"fetched_at": meta["fetched_at"], "models": new},
                         ensure_ascii=False, indent=1))
        return

    print(f"[ok] 已刷新 {meta['source']} @ {meta['fetched_at']}")
    for model in sorted(set(old) | set(new)):
        old_price = old.get(model)
        new_price = new.get(model)
        if old_price == new_price:
            print(f"  {model}: 不变 {new_price}")
        elif old_price is None:
            print(f"  {model}: 新增 {new_price}")
        else:
            print(f"  {model}: {old_price} → {new_price}")


if __name__ == "__main__":
    main()
