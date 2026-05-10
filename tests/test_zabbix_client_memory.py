"""Memory summary 解析逻辑回归测试。

历史背景
--------
之前这里 stub 的是 ``_get_numeric_history``，但 ``get_memory_summary``
后来重构为直接调 ``_get_window_history`` 拿原始点（带 clock 字段，便于
``_aggregate`` 计算 max/p95），导致 stub 成了 dead code，测试实际上是在
调真 RPC——离线无凭证下静默回 fallback stub，断言永远失败。

现在统一 stub ``_get_window_history``，返回 ``{"raw": [{clock, value}]}``
形状——跟生产路径完全一致。
"""

from __future__ import annotations

from services.zabbix_client import ZabbixClient


def _w(values: list[float], *, start: int = 1_700_000_000, step: int = 60) -> dict:
    """构造 ``_get_window_history`` 期望的返回结构。"""
    raw = [{"clock": start + i * step, "value": v} for i, v in enumerate(values)]
    return {"raw": raw, "samples": []}


class _MemoryClient(ZabbixClient):
    """Mock 出 host/items/window —— 不打网络。"""

    def __init__(self) -> None:
        super().__init__(base_url="http://example.com", use_stub=False)

    # 跳过 _resolve_host_id → _rpc('host.get') 这一段
    def _resolve_host_id(self, alert):
        return "host-1"

    def _get_host_items(self, host_id):
        return [
            {"itemid": "1", "key_": "vm.memory.utilization", "name": "", "value_type": "0"},
            {"itemid": "2", "key_": "vm.memory.size[available]", "name": "", "value_type": "3"},
            {"itemid": "3", "key_": "vm.memory.size[total]", "name": "", "value_type": "3"},
        ]

    # 关键：stub 真正被 get_memory_summary 调用的方法
    def _get_window_history(self, item_id, value_type, alert=None):
        history = {
            "1": _w([80.12, 83.21]),                    # 利用率 %
            "2": _w([6442450944, 5651509248]),          # 可用 bytes
            "3": _w([33566887936, 33566887936]),        # 总量 bytes
        }
        return history.get(item_id, {"raw": [], "samples": []})


def test_get_memory_summary_supports_utilization_and_available_keys():
    client = _MemoryClient()

    summary = client.get_memory_summary({"host_name": "P-L-TIDB09", "host_ip": "169.24.7.26"})

    # memory_used_percent = 最新一条 util（aggregate.last）
    assert summary["memory_used_percent"] == 83.21
    # 完整聚合一并暴露
    assert summary["memory_max_percent"] == 83.21
    assert summary["memory_min_percent"] == 80.12
    assert summary["memory_raw_count"] == 2
    # 容量字段：6442450944 / 1024^3 ≈ 5.26 GB（取最新点 5651509248 ≈ 5.26 GB）
    assert round(summary["available_gb"], 2) == 5.26
    assert round(summary["total_gb"], 2) == 31.26


def test_get_memory_summary_falls_back_to_stub_when_no_util_item():
    """没装 vm.memory.* 模板的主机不应该崩——回 stub 让上层显式知道。"""

    class _NoMemClient(_MemoryClient):
        def _get_host_items(self, host_id):
            return [{"itemid": "9", "key_": "system.cpu.util", "name": "", "value_type": "0"}]

    summary = _NoMemClient().get_memory_summary({"host_name": "x"})
    # stub 必带标记，调用方一眼看出不是真数据
    assert summary.get("_stub_data") is True


def test_resolve_host_id_prefers_explicit_host_id():
    client = ZabbixClient(base_url="http://example.com", use_stub=False)

    assert client._resolve_host_id({"host_id": "12345"}) == "12345"
