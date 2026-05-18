"""切换默认模型到一个有效的 model name。"""
from __future__ import annotations
import sys


def main(new_model: str):
    from runtime import create_runtime
    from config import Config
    runtime = create_runtime(Config)
    mm = runtime.model_manager
    # 列当前所有 model_config，找 default
    configs = runtime.store.list_model_configs()
    for c in configs:
        print(f"id={c['id'][:8]} provider={c.get('provider')} name={c.get('name')} model={c.get('model')} is_default={c.get('is_default')}")
    default = next((c for c in configs if c.get("is_default")), None)
    if not default:
        print("没有默认模型！")
        return 1
    print(f"\n当前默认：{default['model']}  →  切换到 {new_model}")
    runtime.store.update_model_config(default["id"], model=new_model)
    print("已更新")
    # 刷新 legacy clients
    if hasattr(runtime, "refresh_legacy_clients"):
        runtime.refresh_legacy_clients()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "doubao-seed-2.0-pro"))
