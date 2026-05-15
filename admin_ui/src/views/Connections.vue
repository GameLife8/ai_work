<template>
  <div>
    <h1 class="page-title">接入管理</h1>
    <p class="page-subtitle">每个接入是一份某种类型的具体连接配置（凭证 + 地址）。同类型可存多份，会话/skill 调用时按 connection_id 路由。</p>

    <div class="toolbar">
      <el-button type="primary" @click="openCreate">+ 新增接入</el-button>
      <el-select v-model="typeFilter" placeholder="全部类型" clearable @change="load" style="width:200px">
        <el-option v-for="d in drivers" :key="d.type" :label="d.display_name" :value="d.type" />
      </el-select>
      <span class="toolbar-right text-muted" style="font-size: 12px">共 {{ rows.length }} 条</span>
    </div>

    <el-table :data="rows" v-loading="loading" stripe>
      <el-table-column label="类型" width="120">
        <template #default="{ row }">
          <span class="code-mono">{{ row.type_code }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="name" label="名称" />
      <el-table-column prop="alias" label="别名" />
      <el-table-column label="默认" width="80">
        <template #default="{ row }">
          <el-tag v-if="row.is_default" type="success" size="small">默认</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="statusTag(row.status)" size="small">{{ row.status || 'unknown' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="260" align="right">
        <template #default="{ row }">
          <el-button size="small" link @click="onValidate(row)">验证</el-button>
          <el-button size="small" link @click="openEdit(row)">编辑</el-button>
          <el-button size="small" link type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dlg.show" :title="dlg.id ? '编辑接入' : '新增接入'" width="720px" top="6vh">
      <el-form :model="dlg.form" label-width="120px" label-position="top">
        <div class="form-row">
          <el-form-item label="接入类型" class="grow">
            <el-select v-model="dlg.form.type_code" :disabled="!!dlg.id" @change="onTypeChange" style="width:100%">
              <el-option v-for="d in drivers" :key="d.type" :label="d.display_name" :value="d.type">
                <span>{{ d.display_name }}</span>
                <span class="opt-hint">{{ d.type }}</span>
              </el-option>
            </el-select>
          </el-form-item>
          <el-form-item label="名称" class="grow">
            <el-input v-model="dlg.form.name" placeholder="例如 dmz-cluster01" />
          </el-form-item>
        </div>
        <div class="form-row">
          <el-form-item label="别名（中文友好名）" class="grow">
            <el-input v-model="dlg.form.alias" placeholder="例如 DMZ 域生产集群" />
          </el-form-item>
          <el-form-item label="设为默认">
            <el-switch v-model="dlg.form.is_default" />
          </el-form-item>
        </div>

        <el-form-item label="触发关键词（标签）">
          <el-select
            v-model="dlg.form.tags"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="输入关键词回车添加；例如 sws、生产、bigdata、192.168.2、master01"
            style="width: 100%"
          />
          <div class="hint">
            <strong>多条 connection 共享同样的关键词 = 一个"逻辑集群"</strong>。
            用户对话里命中任一关键词，AI 就会自动选用对应的 connection_id 调 skill。
            <br>建议同一物理集群的 host_agent / swarm / k8s 三条记录打**相同**关键词，方便 AI 跨 type 路由。
            例如 SWS 集群可以打：<code>sws</code>、<code>主集群</code>、<code>192.168.2</code>。
          </div>
        </el-form-item>

        <div class="section-title">接入参数</div>

        <el-form-item v-for="f in currentFields" :key="f.key" :label="f.label">
          <el-input
            v-if="f.type === 'password'"
            v-model="dlg.form.config[f.key]"
            type="password"
            show-password
            :placeholder="dlg.id ? '留空表示不修改' : f.placeholder"
          />
          <el-switch v-else-if="f.type === 'boolean'" v-model="dlg.form.config[f.key]" />
          <el-input-number v-else-if="f.type === 'integer'" v-model="dlg.form.config[f.key]" controls-position="right" style="width: 100%" />
          <CodeEditor
            v-else-if="f.type === 'textarea'"
            v-model="dlg.form.config[f.key]"
            :language="guessLang(f.key)"
            :height="280"
            :placeholder="f.placeholder"
            :wrap="true"
          />
          <el-input v-else v-model="dlg.form.config[f.key]" :placeholder="f.placeholder" />
          <div class="hint" v-if="f.help">{{ f.help }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg.show = false">取消</el-button>
        <el-button @click="onValidateForm">验证连通</el-button>
        <el-button type="primary" @click="onSubmit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'
import CodeEditor from '../components/CodeEditor.vue'

const rows = ref([]); const drivers = ref([]); const loading = ref(false)
const typeFilter = ref('')
const dlg = ref({ show: false, id: null, form: { type_code: '', name: '', alias: '', is_default: false, tags: [], config: {} } })
const currentFields = computed(() => drivers.value.find(d => d.type === dlg.value.form.type_code)?.fields || [])

function statusTag(s) {
  if (s === 'ok' || s === 'healthy') return 'success'
  if (s === 'fail' || s === 'error') return 'danger'
  return 'info'
}

function guessLang(key) {
  const k = (key || '').toLowerCase()
  if (k.includes('kubeconfig') || k.endsWith('yaml') || k.endsWith('yml')) return 'yaml'
  if (k.includes('json')) return 'json'
  return 'plain'
}

async function load() {
  loading.value = true
  try {
    const url = typeFilter.value ? `/connections?type=${typeFilter.value}` : '/connections'
    rows.value = (await api.get(url)).data
  } finally { loading.value = false }
}

onMounted(async () => {
  drivers.value = (await api.get('/drivers')).data
  await load()
})

function openCreate() {
  dlg.value = { show: true, id: null, form: { type_code: drivers.value[0]?.type || '', name: '', alias: '', is_default: false, tags: [], config: {} } }
  onTypeChange()
}

function openEdit(row) {
  // 编辑时把 password 类字段清空：后端会识别空串/****，保留库里现值
  const fields = drivers.value.find(d => d.type === row.type_code)?.fields || []
  const cfg = { ...row.config }
  for (const f of fields) {
    if (f.type === 'password') cfg[f.key] = ''
  }
  dlg.value = {
    show: true, id: row.id,
    form: {
      type_code: row.type_code, name: row.name, alias: row.alias,
      is_default: row.is_default,
      tags: Array.isArray(row.tags) ? [...row.tags] : [],
      config: cfg,
    },
  }
}

function onTypeChange() {
  const fields = currentFields.value
  for (const f of fields) {
    if (dlg.value.form.config[f.key] === undefined) {
      dlg.value.form.config[f.key] = f.default ?? (f.type === 'boolean' ? false : '')
    }
  }
}

async function onSubmit() {
  try {
    if (dlg.value.id) {
      await api.patch(`/connections/${dlg.value.id}`, dlg.value.form)
    } else {
      await api.post('/connections', dlg.value.form)
    }
    ElMessage.success('已保存')
    dlg.value.show = false
    await load()
  } catch (e) { ElMessage.error(e.response?.data?.error || '保存失败') }
}

async function onValidate(row) {
  const { data } = await api.post(`/connections/${row.id}/validate`)
  data.ok ? ElMessage.success('连通正常') : ElMessage.error(data.message || '连通失败')
  // 刷新列表，让"状态"列从 unknown 变成 ok / fail
  await load()
}

async function onValidateForm() {
  const payload = { ...dlg.value.form }
  if (dlg.value.id) payload.connection_id = dlg.value.id
  const { data } = await api.post('/connections/validate', payload)
  data.ok ? ElMessage.success('连通正常') : ElMessage.error(data.message || '连通失败')
}

async function onDelete(row) {
  await ElMessageBox.confirm(`确认删除接入 ${row.name}？`, '提示', { type: 'warning' })
  await api.delete(`/connections/${row.id}`)
  ElMessage.success('已删除'); await load()
}
</script>

<style scoped>
.hint {
  color: var(--text-muted);
  font-size: 12px;
  margin-top: 4px;
  line-height: 1.5;
}
.form-row {
  display: flex;
  gap: 16px;
}
.form-row :deep(.el-form-item) { flex: 0 0 auto; }
.form-row .grow { flex: 1; }
.section-title {
  margin: 12px 0 16px;
  padding-top: 12px;
  border-top: 1px solid var(--border-soft);
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
}
.opt-hint {
  margin-left: 8px;
  font-size: 11px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}
</style>
