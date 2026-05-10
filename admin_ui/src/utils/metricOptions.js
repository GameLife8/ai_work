/**
 * 平台标准化的逻辑指标名清单（与后端 services.metric_provider.KNOWN_METRICS 一一对应）。
 *
 * 维护准则：后端新增 KNOWN_METRICS 时同步在这里追加一条；前端选择器都从这里来，
 * 避免每个 view 自己 hardcode 重复的 enum 列表。
 */
export const METRIC_OPTIONS = [
  { value: 'cpu.utilization',    label: 'CPU 使用率（cpu.utilization）' },
  { value: 'memory.utilization', label: '内存使用率（memory.utilization）' },
  { value: 'system.load.avg1',   label: '系统 load avg1（system.load.avg1）' },
  { value: 'memory.available',   label: '内存可用（memory.available）' },
  { value: 'memory.total',       label: '内存总量（memory.total）' },
]
