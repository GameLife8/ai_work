"""Provider-agnostic metric protocol.

设计动机
--------
之前的代码把"分析规则"和"Zabbix 怎么查 history"耦合在 ``ZabbixClient`` 里——
``get_metric_summary`` 既懂 history.get 怎么调，又懂 p95 怎么算，还懂哪个
key 是 CPU。换一个监控产品（Prometheus / Datadog / 自研 Agent）就要重写一遍。

本模块抽出最小可行接口：

    HostRef            主机的中性身份（id + name + ip）
    MetricPoint        一条原始采样：(epoch_seconds, value)
    MetricDescriptor   一项指标的"定位句柄"，里面藏 provider 私有结构
    MetricProvider     Protocol：``resolve_host`` / ``find_metric`` / ``query_series``

任何监控产品只要实现 ``MetricProvider`` 三个方法，就能复用全部
``services.metric_analytics`` 里的规则（峰值检测、时间窗口切片、统计聚合）。

为什么是 ``Protocol`` 而不是 ABC
------------------------------
- 现有 ``ZabbixClient`` 还要承担 healthcheck / login / get_host_overview 等遗留
  职责，不适合改继承体系。``Protocol`` + ``runtime_checkable`` 让它"鸭子类型
  天然合规"，零侵入。
- 未来要接 Prometheus 时，新写一个 ``PrometheusClient`` 类，方法签名对得上
  即可，不强迫继承。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# 标准化的逻辑指标名 —— provider 内部把它映射成 native key
# （Zabbix: system.cpu.util；Prometheus: node_cpu_seconds_total{mode!="idle"}; ...）
METRIC_CPU_UTIL    = "cpu.utilization"
METRIC_MEM_UTIL    = "memory.utilization"
METRIC_LOAD_AVG1   = "system.load.avg1"
METRIC_MEM_FREE    = "memory.available"
METRIC_MEM_TOTAL   = "memory.total"


# 所有 metric_analytics 默认认得的逻辑名集合（让 skill 可以做参数校验）
KNOWN_METRICS: tuple[str, ...] = (
    METRIC_CPU_UTIL,
    METRIC_MEM_UTIL,
    METRIC_LOAD_AVG1,
    METRIC_MEM_FREE,
    METRIC_MEM_TOTAL,
)


@dataclass(frozen=True)
class HostRef:
    """主机的 provider 中性身份。

    ``extra`` 装 provider 内部识别需要、但调用方不该依赖的字段
    （比如 Zabbix 的 ``host`` 字段名、Prometheus 的 ``instance`` label）。
    """

    id: str
    name: str
    ip: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricPoint:
    """一条原始采样点。

    ``timestamp`` 始终是 UTC epoch seconds——上层渲染再转本地时区。
    """

    timestamp: int
    value: float


@dataclass(frozen=True)
class MetricDescriptor:
    """一项指标在 provider 内部的"定位句柄"。

    - ``name`` 是逻辑名（``cpu.utilization``），调用方写代码用
    - ``unit`` 是 provider 报告的物理单位（``%`` / ``B`` / ``s``）
    - ``provider_handle`` 不透明，只有创造它的 provider 自己懂；
      analytics 层只会原样把它喂回 ``query_series``
    """

    name: str
    unit: str
    provider_handle: Any


@runtime_checkable
class MetricProvider(Protocol):
    """监控数据 provider 的最小接口。

    实现要点
    --------
    - ``name`` 是 provider 类型识别符（``zabbix`` / ``prometheus`` ...），
      用于日志和错误信息。
    - ``resolve_host`` 接受用户态查询串（host name / IP / 后端 ID 都行），
      返回标准化身份；找不到返回 None（不是抛异常——上层会包装成更友好的错误）。
    - ``find_metric`` 把逻辑指标名转成可查询的 descriptor。同样找不到返回 None。
    - ``query_series`` 必须返回**按时间升序**的原始点；窗口边界 inclusive。
      provider 自带的限流（如 Zabbix history.get limit）应在此层透明处理，
      并 log warning，不向调用方暴露截断细节。
    """

    name: str

    def resolve_host(self, query: str) -> HostRef | None: ...

    def find_metric(self, host: HostRef, metric_name: str) -> MetricDescriptor | None: ...

    def query_series(
        self,
        host: HostRef,
        metric: MetricDescriptor,
        start_ts: int,
        end_ts: int,
    ) -> list[MetricPoint]: ...
