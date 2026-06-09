<template>
  <div>
    <h1 class="page-title">Skill 注册表</h1>
    <p class="page-subtitle">所有从 <code>skills/</code> 目录扫描到的能力（含 HTTP YAML 注册的）。模型在 function calling 时看到的就是这一份。</p>

    <div class="toolbar">
      <el-input v-model="filter" placeholder="搜索 skill code / 名称 / 描述" style="width: 320px" clearable>
        <template #prefix>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="opacity: .5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        </template>
      </el-input>
      <el-select v-model="catFilter" placeholder="全部分类" clearable style="width:160px">
        <el-option v-for="c in categories" :key="c" :label="c" :value="c" />
      </el-select>
      <el-select v-model="sourceFilter" placeholder="全部来源" clearable style="width:140px">
        <el-option label="Python 原生" value="python" />
        <el-option label="HTTP YAML" value="http" />
      </el-select>
      <span class="toolbar-right text-muted" style="font-size: 12px">{{ filtered.length }} / {{ rows.length }}</span>
    </div>

    <el-table :data="filtered" stripe row-class-name="skill-row">
      <el-table-column label="Skill" min-width="280">
        <template #default="{ row }">
          <div class="skill-head">
            <span class="skill-name">{{ row.name }}</span>
            <el-tag v-if="row.source === 'http'" size="small" type="info" effect="plain" class="src-chip">HTTP</el-tag>
            <el-tag v-if="row.requires_admin_approval" size="small" type="danger" effect="plain" class="src-chip">需 admin 审批</el-tag>
          </div>
          <div class="skill-code">{{ row.code }}</div>
        </template>
      </el-table-column>

      <el-table-column label="分类" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="catTag(row.category)">{{ row.category }}</el-tag>
        </template>
      </el-table-column>

      <el-table-column prop="required_connection_type" label="所需接入" width="160">
        <template #default="{ row }">
          <span v-if="row.required_connection_type" class="code-mono">{{ row.required_connection_type }}</span>
          <span v-else class="text-muted">—</span>
        </template>
      </el-table-column>

      <el-table-column prop="description" label="给模型的描述" min-width="360">
        <template #default="{ row }">
          <el-tooltip
            placement="top-start"
            :show-after="300"
            :hide-after="0"
            popper-class="desc-tooltip"
          >
            <template #content>
              <div class="desc-full">{{ row.description }}</div>
            </template>
            <div class="desc">{{ row.description }}</div>
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column label="读写" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.read_only ? 'success' : 'warning'">
            {{ row.read_only ? '只读' : '写' }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="100" align="right" fixed="right">
        <template #default="{ row }">
          <el-button link size="small" @click="openDetail(row)">详情</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 详情对话框：完整 description + 所有元数据 + params_schema -->
    <el-dialog
      v-model="detail.show"
      :title="detail.row?.name || ''"
      width="720px"
      top="6vh"
    >
      <div v-if="detail.row" class="detail-meta">
        <div class="meta-row">
          <span class="label">Code</span>
          <span class="code-mono">{{ detail.row.code }}</span>
        </div>
        <div class="meta-row">
          <span class="label">分类</span>
          <el-tag size="small" :type="catTag(detail.row.category)">{{ detail.row.category }}</el-tag>
          <el-tag v-if="detail.row.source === 'http'" size="small" type="info" effect="plain" style="margin-left:6px">HTTP YAML</el-tag>
          <el-tag v-else size="small" type="info" effect="plain" style="margin-left:6px">Python 原生</el-tag>
        </div>
        <div class="meta-row">
          <span class="label">所需接入</span>
          <span v-if="detail.row.required_connection_type" class="code-mono">{{ detail.row.required_connection_type }}</span>
          <span v-else class="text-muted">无（独立 skill）</span>
        </div>
        <div class="meta-row">
          <span class="label">读/写</span>
          <el-tag size="small" :type="detail.row.read_only ? 'success' : 'warning'">{{ detail.row.read_only ? '只读' : '写' }}</el-tag>
          <el-tag v-if="detail.row.requires_admin_approval" size="small" type="danger" style="margin-left:6px">需 admin 审批</el-tag>
        </div>
        <div class="meta-row">
          <span class="label">可见性</span>
          <span>{{ detail.row.visibility === 'admin' ? '仅 admin' : '所有用户' }}</span>
        </div>
      </div>

      <div class="section-title">完整描述（给模型看的）</div>
      <div class="full-desc">{{ detail.row?.description }}</div>

      <div class="section-title" style="margin-top: 16px">参数 Schema (JSON)</div>
      <CodeEditor
        :model-value="prettyJson(detail.row?.params_schema)"
        language="json"
        readonly
        :height="280"
      />
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import api from '../api'
import CodeEditor from '../components/CodeEditor.vue'

const rows = ref([])
const filter = ref('')
const catFilter = ref('')
const sourceFilter = ref('')
const detail = ref({ show: false, row: null })

const categories = computed(() => Array.from(new Set(rows.value.map(r => r.category))).sort())

const filtered = computed(() => {
  let list = rows.value
  if (catFilter.value) list = list.filter(r => r.category === catFilter.value)
  if (sourceFilter.value) list = list.filter(r => (r.source || 'python') === sourceFilter.value)
  if (filter.value) {
    const q = filter.value.toLowerCase()
    list = list.filter(r =>
      r.code.toLowerCase().includes(q) ||
      (r.name || '').toLowerCase().includes(q) ||
      (r.description || '').toLowerCase().includes(q),
    )
  }
  return list
})

function catTag(cat) {
  return ({
    swarm: '',
    k8s: 'success',
    zabbix: 'warning',
    alerts: 'info',
    platform: 'danger',
    host: 'info',
    integration: 'info',
  })[cat] || ''
}

function prettyJson(obj) {
  if (!obj) return ''
  try { return JSON.stringify(obj, null, 2) } catch { return String(obj) }
}

function openDetail(row) { detail.value = { show: true, row } }

onMounted(async () => {
  rows.value = (await api.get('/skills')).data
})
</script>

<style scoped>
.skill-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.skill-name {
  font-weight: 500;
  color: var(--text-primary);
  font-size: 13.5px;
}
.src-chip {
  font-size: 10.5px !important;
  padding: 0 6px !important;
  height: 18px !important;
  line-height: 18px !important;
}
.skill-code {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-muted);
  margin-top: 4px;
}

/* 描述：表格里 2 行截断 + tooltip 看全 */
.desc {
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  cursor: help;
}

/* tooltip 内容：保留换行，最大宽度限制好阅读 */
:global(.desc-tooltip.el-popper) {
  max-width: 520px !important;
  font-size: 12.5px !important;
  line-height: 1.7 !important;
}
.desc-full {
  white-space: pre-wrap;
  word-break: break-word;
}

code {
  font-family: var(--font-mono);
  font-size: 12.5px;
  background: var(--bg-soft);
  padding: 1px 6px;
  border-radius: 3px;
  color: var(--brand-700);
}

/* 表格行高松一点 */
:deep(.skill-row .el-table__cell) { padding: 14px 12px !important; }

/* 详情对话框 */
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
.full-desc {
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--bg-soft);
  padding: 12px 16px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-primary);
}
</style>
