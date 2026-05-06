<template>
  <div>
    <h1 class="page-title">调用审计</h1>
    <p class="page-subtitle">所有 skill 调用的统一审计；写操作可通过 args._extra.confirmation_token 反查发起人。</p>

    <div class="toolbar">
      <el-input v-model="filter" placeholder="搜索 skill code / 用户" style="width: 280px" clearable />
      <el-select v-model="statusFilter" placeholder="全部状态" clearable style="width: 140px">
        <el-option label="ok" value="ok" />
        <el-option label="error" value="error" />
      </el-select>
      <el-button @click="load">刷新</el-button>
      <span class="toolbar-right text-muted" style="font-size: 12px">{{ filtered.length }} / {{ rows.length }}</span>
    </div>

    <el-table :data="filtered" stripe v-loading="loading">
      <el-table-column prop="id" label="#" width="70" />
      <el-table-column label="时间" width="180">
        <template #default="{ row }"><span class="text-secondary">{{ formatTime(row.created_at) }}</span></template>
      </el-table-column>
      <el-table-column prop="user" label="用户" width="120">
        <template #default="{ row }">{{ row.user || '—' }}</template>
      </el-table-column>
      <el-table-column label="Skill" min-width="220">
        <template #default="{ row }"><span class="code-mono">{{ row.skill_code }}</span></template>
      </el-table-column>
      <el-table-column prop="connection_id" label="连接" width="200">
        <template #default="{ row }">
          <span v-if="row.connection_id" class="code-mono">{{ row.connection_id }}</span>
          <span v-else class="text-muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.status === 'ok' ? 'success' : 'danger'" size="small">{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="latency_ms" label="耗时(ms)" width="100" align="right" />
      <el-table-column label="" width="80" align="right">
        <template #default="{ row }">
          <el-button link size="small" @click="openDetail(row)">详情</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="detail.show" title="调用详情" width="780px" top="5vh">
      <div v-if="detail.row" class="detail-meta">
        <div class="meta-row"><span class="label">Skill</span><span class="code-mono">{{ detail.row.skill_code }}</span></div>
        <div class="meta-row"><span class="label">用户</span>{{ detail.row.user || '—' }}</div>
        <div class="meta-row"><span class="label">连接</span><span class="code-mono">{{ detail.row.connection_id || '—' }}</span></div>
        <div class="meta-row"><span class="label">会话</span><span class="code-mono">{{ detail.row.session_id || '—' }}</span></div>
        <div class="meta-row"><span class="label">状态</span><el-tag :type="detail.row.status === 'ok' ? 'success' : 'danger'" size="small">{{ detail.row.status }}</el-tag><span class="text-muted" style="margin-left:8px">{{ detail.row.latency_ms }}ms</span></div>
        <div class="meta-row" v-if="detail.row.error"><span class="label">错误</span><span style="color: var(--danger)">{{ detail.row.error }}</span></div>
      </div>

      <div class="section-title">参数</div>
      <CodeEditor :model-value="prettyJson(detail.row?.args_json)" language="json" readonly :height="200" />

      <div class="section-title" style="margin-top: 14px">结果</div>
      <CodeEditor :model-value="prettyJson(detail.row?.result_json)" language="json" readonly :height="280" />
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import api from '../api'
import CodeEditor from '../components/CodeEditor.vue'

const rows = ref([])
const loading = ref(false)
const filter = ref('')
const statusFilter = ref('')
const detail = ref({ show: false, row: null })

const filtered = computed(() => {
  let list = rows.value
  if (statusFilter.value) list = list.filter(r => r.status === statusFilter.value)
  if (filter.value) {
    const q = filter.value.toLowerCase()
    list = list.filter(r =>
      (r.skill_code || '').toLowerCase().includes(q) ||
      (r.user || '').toLowerCase().includes(q),
    )
  }
  return list
})

function prettyJson(s) {
  if (!s) return ''
  try {
    const obj = typeof s === 'string' ? JSON.parse(s) : s
    return JSON.stringify(obj, null, 2)
  } catch { return typeof s === 'string' ? s : JSON.stringify(s) }
}

function formatTime(s) {
  if (!s) return ''
  try { return new Date(s).toLocaleString('zh-CN', { hour12: false }) } catch { return s }
}

async function load() {
  loading.value = true
  try { rows.value = (await api.get('/skill-calls?limit=200')).data }
  finally { loading.value = false }
}

function openDetail(row) { detail.value = { show: true, row } }

onMounted(load)
</script>

<style scoped>
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
  min-width: 50px;
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
