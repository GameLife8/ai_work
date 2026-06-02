<template>
  <div>
    <h1 class="page-title">模型管理</h1>
    <p class="page-subtitle">默认接火山方舟，兼容通义千问、智谱 GLM、DeepSeek、Moonshot 等任意 OpenAI 协议兼容的国产模型。</p>

    <div class="toolbar">
      <el-button type="primary" @click="openCreate">+ 新增模型</el-button>
    </div>
    <el-table :data="rows" v-loading="loading" stripe>
      <el-table-column prop="provider" label="厂商" width="160" />
      <el-table-column prop="name" label="名称" />
      <el-table-column prop="model" label="模型 ID" />
      <el-table-column prop="base_url" label="Base URL" />
      <el-table-column label="默认" width="80">
        <template #default="{ row }"><el-tag v-if="row.is_default" type="success">默认</el-tag></template>
      </el-table-column>
      <el-table-column label="工具选择" width="110">
        <template #default="{ row }">
          <span v-if="row.tool_choice_preference" class="code-mono">{{ row.tool_choice_preference }}</span>
          <span v-else class="text-muted">auto</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180">
        <template #default="{ row }">
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dlg.show" :title="dlg.id ? '编辑模型' : '新增模型'" width="560px">
      <el-form :model="dlg.form" label-width="100px">
        <el-form-item label="厂商">
          <el-select v-model="dlg.form.provider" filterable allow-create>
            <el-option label="火山方舟" value="volcengine_ark" />
            <el-option label="火山 Coding" value="volcengine_coding" />
            <el-option label="通义千问" value="qwen" />
            <el-option label="智谱 GLM" value="zhipu" />
            <el-option label="DeepSeek" value="deepseek" />
            <el-option label="Moonshot" value="moonshot" />
            <el-option label="OpenAI 兼容" value="openai_compatible" />
          </el-select>
        </el-form-item>
        <el-form-item label="名称"><el-input v-model="dlg.form.name" /></el-form-item>
        <el-form-item label="Base URL"><el-input v-model="dlg.form.base_url" placeholder="https://ark.cn-beijing.volces.com/api/v3" /></el-form-item>
        <el-form-item label="API Key">
          <el-input v-model="dlg.form.api_key" type="password" show-password :placeholder="dlg.id ? '留空表示不修改' : ''" />
        </el-form-item>
        <el-form-item label="模型 ID"><el-input v-model="dlg.form.model" placeholder="ep-xxx / qwen-plus / glm-4 / deepseek-chat ..." /></el-form-item>
        <el-form-item label="超时(秒)"><el-input-number v-model="dlg.form.timeout_seconds" :min="10" :max="600" /></el-form-item>
        <el-form-item label="设为默认"><el-switch v-model="dlg.form.is_default" /></el-form-item>
        <el-form-item v-if="dlg.id" label="工具选择">
          <el-select v-model="dlg.form.tool_choice_preference" clearable placeholder="auto（默认）" style="width: 220px">
            <el-option label="auto（模型自己决定，默认）" value="auto" />
            <el-option label="required（每轮强制调工具）" value="required" />
            <el-option label="none（禁止调工具，纯生成）" value="none" />
          </el-select>
          <div class="form-hint">按该模型实测行为调；不同国产模型对 auto 的实现差异较大。仅编辑时可设。</div>
        </el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="onSubmit">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const rows = ref([]); const loading = ref(false)
const dlg = ref({ show: false, id: null, form: blankForm() })
function blankForm() {
  return { provider: 'volcengine_ark', name: '', base_url: '', api_key: '', model: '', timeout_seconds: 120, is_default: false }
}

async function load() {
  loading.value = true
  try {
    rows.value = (await api.get('/models')).data
  } finally {
    // 之前用 ``loading = true ... loading = false`` 单行串起来，
    // 一旦 await 抛错 loading 永远停在 true，loading 蒙层卡住整张表。
    loading.value = false
  }
}
onMounted(load)

function openCreate() { dlg.value = { show: true, id: null, form: blankForm() } }
function openEdit(row) { dlg.value = { show: true, id: row.id, form: { ...row, api_key: '' } } }

async function onSubmit() {
  try {
    if (dlg.value.id) {
      const payload = { ...dlg.value.form }
      if (!payload.api_key) delete payload.api_key
      await api.patch(`/models/${dlg.value.id}`, payload)
    } else {
      await api.post('/models', dlg.value.form)
    }
    ElMessage.success('已保存'); dlg.value.show = false; await load()
  } catch (e) { ElMessage.error(e.response?.data?.error || '保存失败') }
}

async function onDelete(row) {
  await ElMessageBox.confirm(`确认删除模型 ${row.name}？`, '提示', { type: 'warning' })
  await api.delete(`/models/${row.id}`); ElMessage.success('已删除'); await load()
}
</script>

<style scoped>
.form-hint {
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.5;
  margin-top: 4px;
}
</style>
