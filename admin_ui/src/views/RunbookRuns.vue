<template>
  <div>
    <h1 class="page-title">剧本执行历史</h1>
    <p class="page-subtitle">每次 <code>platform_run_runbook</code> 的完整执行轨迹，含每节点的 args / 结果 / 信号 / 耗时，可逐步回放复盘。</p>

    <div class="toolbar">
      <el-select v-model="rbFilter" placeholder="全部剧本" clearable @change="load" style="width: 260px">
        <el-option v-for="rb in runbooks" :key="rb.key" :label="rb.title" :value="rb.key" />
      </el-select>
      <el-button @click="load">刷新</el-button>
      <span class="toolbar-right text-muted" style="font-size: 12px">{{ rows.length }} 条</span>
    </div>

    <el-table :data="rows" v-loading="loading" stripe>
      <el-table-column label="时间" width="180">
        <template #default="{ row }"><span class="text-secondary">{{ formatTime(row.created_at) }}</span></template>
      </el-table-column>
      <el-table-column label="剧本" min-width="240">
        <template #default="{ row }">
          <span class="code-mono">{{ row.runbook_key }}</span>
          <span class="text-muted" style="margin-left:6px">v{{ row.runbook_version }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="triggered_by" label="触发人" width="120">
        <template #default="{ row }">{{ row.triggered_by || '—' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="statusTag(row.global_status)" size="small">{{ row.global_status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="节点" width="80" align="center">
        <template #default="{ row }">{{ countDoneNodes(row) }}</template>
      </el-table-column>
      <el-table-column label="信号" width="80" align="center">
        <template #default="{ row }">{{ (row.signals || []).length }}</template>
      </el-table-column>
      <el-table-column prop="total_ms" label="耗时(ms)" width="110" align="right" />
      <el-table-column label="" width="80" align="right">
        <template #default="{ row }">
          <el-button size="small" link @click="openDetail(row)">回放</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 回放对话框 -->
    <el-dialog
      v-model="detail.show"
      :title="`执行回放：${detail.row?.runbook_key}`"
      width="940px"
      top="3vh"
    >
      <div v-if="detail.row" class="run-meta">
        <div class="meta-item"><span class="label">execution_id</span><span class="code-mono">{{ detail.row.execution_id }}</span></div>
        <div class="meta-item"><span class="label">状态</span><el-tag :type="statusTag(detail.row.global_status)" size="small">{{ detail.row.global_status }}</el-tag></div>
        <div class="meta-item"><span class="label">耗时</span>{{ detail.row.total_ms }}ms</div>
        <div class="meta-item"><span class="label">触发人</span>{{ detail.row.triggered_by || '—' }}</div>
        <div class="meta-item" v-if="detail.row.abort_reason"><span class="label">中止原因</span><span style="color: var(--danger)">{{ detail.row.abort_reason }}</span></div>
      </div>

      <el-tabs v-model="detail.tab" class="run-tabs">
        <el-tab-pane label="节点状态" name="nodes">
          <div class="nodes-list">
            <div v-for="(n, i) in detail.row?.node_states || []" :key="i" class="node-card">
              <div class="node-head">
                <span class="node-id code-mono">{{ n.node_id }}</span>
                <el-tag :type="nodeStatusTag(n.status)" size="small">{{ n.status }}</el-tag>
                <span v-if="n.latency_ms != null" class="text-muted">{{ n.latency_ms }}ms</span>
                <span v-if="n.attempts > 1" class="text-muted">尝试 {{ n.attempts }} 次</span>
              </div>
              <div v-if="n.skipped_reason" class="node-skipped">⏭ {{ n.skipped_reason }}</div>
              <div v-if="n.error" class="node-error">❌ {{ n.error }}</div>
              <div v-if="Object.keys(n.args_resolved || {}).length" class="node-args">
                <span class="text-muted">args：</span>
                <span class="code-mono">{{ JSON.stringify(n.args_resolved) }}</span>
              </div>
              <div v-if="(n.signals || []).length" class="node-signals">
                <span class="text-muted">signals：</span>
                <el-tag v-for="(s, j) in n.signals" :key="j" size="small" type="warning" style="margin-right: 4px">
                  {{ s.type }}
                </el-tag>
              </div>
              <details v-if="n.result" class="node-result">
                <summary class="text-muted" style="font-size: 12px; cursor: pointer">展开 result</summary>
                <CodeEditor :model-value="prettyJson(n.result)" language="json" readonly :height="200" />
              </details>
            </div>
          </div>
        </el-tab-pane>

        <el-tab-pane label="所有信号" name="signals">
          <CodeEditor :model-value="prettyJson(detail.row?.signals)" language="json" readonly :height="420" />
        </el-tab-pane>

        <el-tab-pane label="最终报告" name="report">
          <div class="report-md">{{ detail.row?.final_report || '（无报告）' }}</div>
        </el-tab-pane>

        <el-tab-pane label="原始入参" name="inputs">
          <CodeEditor :model-value="prettyJson(detail.row?.user_inputs)" language="json" readonly :height="200" />
        </el-tab-pane>
      </el-tabs>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import api from '../api'
import CodeEditor from '../components/CodeEditor.vue'

const rows = ref([])
const runbooks = ref([])
const loading = ref(false)
const rbFilter = ref('')
const detail = ref({ show: false, row: null, tab: 'nodes' })

function statusTag(s) {
  return ({ done: 'success', running: 'warning', failed: 'danger', timeout: 'danger', cancelled: 'info' })[s] || 'info'
}
function nodeStatusTag(s) {
  return ({ done: 'success', running: 'warning', error: 'danger', timeout: 'danger', skipped: 'info', pending: 'info' })[s] || 'info'
}
function countDoneNodes(row) {
  const ns = row.node_states || []
  const done = ns.filter(n => n.status === 'done').length
  return `${done}/${ns.length}`
}
function prettyJson(v) {
  if (v === null || v === undefined) return ''
  try { return JSON.stringify(v, null, 2) }
  catch { return String(v) }
}
function formatTime(s) {
  if (!s) return ''
  try { return new Date(s).toLocaleString('zh-CN', { hour12: false }) } catch { return s }
}

async function load() {
  loading.value = true
  try {
    const url = rbFilter.value ? `/runbook-runs?rb_key=${rbFilter.value}` : '/runbook-runs'
    rows.value = (await api.get(url)).data
  } finally { loading.value = false }
}
async function loadRunbooks() {
  runbooks.value = (await api.get('/runbooks')).data
}

onMounted(async () => {
  await loadRunbooks()
  await load()
})

function openDetail(row) {
  detail.value = { show: true, row, tab: 'nodes' }
}
</script>

<style scoped>
.run-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 16px 24px;
  background: var(--bg-soft);
  padding: 12px 16px;
  border-radius: var(--radius-sm);
  margin-bottom: 12px;
  font-size: 13px;
}
.meta-item { display: flex; gap: 8px; align-items: center; }
.meta-item .label { color: var(--text-muted); font-size: 12px; }

.run-tabs { margin-top: 8px; }

.nodes-list { display: flex; flex-direction: column; gap: 10px; }
.node-card {
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
}
.node-head { display: flex; align-items: center; gap: 10px; }
.node-id { font-weight: 500; }
.node-skipped { color: var(--text-muted); font-size: 12.5px; margin-top: 4px; }
.node-error { color: var(--danger); font-size: 12.5px; margin-top: 4px; }
.node-args { font-size: 12.5px; margin-top: 4px; }
.node-signals { font-size: 12.5px; margin-top: 4px; }
.node-result { margin-top: 8px; }

.report-md {
  white-space: pre-wrap;
  font-size: 13.5px;
  line-height: 1.8;
  padding: 16px 18px;
  background: var(--bg-soft);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-family: var(--font-sans);
}
code { font-family: var(--font-mono); background: var(--bg-soft); padding: 1px 5px; border-radius: 3px; color: var(--brand-700); }
</style>
