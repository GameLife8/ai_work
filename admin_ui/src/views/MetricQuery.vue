<template>
  <div>
    <h1 class="page-title">指标查询</h1>
    <p class="page-subtitle">
      面向 "最近一次 CPU/内存最高值"、"故障时刻 ±N 分钟波动" 这类排障问题。
      规则在后端 <code>services.metric_analytics</code>，跟具体监控产品（Zabbix /
      Prometheus / Datadog ...）解耦——换 provider 不影响这里的查询语义。
    </p>

    <!-- 模式切换 -->
    <el-tabs v-model="mode" class="mode-tabs">
      <el-tab-pane label="峰值查询（最近 N 小时）" name="peak"></el-tab-pane>
      <el-tab-pane label="时刻附近窗口（±N 分钟）" name="window"></el-tab-pane>
    </el-tabs>

    <el-card class="form-card">
      <el-form :model="form" label-width="120px" label-position="top">
        <div class="form-row">
          <el-form-item label="主机（host / IP / hostid）" class="grow">
            <el-input
              v-model="form.host_query"
              placeholder="例如 P-L-TIDB09 / 169.24.7.26"
              clearable
              @keyup.enter="onRun"
            />
          </el-form-item>
          <el-form-item label="指标" style="width: 240px">
            <el-select v-model="form.metric" style="width: 100%">
              <el-option
                v-for="m in metricOptions"
                :key="m.value"
                :label="m.label"
                :value="m.value"
              />
            </el-select>
          </el-form-item>
        </div>

        <div class="form-row" v-if="mode === 'peak'">
          <el-form-item label="回看小时数" style="width: 200px">
            <el-input-number
              v-model="form.lookback_hours"
              :min="0.5"
              :max="720"
              :step="1"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="方向" style="width: 160px">
            <el-select v-model="form.direction" style="width: 100%">
              <el-option label="最大值（max）" value="max" />
              <el-option label="最小值（min）" value="min" />
            </el-select>
          </el-form-item>
          <el-form-item label="峰值附近上下文（秒）" style="width: 220px">
            <el-input-number
              v-model="form.context_window_seconds"
              :min="60"
              :max="3600"
              :step="60"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
        </div>

        <div class="form-row" v-else>
          <el-form-item label="中心时间" class="grow">
            <el-input
              v-model="form.center_time"
              placeholder="留空 = 当前时间；支持 2026-04-02 12:34:00 / ISO / epoch 秒"
              clearable
            />
            <div class="hint">不带时区的字符串按 Asia/Shanghai 解析。</div>
          </el-form-item>
          <el-form-item label="半窗口（分钟）" style="width: 200px">
            <el-input-number
              v-model="form.half_width_minutes"
              :min="1"
              :max="360"
              :step="1"
              controls-position="right"
              style="width: 100%"
            />
            <div class="hint">实际粒度看 provider 采集频率。</div>
          </el-form-item>
        </div>

        <el-form-item label="connection（不填走默认）">
          <el-select
            v-model="form.connection_id"
            clearable
            filterable
            placeholder="默认走系统默认 zabbix connection"
            style="width: 360px"
          >
            <el-option
              v-for="c in zabbixConnections"
              :key="c.id"
              :label="`${c.alias || c.name} (${c.type_code})`"
              :value="c.id"
            />
          </el-select>
        </el-form-item>

        <div class="action-row">
          <el-button
            type="primary"
            :loading="loading"
            :disabled="!form.host_query || !form.metric"
            @click="onRun"
          >
            查询
          </el-button>
          <el-button @click="onReset">重置</el-button>
        </div>
      </el-form>
    </el-card>

    <!-- 结果展示 -->
    <el-card v-if="result" class="result-card">
      <div class="result-head">
        <h2 class="result-title">
          {{ mode === 'peak' ? '峰值结果' : '时刻附近窗口' }}
        </h2>
        <el-tag v-if="result._stub_data" type="warning" size="small">
          ⚠️ Stub 模拟数据
        </el-tag>
      </div>

      <!-- summary 信息 -->
      <div class="summary-grid">
        <div class="summary-item">
          <div class="label">主机</div>
          <div class="value">
            {{ result.host?.host_name }}
            <span class="muted">({{ result.host?.host_ip || result.host?.host_id }})</span>
          </div>
        </div>
        <div class="summary-item">
          <div class="label">指标</div>
          <div class="value code-mono">{{ result.metric }}</div>
        </div>
        <div class="summary-item">
          <div class="label">单位</div>
          <div class="value">{{ result.unit || '—' }}</div>
        </div>
        <div class="summary-item">
          <div class="label">窗口</div>
          <div class="value">
            {{ formatTime(result.window?.start_time) }} →
            {{ formatTime(result.window?.end_time) }}
          </div>
        </div>
      </div>

      <!-- peak 模式特有 -->
      <template v-if="mode === 'peak'">
        <div v-if="result.peak" class="peak-banner">
          <div class="peak-meta">
            <div class="peak-label">{{ form.direction === 'min' ? '最低值' : '最高值' }}</div>
            <div class="peak-value">{{ result.peak.value }}<span class="unit">{{ result.unit }}</span></div>
          </div>
          <div class="peak-time">
            <span class="muted">出现于</span>
            <span>{{ formatTime(result.peak.time) }}</span>
          </div>
        </div>
        <el-alert
          v-else
          :title="result.warning || '窗口内无数据'"
          type="warning"
          :closable="false"
          show-icon
        />
      </template>

      <!-- 全窗统计 -->
      <div v-if="result.stats && Object.keys(result.stats).length" class="stats-grid">
        <div v-for="k in statKeys" :key="k" class="stat-cell">
          <div class="stat-key">{{ k }}</div>
          <div class="stat-val">{{ result.stats[k] }}</div>
        </div>
      </div>

      <!-- 原始/上下文采样 -->
      <div class="section-title" v-if="contextRows.length">
        {{ mode === 'peak' ? '峰值前后采样' : '原始采样' }}（{{ contextRows.length }} 点）
      </div>
      <el-table v-if="contextRows.length" :data="contextRows" stripe size="small" max-height="360">
        <el-table-column label="时间" width="200">
          <template #default="{ row }">{{ formatTime(row.time) }}</template>
        </el-table-column>
        <el-table-column label="epoch" width="140" prop="timestamp" />
        <el-table-column label="值">
          <template #default="{ row }">
            <span class="code-mono">{{ row.value }}{{ result.unit }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-empty
      v-else-if="!loading && hasRunOnce"
      description="无结果。检查主机名/指标是否正确，或换个时间窗口。"
    />
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'
import { METRIC_OPTIONS } from '../utils/metricOptions'

const mode = ref('peak')
const loading = ref(false)
const hasRunOnce = ref(false)
const result = ref(null)
const zabbixConnections = ref([])

const metricOptions = METRIC_OPTIONS

function blankForm() {
  return {
    host_query: '',
    metric: 'cpu.utilization',
    lookback_hours: 24,
    direction: 'max',
    context_window_seconds: 300,
    center_time: '',
    half_width_minutes: 10,
    connection_id: '',
  }
}
const form = ref(blankForm())

const statKeys = computed(() => {
  const order = ['avg', 'max', 'min', 'p95', 'last', 'raw_count']
  if (!result.value?.stats) return []
  return order.filter((k) => result.value.stats[k] !== undefined)
})

const contextRows = computed(() => {
  if (!result.value) return []
  return result.value.context_samples || result.value.samples || []
})

function formatTime(s) {
  if (!s) return ''
  try {
    const d = new Date(s)
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch { return s }
}

async function loadConnections() {
  // 普通用户没权限拉 /connections —— 静默跳过即可，会走默认。
  try {
    const { data } = await api.get('/connections?type=zabbix')
    zabbixConnections.value = data || []
  } catch (e) {
    e.handled = true
  }
}
onMounted(loadConnections)

function onReset() {
  form.value = blankForm()
  result.value = null
  hasRunOnce.value = false
}

async function onRun() {
  if (!form.value.host_query || !form.value.metric) {
    ElMessage.warning('请填写主机和指标')
    return
  }
  loading.value = true
  hasRunOnce.value = true
  try {
    const skillCode = mode.value === 'peak' ? 'metric_query_peak' : 'metric_query_window_around'
    const params =
      mode.value === 'peak'
        ? {
            host_query: form.value.host_query.trim(),
            metric: form.value.metric,
            lookback_hours: form.value.lookback_hours,
            direction: form.value.direction,
            context_window_seconds: form.value.context_window_seconds,
            ...(form.value.connection_id ? { connection_id: form.value.connection_id } : {}),
          }
        : {
            host_query: form.value.host_query.trim(),
            metric: form.value.metric,
            half_width_minutes: form.value.half_width_minutes,
            ...(form.value.center_time ? { center_time: form.value.center_time } : {}),
            ...(form.value.connection_id ? { connection_id: form.value.connection_id } : {}),
          }

    const { data } = await api.post(`/skills/${skillCode}/invoke`, { params })
    if (data.status !== 'ok') {
      ElMessage.error(data.message || data.error_code || '查询失败')
      result.value = null
      return
    }
    // SkillInvoker envelope: { skill, status, latency_ms, result, read_only }
    // result 即 skill.run 的返回值（这里就是 find_peak / fetch_window 的 dict），
    // 可能附带 _signals 字段（比如 CPU > 90% 时挂 SIG_HIGH_CPU），先不渲染。
    result.value = data.result || {}
  } catch (e) {
    e.handled = true
    ElMessage.error(`查询失败：${e.response?.data?.error || e.message || '未知错误'}`)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.mode-tabs { margin-bottom: 16px; }

.form-card {
  border: 1px solid var(--border-soft);
  margin-bottom: 16px;
}
.form-row {
  display: flex;
  gap: 16px;
}
.form-row :deep(.el-form-item) { flex: 0 0 auto; }
.form-row .grow { flex: 1; }
.action-row {
  margin-top: 4px;
  display: flex;
  gap: 8px;
}
.hint {
  color: var(--text-muted);
  font-size: 12px;
  margin-top: 4px;
  line-height: 1.5;
}

.result-card {
  border: 1px solid var(--border-soft);
}
.result-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.result-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0;
  color: var(--text-primary);
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
  padding: 14px 16px;
  background: var(--bg-soft);
  border-radius: var(--radius-sm);
}
.summary-item .label {
  font-size: 11.5px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: .5px;
  margin-bottom: 4px;
}
.summary-item .value {
  font-size: 13.5px;
  color: var(--text-primary);
}
.summary-item .muted { color: var(--text-muted); margin-left: 4px; }

.peak-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 22px;
  margin-bottom: 16px;
  background: linear-gradient(135deg, var(--brand-50), #fff);
  border: 1px solid var(--brand-100);
  border-radius: var(--radius-md);
}
.peak-label {
  font-size: 12px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: .5px;
}
.peak-value {
  font-size: 36px;
  font-weight: 600;
  color: var(--brand-700);
  letter-spacing: -1px;
  margin-top: 2px;
}
.peak-value .unit {
  font-size: 16px;
  font-weight: 400;
  color: var(--text-muted);
  margin-left: 4px;
}
.peak-time { color: var(--text-secondary); font-size: 13px; }

.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 8px;
  margin-bottom: 16px;
}
.stat-cell {
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
}
.stat-key {
  font-size: 11.5px;
  color: var(--text-muted);
  text-transform: uppercase;
}
.stat-val {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
  font-family: var(--font-mono);
}

.section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin: 8px 0;
}
code {
  font-family: var(--font-mono);
  font-size: 12.5px;
  background: var(--bg-soft);
  padding: 1px 6px;
  border-radius: 3px;
  color: var(--brand-700);
}
</style>
