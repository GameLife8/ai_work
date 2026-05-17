"""信号扫描器（独立模块）。

为什么把扫描抽出 skill
=====================
重构前每个 skill 内部都内置一段 ``_scan_*_signals(...)`` 函数，扫 stdout 里
"OOMKilled" / "no space left" / "CrashLoopBackOff" 这类关键词。这造成两个问题：

1. **跟执行强耦合**：扫描永远在那个特定 skill 里跑；同样语义的命令换条路径
   （比如改走 ``kube_query`` 跑 describe）就丢了信号。
2. **重复逻辑**：``swarm_get_failed_tasks`` 跟 ``swarm_get_service_logs_filter``
   都要扫 OOM、磁盘满，代码重复抄过。

重构后把扫描当**纯函数库**：通用查询 skill 跑完命令后按 ``(verb, resource)`` 或
"什么样的文本"路由到合适的 scanner，attach signals 即可。

公开接口
--------
每个 scanner 都是 ``scan(...) -> list[dict]``，dict 用 ``signals.signal()`` 构造，
返回空列表 = 没识别到。调用方拿到后用 ``signals.attach(result, sigs)`` 挂到
返回 envelope 即可。

模块约定：
- ``k8s_describe.scan(text, *, name, namespace)``       —— kubectl describe pod
- ``k8s_pod_list.scan(pods, *, namespace)``             —— kubectl get pods slim list
- ``k8s_logs.scan(text, *, pod, namespace)``            —— kubectl logs
- ``swarm_logs.scan(service, text)``                    —— docker service logs
- ``swarm_failed_tasks.scan(failed_tasks)``             —— docker service ps（失败）
- ``host_kernel.scan(node, text)``                      —— dmesg
- ``host_iptables.scan(node, text, *, probe_port=None)``  —— iptables-save
- ``host_storage.scan(node, rows)``                     —— df + lsblk 解析后

设计要点
- scanner 不依赖 ctx / runtime / store —— 纯函数，方便单测和复用。
- 跨 scanner 共享的关键词常量统一放 ``signals.py`` 的 SIG_* 常量。
- 不引入新的 signal type；如果原 skill 用到的 type 还没有，再去 signals.py 加。
"""

from __future__ import annotations
