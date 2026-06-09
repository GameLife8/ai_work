<template>
  <div>
    <h1 class="page-title">异步任务</h1>
    <p class="page-subtitle">
      所有通过 host_run_command_async 提交的长命令任务。数据写在 platform_async_task 表，agent 重启 / 会话中断都不丢。
      后台 poller 每 10 秒同步一次状态。
    </p>

    <div class="toolbar">
      <el-radio-group v-model="filter.status" size="default" @change="load">
        <el-radio-button label="">全部</el-radio-button>
        <el-radio-button label="running">运行中</el-radio-button>
        <el-radio-button label="done">完成</el-radio-button>
        <el-radio-button label="error">错误</el-radio-button>
        <el-radio-button label="timeout">超时</el-radio-button>
        <el-radio-button label="cancelled">已取消</el-radio-button>
        <el-radio-button label="lost">丢失</el-radio-button>
      </el-radio-group>
      <el-input
        v-model="filter.node"
        placeholder="按节点过滤"
        size="default"
        clearable
        style="width: 200px"
        @change="load"
      />
      <el-button @click="load">刷新</el-button>
      <el-switch
        v-model="autoRefresh"
        active-text="自动刷新 10s"
        @change="onAutoRefreshChange"
      />
      <span class="toolbar-right text-muted" style="font-size: 12px">共 {{ rows.length }} 条</span>
    </div>

    <el-table :data="rows" v-loading="loading" stripe size="small">
      <el-table-column prop="submitted_at" label="提交时间" width="170">
        <template #default="{ row }">
          <span class="text-secondary">{{ formatTime(row.submitted_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="tagType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="节点" width="160">
        <template #default="{ row }">
          <span class="code-mono">{{ row.node }}</span>
          <div class="text-muted" style="font-size: 11px">{{ row.connection_name || row.connection_id }}</div>
        </template>
      </el-table-column>
      <el-table-column label="命令" min-width="280">
        <template #default="{ row }">
          <div class="cmd-preview">{{ row.command }}</div>
        </template>
      </el-table-column>
      <el-table-column prop="submitted_by" label="提交人" width="100">
        <template #default="{ row }">
          {{ row.submitted_by || '—' }}
        </template>
      </el-table-column>
      <el-table-column label="耗时" width="100">
        <template #default="{ row }">
          <span v-if="row.duration_ms">{{ formatDuration(row.duration_ms) }}</span>
          <span v-else-if="row.status === 'running'" class="text-muted">{{ runningElapsed(row.started_at) }}</span>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column label="退出码" width="70">
        <template #default="{ row }">
          <span v-if="row.exit_code === null || row.exit_code === undefined">—</span>
          <span v-else :class="row.exit_code === 0 ? 'text-success' : 'text-danger'">{{ row.exit_code }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120" align="center" fixed="right">
        <template #default="{ row }">
          <el-button size="small" link @click="openDetail(row)">详情</el-button>
          <el-button
            v-if="row.status === 'running' || row.status === 'submitting'"
            size="small"
            link
            type="danger"
            @click="onCancel(row)"
          >取消</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="detail.show" :title="`任务 ${detail.row?.task_id || ''}`" width="900px" top="4vh">
      <div v-if="detail.row" class="detail-meta">
        <div class="meta-row">
          <span class="meta-label">状态：</span>
          <el-tag :type="tagType(detail.row.status)" size="small">{{ statusLabel(detail.row.status) }}</el-tag>
          <el-button size="small" link style="margin-left: 10px" @click="refreshDetail">同步刷新</el-button>
        </div>
        <div class="meta-row"><span class="meta-label">命令：</span><span class="code-mono">{{ detail.row.command }}</span></div>
        <div class="meta-row"><span class="meta-label">节点：</span>{{ detail.row.node }}（{{ detail.row.connection_name || detail.row.connection_id }}）</div>
        <div class="meta-row"><span class="meta-label">提交人：</span>{{ detail.row.submitted_by || '—' }}</div>
        <div class="meta-row"><span class="meta-label">session_id：</span><span class="code-mono">{{ detail.row.session_id || '—' }}</span></div>
        <div class="meta-row"><span class="meta-label">提交时间：</span>{{ formatTime(detail.row.submitted_at) }}</div>
        <div class="meta-row"><span class="meta-label">开始时间：</span>{{ formatTime(detail.row.started_at) || '—' }}</div>
        <div class="meta-row"><span class="meta-label">结束时间：</span>{{ formatTime(detail.row.ended_at) || '—' }}</div>
        <div class="meta-row"><span class="meta-label">耗时：</span>{{ detail.row.duration_ms ? formatDuration(detail.row.duration_ms) : '—' }}</div>
        <div class="meta-row"><span class="meta-label">退出码：</span>{{ detail.row.exit_code ?? '—' }}</div>
        <div class="meta-row"><span class="meta-label">最大运行时长：</span>{{ detail.row.max_runtime_sec }} 秒</div>
        <div class="meta-row"><span class="meta-label">轮询次数：</span>{{ detail.row.poll_count || 0 }}</div>
        <div class="meta-row"><span class="meta-label">最近 poll：</span>{{ formatTime(detail.row.last_poll_at) || '—' }}</div>
        <div v-if="detail.row.last_poll_error" class="meta-row">
          <span class="meta-label">最近错误：</span><span class="text-danger">{{ detail.row.last_poll_error }}</span>
        </div>
      </div>

      <div v-if="detail.row" style="margin-top: 16px">
        <div class="meta-label" style="margin-bottom: 6px">stdout {{ detail.row.truncated ? '（已截断）' : '' }}</div>
        <pre class="output-block">{{ detail.row.stdout || '（空）' }}</pre>
        <div class="meta-label" style="margin: 12px 0 6px">stderr</div>
        <pre class="output-block error-output">{{ detail.row.stderr || '（空）' }}</pre>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const rows = ref([])
const loading = ref(false)
const filter = reactive({ status: '', node: '' })
const detail = ref({ show: false, row: null })
const autoRefresh = ref(false)
let timer = null

function tagType(s) {
  return ({
    submitting: 'warning',
    running:    'primary',
    done:       'success',
    error:      'danger',
    timeout:    'danger',
    cancelled:  'info',
    lost:       'info',
  })[s] || ''
}
function statusLabel(s) {
  return ({
    submitting: '提交中',
    running:    '运行中',
    done:       '完成',
    error:      '错误',
    timeout:    '超时',
    cancelled:  '已取消',
    lost:       '丢失',
  })[s] || s
}

function formatTime(s) {
  if (!s) return ''
  try { return new Date(s).toLocaleString('zh-CN', { hour12: false }) } catch { return s }
}

function formatDuration(ms) {
  if (!ms) return ''
  if (ms < 1000) return `${ms}ms`
  const sec = ms / 1000
  if (sec < 60) return `${sec.toFixed(1)}s`
  const m = Math.floor(sec / 60), s = Math.round(sec % 60)
  return `${m}m${s}s`
}

function runningElapsed(startedAt) {
  if (!startedAt) return ''
  try {
    const elapsed = (Date.now() - new Date(startedAt).getTime())
    return formatDuration(elapsed)
  } catch { return '' }
}

async function load() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    if (filter.status) params.set('status', filter.status)
    if (filter.node) params.set('node', filter.node)
    params.set('limit', '200')
    rows.value = (await api.get(`/async-tasks?${params}`)).data
  } finally { loading.value = false }
}

function openDetail(row) {
  detail.value = { show: true, row }
  // 打开就同步刷一次，拿最新 stdout
  refreshDetail()
}

async function refreshDetail() {
  if (!detail.value.row) return
  try {
    const { data } = await api.post(`/async-tasks/${detail.value.row.task_id}/refresh`)
    detail.value.row = data
    // 列表里那行也更新
    const idx = rows.value.findIndex((r) => r.task_id === data.task_id)
    if (idx >= 0) rows.value[idx] = data
  } catch (e) {
    e.handled = true
    ElMessage.error(`刷新失败：${e.response?.data?.error || e.message}`)
  }
}

async function onCancel(row) {
  try {
    await ElMessageBox.confirm(
      `取消任务 ${row.task_id}？\n命令：${row.command}`,
      '取消任务',
      { type: 'warning', confirmButtonText: '取消任务', cancelButtonText: '关闭' },
    )
  } catch { return }
  try {
    await api.post(`/async-tasks/${row.task_id}/cancel`)
    ElMessage.success('已发取消信号')
  } catch (e) {
    e.handled = true
    ElMessage.error(`取消失败：${e.response?.data?.error || e.message}`)
  } finally {
    await load()
  }
}

function onAutoRefreshChange(v) {
  if (timer) { clearInterval(timer); timer = null }
  if (v) { timer = setInterval(load, 10000) }
}

onMounted(load)
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.page-title { margin: 0 0 6px; font-size: 18px; font-weight: 600; }
.page-subtitle { color: #6b7280; font-size: 13px; margin: 0 0 16px; }
.toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.toolbar-right { margin-left: auto; }
.text-muted { color: #94a3b8; }
.text-success { color: #16a34a; font-weight: 600; }
.text-danger { color: #dc2626; }
.text-secondary { color: #6b7280; font-size: 12px; }
.code-mono { font-family: 'JetBrains Mono', Menlo, Consolas, monospace; font-size: 12px; }
.cmd-preview {
  font-family: 'JetBrains Mono', Menlo, Consolas, monospace;
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.detail-meta { background: #f8fafc; padding: 12px 14px; border-radius: 6px; font-size: 13px; }
.meta-row { padding: 4px 0; }
.meta-label { color: #64748b; display: inline-block; min-width: 90px; }
.output-block {
  background: #0f172a;
  color: #e2e8f0;
  font-family: 'JetBrains Mono', Menlo, Consolas, monospace;
  font-size: 12px;
  padding: 12px;
  border-radius: 4px;
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
.error-output { color: #fca5a5; }
</style>
