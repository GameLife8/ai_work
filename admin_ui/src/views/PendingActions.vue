<template>
  <div>
    <h1 class="page-title">写操作待确认</h1>
    <p class="page-subtitle">所有 read_only=False 的 skill 调用先落到这里。Chainlit 卡片、本页按钮、MCP 客户端 platform_confirm_action 共用同一条审批流。</p>

    <div class="toolbar">
      <el-radio-group v-model="filter" size="default" @change="load">
        <el-radio-button label="">全部</el-radio-button>
        <el-radio-button label="pending">待确认</el-radio-button>
        <el-radio-button label="executed">已执行</el-radio-button>
        <el-radio-button label="rejected">已拒绝</el-radio-button>
        <el-radio-button label="failed">失败</el-radio-button>
        <el-radio-button label="expired">超时</el-radio-button>
      </el-radio-group>
      <el-button @click="load">刷新</el-button>
      <span class="toolbar-right text-muted" style="font-size: 12px">共 {{ rows.length }} 条</span>
    </div>

    <el-table :data="rows" v-loading="loading" stripe>
      <el-table-column prop="created_at" label="发起时间" width="180">
        <template #default="{ row }">
          <span class="text-secondary">{{ formatTime(row.created_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="Skill" min-width="220">
        <template #default="{ row }">
          <span class="code-mono">{{ row.skill_code }}</span>
          <el-tag v-if="row.requires_admin_approval" size="small" type="danger" style="margin-left:6px">admin</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="requested_by" label="发起人" width="120">
        <template #default="{ row }">
          {{ row.requested_by || '—' }}
        </template>
      </el-table-column>
      <el-table-column label="参数预览" min-width="240">
        <template #default="{ row }">
          <div class="args-preview">{{ argsLine(row.args_json) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="tagType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="240" align="right" fixed="right">
        <template #default="{ row }">
          <template v-if="row.status === 'pending'">
            <el-button size="small" type="primary" @click="onConfirm(row)">确认</el-button>
            <el-button size="small" @click="onReject(row)">拒绝</el-button>
            <el-button size="small" link @click="openDetail(row)">详情</el-button>
          </template>
          <el-button v-else size="small" link @click="openDetail(row)">详情</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="detail.show" title="操作详情" width="720px" top="6vh">
      <div v-if="detail.row" class="detail-meta">
        <div class="meta-row">
          <span class="label">Skill</span>
          <span class="code-mono">{{ detail.row.skill_code }}</span>
        </div>
        <div class="meta-row">
          <span class="label">状态</span>
          <el-tag :type="tagType(detail.row.status)" size="small">{{ statusLabel(detail.row.status) }}</el-tag>
        </div>
        <div class="meta-row">
          <span class="label">发起人</span>
          <span>{{ detail.row.requested_by || '—' }}</span>
          <span class="text-muted" style="margin-left: 4px">at {{ formatTime(detail.row.created_at) }}</span>
        </div>
        <div class="meta-row" v-if="detail.row.decided_by">
          <span class="label">决定人</span>
          <span>{{ detail.row.decided_by }}</span>
          <span class="text-muted" style="margin-left: 4px">at {{ formatTime(detail.row.decided_at) }}</span>
        </div>
        <div class="meta-row" v-if="detail.row.connection_id">
          <span class="label">连接</span>
          <span class="code-mono">{{ detail.row.connection_id }}</span>
        </div>
        <div class="meta-row" v-if="detail.row.reject_reason">
          <span class="label">拒绝原因</span>
          <span style="color: var(--danger)">{{ detail.row.reject_reason }}</span>
        </div>
      </div>

      <div class="section-title">参数</div>
      <CodeEditor
        :model-value="prettyJson(detail.row?.args_json)"
        language="json"
        readonly
        :height="200"
      />

      <div v-if="detail.row?.preview_json" class="section-title" style="margin-top: 14px">Preview</div>
      <CodeEditor
        v-if="detail.row?.preview_json"
        :model-value="prettyJson(detail.row.preview_json)"
        language="json"
        readonly
        :height="160"
      />
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'
import CodeEditor from '../components/CodeEditor.vue'

const rows = ref([])
const loading = ref(false)
const filter = ref('pending')
const detail = ref({ show: false, row: null })

function tagType(status) {
  return ({
    pending: 'warning',
    executed: 'success',
    rejected: 'info',
    failed: 'danger',
    expired: 'info',
  })[status] || ''
}
function statusLabel(s) {
  return ({ pending: '待确认', executed: '已执行', rejected: '已拒绝', failed: '失败', expired: '超时' })[s] || s
}

function prettyJson(obj) {
  if (obj === null || obj === undefined) return ''
  try { return JSON.stringify(obj, null, 2) } catch { return String(obj) }
}

function argsLine(obj) {
  if (!obj) return ''
  try {
    const s = JSON.stringify(obj)
    return s.length > 80 ? s.slice(0, 80) + '…' : s
  } catch { return String(obj) }
}

function formatTime(s) {
  if (!s) return ''
  try {
    const d = new Date(s)
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch { return s }
}

async function load() {
  loading.value = true
  try {
    const url = filter.value ? `/pending-actions?status=${filter.value}` : '/pending-actions'
    rows.value = (await api.get(url)).data
  } finally { loading.value = false }
}

onMounted(load)

async function onConfirm(row) {
  try {
    await ElMessageBox.confirm(
      `确认执行 ${row.skill_code}？\n\n参数：${argsLine(row.args_json)}`,
      '⚠️ 写操作确认',
      { type: 'warning', confirmButtonText: '执行', cancelButtonText: '取消', dangerouslyUseHTMLString: false },
    )
  } catch { return }
  // confirm POST 之前没 try/catch——网络挂了页面什么都不变，
  // 用户以为没生效又点一次，造成重复执行。这里 catch + 强制 reload。
  try {
    const { data } = await api.post(`/pending-actions/${row.token}/confirm`)
    if (data.status === 'ok') ElMessage.success('已执行')
    else ElMessage.error(data.message || data.error_code || '执行失败')
  } catch (e) {
    e.handled = true   // 让 axios interceptor 跳过 5xx toast，避免 double 弹
    ElMessage.error(`确认失败：${e.response?.data?.error || e.message || '网络异常'}`)
  } finally {
    await load()
  }
}

async function onReject(row) {
  try {
    const { value: reason } = await ElMessageBox.prompt('拒绝原因（可选）', '拒绝操作', {
      inputPlaceholder: '例如：参数错误 / 应在维护窗口外执行',
    })
    await api.post(`/pending-actions/${row.token}/reject`, { reason: reason || '' })
    ElMessage.success('已拒绝')
  } catch { /* user cancelled */ }
  await load()
}

function openDetail(row) { detail.value = { show: true, row } }
</script>

<style scoped>
.args-preview {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.detail-meta {
  background: var(--bg-soft);
  border-radius: var(--radius-sm);
  padding: 14px 18px;
  margin-bottom: 16px;
}
.meta-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  font-size: 13px;
}
.meta-row:last-child { margin-bottom: 0; }
.meta-row .label {
  color: var(--text-muted);
  font-size: 12px;
  min-width: 64px;
}
.section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}
</style>
