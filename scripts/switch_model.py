"""切换默认模型到一个有效的 model name。

注意：必须经 ``ModelManager.update`` 而不是直接 ``store.update_model_config``——
后者会绕过 ``_client_cache`` 失效 + ``on_change`` 回调（alert pipeline 刷新），
导致脚本说"已更新"但运行中进程仍用旧模型。
"""
from __future__ import annotations
import sys


def main(new_model: str):
    from runtime import create_runtime
    from config import Config
    runtime = create_runtime(Config)
    mm = runtime.model_manager
    configs = mm.list()
    for c in configs:
        print(f"id={c['id'][:8]} provider={c.get('provider')} name={c.get('name')} model={c.get('model')} is_default={c.get('is_default')}")
    default = next((c for c in configs if c.get("is_default")), None)
    if not default:
        print("没有默认模型！")
        return 1
    print(f"\n当前默认：{default['model']}  →  切换到 {new_model}")
    # 走 manager —— 自动失效缓存 + 触发 on_change 回调（runtime 已注册 legacy refresh）
    mm.update(default["id"], model=new_model)
    print("已更新（_client_cache 已失效，on_change 回调已触发）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "doubao-seed-2-0-pro-260215"))
