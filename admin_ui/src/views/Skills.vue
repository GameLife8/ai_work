<template>
  <div>
    <h1 class="page-title">Skill 注册表</h1>
    <p class="page-subtitle">所有从 <code>skills/</code> 目录扫描到的能力。模型在 function calling 时看到的就是这一份。</p>

    <div class="toolbar">
      <el-input v-model="filter" placeholder="搜索 skill code / 名称 / 描述" style="width: 320px" clearable>
        <template #prefix>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="opacity: .5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        </template>
      </el-input>
      <el-select v-model="catFilter" placeholder="全部分类" clearable style="width:160px">
        <el-option v-for="c in categories" :key="c" :label="c" :value="c" />
      </el-select>
      <span class="toolbar-right text-muted" style="font-size: 12px">{{ filtered.length }} / {{ rows.length }}</span>
    </div>

    <el-table :data="filtered" stripe>
      <el-table-column label="Skill" min-width="280">
        <template #default="{ row }">
          <div class="skill-name">{{ row.name }}</div>
          <div class="skill-code">{{ row.code }}</div>
        </template>
      </el-table-column>
      <el-table-column label="分类" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="catTag(row.category)">{{ row.category }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="required_connection_type" label="所需接入" width="130">
        <template #default="{ row }">
          <span v-if="row.required_connection_type" class="code-mono">{{ row.required_connection_type }}</span>
          <span v-else class="text-muted">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="给模型的描述" min-width="320">
        <template #default="{ row }">
          <div class="desc">{{ row.description }}</div>
        </template>
      </el-table-column>
      <el-table-column label="只读" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.read_only ? 'success' : 'warning'">
            {{ row.read_only ? '只读' : '写' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="审批" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.requires_admin_approval" size="small" type="danger">需 admin</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="" width="80" align="right">
        <template #default="{ row }">
          <el-button link size="small" @click="openSchema(row)">Schema</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="schemaDlg.show" :title="schemaDlg.row?.code" width="640px" top="8vh">
      <div class="schema-meta" v-if="schemaDlg.row">
        <div class="meta-row"><span class="label">名称</span><span>{{ schemaDlg.row.name }}</span></div>
        <div class="meta-row"><span class="label">分类</span><el-tag size="small" :type="catTag(schemaDlg.row.category)">{{ schemaDlg.row.category }}</el-tag></div>
        <div class="meta-row"><span class="label">读写</span>
          <el-tag size="small" :type="schemaDlg.row.read_only ? 'success' : 'warning'">{{ schemaDlg.row.read_only ? '只读' : '写' }}</el-tag>
          <el-tag v-if="schemaDlg.row.requires_admin_approval" size="small" type="danger" style="margin-left:6px">需 admin 审批</el-tag>
        </div>
      </div>
      <div class="section-title">参数 Schema (JSON)</div>
      <CodeEditor
        :model-value="prettyJson(schemaDlg.row?.params_schema)"
        language="json"
        readonly
        :height="320"
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
const schemaDlg = ref({ show: false, row: null })

const categories = computed(() => Array.from(new Set(rows.value.map(r => r.category))).sort())

const filtered = computed(() => {
  let list = rows.value
  if (catFilter.value) list = list.filter(r => r.category === catFilter.value)
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
  })[cat] || ''
}

function prettyJson(obj) {
  if (!obj) return ''
  try { return JSON.stringify(obj, null, 2) } catch { return String(obj) }
}

function openSchema(row) {
  schemaDlg.value = { show: true, row }
}

onMounted(async () => {
  rows.value = (await api.get('/skills')).data
})
</script>

<style scoped>
.skill-name {
  font-weight: 500;
  color: var(--text-primary);
  font-size: 13.5px;
}
.skill-code {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-muted);
  margin-top: 2px;
}
.desc {
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
code {
  font-family: var(--font-mono);
  font-size: 12.5px;
  background: var(--bg-soft);
  padding: 1px 6px;
  border-radius: 3px;
  color: var(--brand-700);
}
.schema-meta {
  background: var(--bg-soft);
  border-radius: var(--radius-sm);
  padding: 12px 16px;
  margin-bottom: 16px;
}
.meta-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
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
