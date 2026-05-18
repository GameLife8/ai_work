"""测意图分类器。"""
from __future__ import annotations
from ops_agent.agent import _classify_intent


CASES = [
    # 应该是 config_view
    ("低代码平台的k8s 的coredns 配置可以展示一下么", "config_view"),
    ("看一下 swarm 里 haitu 服务的完整配置", "config_view"),
    ("配置文件给我看看", "config_view"),
    ("展示 coredns 的 ConfigMap", "config_view"),
    ("K8s coredns 看一下", "config_view"),
    ("瞧一下镜像", "config_view"),
    ("环境变量怎么配的", None),       # 没动词，不命中（合理，模糊）
    ("把环境变量给我看看", "config_view"),
    ("原文贴一下", "config_view"),
    ("查询配置", "config_view"),
    # 应该不是 config_view
    ("BigData swarm 里有哪些服务", "list_state"),
    ("你好", "chat_intro"),
    ("为什么 web 服务起不来", "diagnose"),
    ("跑 du -sh /var/log", "async_task"),
    ("把 web 扩容到 5 个", "write_action"),
    ("近 1 小时 CPU 趋势", "monitor"),
]


def main():
    pass_count = 0
    for q, expected in CASES:
        got = _classify_intent(q)
        ok = (got == expected)
        if ok:
            pass_count += 1
        marker = "✅" if ok else "❌"
        got_s = got or "None"
        exp_s = expected or "None"
        print(f"{marker} got={got_s:12s} expected={exp_s:12s}  ← {q}")
    print(f"\n{pass_count}/{len(CASES)} pass")


if __name__ == "__main__":
    main()
