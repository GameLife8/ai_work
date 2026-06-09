<template>
  <div>
    <h1 class="page-title">诊断剧本（图执行）</h1>
    <p class="page-subtitle">
      把"运维老司机查问题套路"沉淀成可执行的有向图。模型在 chainlit 调
      <code>platform_run_runbook(user_query, inputs)</code>，平台按图自动跑一遍 skill，
      最后让模型只写中文报告。改完<b>立即热加载</b>，不用重启服务。
    </p>

    <div class="toolbar">
      <el-button type="primary" @click="openCreate">+ 新增剧本</el-button>
      <el-button @click="load">刷新</el-button>
      <RouterLink to="/runbook-runs" class="link-btn">查看执行历史 →</RouterLink>
      <span class="toolbar-right text-muted" style="font-size: 12px">{{ rows.length }} 条</span>
    </div>

    <el-table :data="rows" v-loading="loading" stripe>
      <el-table-column label="Key / 标题" min-width="280">
        <template #default="{ row }">
          <div class="rb-title">{{ row.title }}</div>
          <div class="rb-key code-mono">{{ row.key }}</div>
        </template>
      </el-table-column>
      <el-table-column label="触发关键词" min-width="200">
        <template #default="{ row }">
          <el-tag v-for="t in row.triggers" :key="t" size="small" type="info" style="margin: 2px 4px 2px 0">{{ t }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="入参" width="180">
        <template #default="{ row }">
          <span v-for="i in row.inputs" :key="i" class="code-mono" style="margin-right:4px">{{ i }}</span>
          <span v-if="!row.inputs?.length" class="text-muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="节点" width="80" align="center">
        <template #default="{ row }">{{ row.node_count }}</template>
      </el-table-column>
      <el-table-column label="启用" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="版本" width="80" align="center">
        <template #default="{ row }">v{{ row.version }}</template>
      </el-table-column>
      <el-table-column label="操作" width="200" align="right" fixed="right">
        <template #default="{ row }">
          <el-button size="small" link @click="openEdit(row)">编辑</el-button>
          <el-button size="small" link type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="dlg.show"
      :title="dlg.create ? '新增剧本' : `编辑剧本：${dlg.form.key}`"
      width="980px"
      top="3vh"
      :close-on-click-modal="false"
    >
      <div class="edit-grid">
        <div class="edit-side">
          <el-form label-position="top" size="default">
            <el-form-item label="Key（创建后不可改）">
              <el-input v-model="dlg.form.key" :disabled="!dlg.create" placeholder="swarm_xxx_yyy" />
            </el-form-item>
            <el-form-item label="标题">
              <el-input v-model="dlg.form.title" />
            </el-form-item>
            <el-form-item label="描述">
              <el-input v-model="dlg.form.description" type="textarea" :rows="2" />
            </el-form-item>
            <el-form-item label="触发关键词">
              <el-input v-model="triggersText" placeholder="逗号分隔" type="textarea" :rows="2" />
              <div class="hint">用户原话命中任意一个就会用这个剧本</div>
            </el-form-item>
            <el-form-item label="必填入参">
              <el-input v-model="inputsText" placeholder="service_name,namespace 等，逗号分隔" />
            </el-form-item>
            <el-form-item label="启用"><el-switch v-model="dlg.form.enabled" /></el-form-item>
          </el-form>

          <div class="actions">
            <el-button @click="onValidate">仅校验</el-button>
            <el-button type="primary" @click="onSave">保存（自动校验+热加载）</el-button>
          </div>

          <div v-if="validateResult.errors?.length" class="errors">
            <div class="err-title">校验错误：</div>
            <ul>
              <li v-for="(e, i) in validateResult.errors" :key="i">{{ e }}</li>
            </ul>
          </div>
          <div v-else-if="validateResult.ok" class="ok-msg">✅ 校验通过</div>
        </div>

        <div class="edit-main">
          <div class="editor-label">剧本定义（YAML）</div>
          <CodeEditor v-model="dlg.yaml" language="yaml" :height="600" />
          <div class="hint" style="margin-top: 6px">
            参数引用：<code>$user.X</code> / <code>$nodes.&lt;id&gt;.&lt;path&gt;</code> /
            <code>$signals.&lt;type&gt;.&lt;field&gt;</code>。条件 type 支持
            <code>has_signal / status_ok / field_eq / any_of / not</code> 等。
          </div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { RouterLink } from 'vue-router'
import api from '../api'
import CodeEditor from '../components/CodeEditor.vue'

const rows = ref([])
const loading = ref(false)
const dlg = ref({
  show: false,
  create: false,
  form: { key: '', title: '', description: '', enabled: true },
  yaml: '',
})
const validateResult = ref({ ok: null, errors: [] })

const triggersText = computed({
  get: () => (dlg.value.form.triggers || []).join(', '),
  set: (v) => { dlg.value.form.triggers = v.split(/[,，]/).map(s => s.trim()).filter(Boolean) },
})
const inputsText = computed({
  get: () => (dlg.value.form.inputs || []).join(', '),
  set: (v) => { dlg.value.form.inputs = v.split(/[,，]/).map(s => s.trim()).filter(Boolean) },
})

async function load() {
  loading.value = true
  try { rows.value = (await api.get('/runbooks')).data }
  finally { loading.value = false }
}
onMounted(load)

function defaultYaml(key) {
  return `key: ${key}
title: 新剧本
description: 简要说明
triggers: []
inputs: []
start_node: step1
nodes:
  step1:
    skill: swarm_check_service_health
    args:
      service_name: $user.service_name
    edges:
      - target: step2

  step2:
    skill: swarm_get_failed_tasks
    args:
      service_name: $user.service_name
`
}

function openCreate() {
  dlg.value = {
    show: true,
    create: true,
    form: { key: '', title: '', description: '', enabled: true, triggers: [], inputs: [] },
    yaml: defaultYaml('new_runbook'),
  }
  validateResult.value = { ok: null, errors: [] }
}

async function openEdit(row) {
  const { data } = await api.get(`/runbooks/${row.key}`)
  const def = data.definition || {}
  dlg.value = {
    show: true,
    create: false,
    form: {
      key: data.key,
      title: data.title,
      description: data.description || '',
      triggers: data.triggers || [],
      inputs: data.inputs || [],
      enabled: data.enabled,
    },
    yaml: yamlStringify(def),
  }
  validateResult.value = { ok: null, errors: [] }
}

function yamlStringify(obj) {
  // 简版 yaml 输出（不依赖 js-yaml；够用且保持顺序）
  return _y(obj, 0)
}
function _y(v, indent) {
  const pad = '  '.repeat(indent)
  if (v === null || v === undefined) return 'null'
  if (typeof v === 'boolean') return String(v)
  if (typeof v === 'number') return String(v)
  if (typeof v === 'string') {
    if (/^[\w./:$\-_=*]+$/.test(v) && !/^\s|\s$/.test(v)) return v
    return JSON.stringify(v)
  }
  if (Array.isArray(v)) {
    if (!v.length) return '[]'
    return '\n' + v.map(x => `${pad}- ${_y(x, indent + 1).replace(/^\n/, '').trimStart()}`).join('\n')
  }
  if (typeof v === 'object') {
    const keys = Object.keys(v)
    if (!keys.length) return '{}'
    return '\n' + keys.map(k => `${pad}${k}: ${_y(v[k], indent + 1)}`.replace(/:\s*\n\s*\n/, ':\n')).join('\n')
  }
  return String(v)
}

function parseYaml(yamlText) {
  // 太重所以这里用 JSON fallback：admin 写不了 YAML 就让他们写 JSON。
  // 先尝试当 JSON parse；失败则要求装 js-yaml（生产建议加 dep）。
  const trimmed = yamlText.trim()
  if (trimmed.startsWith('{')) return JSON.parse(trimmed)
  // 极简 YAML → JS 解析（仅支持本平台用到的子集）
  return yamlToObj(yamlText)
}

// 极简 YAML 解析：覆盖本平台用法（key: value, 嵌套 dict, 数组, bool/null/number/string, $-引用）
function yamlToObj(text) {
  const lines = text.split('\n').filter(l => l.length === 0 || !/^\s*#/.test(l))
  let i = 0
  function curIndent(line) {
    return line.search(/\S|$/)
  }
  function parseScalar(s) {
    s = s.trim()
    if (s === '' || s === 'null' || s === '~') return null
    if (s === 'true') return true
    if (s === 'false') return false
    if (/^-?\d+$/.test(s)) return parseInt(s, 10)
    if (/^-?\d+\.\d+$/.test(s)) return parseFloat(s)
    if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) {
      return JSON.parse(s.replace(/^'/, '"').replace(/'$/, '"'))
    }
    return s
  }
  function parseBlock(indent) {
    // peek first non-empty line at this indent
    while (i < lines.length && lines[i].trim() === '') i++
    if (i >= lines.length) return null
    const first = lines[i]
    const ind = curIndent(first)
    if (ind < indent) return null
    if (first.trim().startsWith('-')) {
      // array
      const arr = []
      while (i < lines.length) {
        const ln = lines[i]
        if (ln.trim() === '') { i++; continue }
        const lnInd = curIndent(ln)
        if (lnInd < indent) break
        if (lnInd > indent) break
        const tail = ln.slice(lnInd + 1).trimStart() // 去掉 "- "
        // 行内有 key: 当作 inline dict 起点
        if (tail.includes(':') && !tail.startsWith('"')) {
          // 把 "- key: val" 视为 dict 第一项
          const dict = {}
          const colonIdx = tail.indexOf(':')
          const key = tail.slice(0, colonIdx).trim()
          const val = tail.slice(colonIdx + 1).trim()
          i++
          if (val === '') {
            dict[key] = parseBlock(lnInd + 2)
          } else {
            dict[key] = parseScalar(val)
          }
          // 继续读后续 indent>lnInd 的 key 当成同一 dict 的字段
          while (i < lines.length) {
            const nx = lines[i]
            if (nx.trim() === '') { i++; continue }
            const nxInd = curIndent(nx)
            if (nxInd <= lnInd) break
            const trimmed = nx.trim()
            const ci = trimmed.indexOf(':')
            if (ci < 0) break
            const k2 = trimmed.slice(0, ci).trim()
            const v2 = trimmed.slice(ci + 1).trim()
            i++
            if (v2 === '') dict[k2] = parseBlock(nxInd + 2)
            else dict[k2] = parseScalar(v2)
          }
          arr.push(dict)
        } else {
          i++
          arr.push(parseScalar(tail))
        }
      }
      return arr
    } else {
      // dict
      const dict = {}
      while (i < lines.length) {
        const ln = lines[i]
        if (ln.trim() === '') { i++; continue }
        const lnInd = curIndent(ln)
        if (lnInd < indent) break
        if (lnInd > indent) {
          // shouldn't happen; skip
          i++
          continue
        }
        const trimmed = ln.trim()
        const ci = trimmed.indexOf(':')
        if (ci < 0) { i++; continue }
        const key = trimmed.slice(0, ci).trim()
        const val = trimmed.slice(ci + 1).trim()
        i++
        if (val === '') {
          dict[key] = parseBlock(lnInd + 2)
        } else {
          dict[key] = parseScalar(val)
        }
      }
      return dict
    }
  }
  return parseBlock(0)
}

async function buildPayload() {
  let definition
  try {
    definition = parseYaml(dlg.value.yaml)
  } catch (e) {
    throw new Error('YAML/JSON 解析失败：' + e.message)
  }
  if (!definition || typeof definition !== 'object') {
    throw new Error('definition 不是一个对象')
  }
  // 同步表单字段
  definition.key = dlg.value.form.key
  definition.title = dlg.value.form.title || dlg.value.form.key
  definition.description = dlg.value.form.description || ''
  definition.triggers = dlg.value.form.triggers || []
  definition.inputs = dlg.value.form.inputs || []
  definition.enabled = !!dlg.value.form.enabled
  return definition
}

async function onValidate() {
  validateResult.value = { ok: null, errors: [] }
  try {
    const def = await buildPayload()
    const { data } = await api.post(`/runbooks/${dlg.value.form.key}/validate`, { definition: def })
    validateResult.value = data
    data.ok ? ElMessage.success('校验通过') : ElMessage.warning('校验未通过')
  } catch (e) {
    validateResult.value = { ok: false, errors: [e.message || String(e)] }
  }
}

async function onSave() {
  try {
    const def = await buildPayload()
    if (!def.key) { ElMessage.warning('Key 必填'); return }
    const { data } = await api.put(`/runbooks/${def.key}`, { definition: def })
    if (data.error) {
      validateResult.value = { ok: false, errors: data.errors || [data.message || data.error] }
      ElMessage.error('保存被拒：' + (data.message || data.error))
      return
    }
    ElMessage.success('已保存并热加载')
    dlg.value.show = false
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.message || e.message || '保存失败')
  }
}

async function onDelete(row) {
  await ElMessageBox.confirm(`确认删除剧本 ${row.key}？`, '提示', { type: 'warning' })
  await api.delete(`/runbooks/${row.key}`)
  ElMessage.success('已删除')
  await load()
}
</script>

<style scoped>
.rb-title { font-weight: 500; color: var(--text-primary); font-size: 14px; }
.rb-key { font-size: 11.5px; color: var(--text-muted); margin-top: 3px; }
.link-btn { color: var(--brand-700); text-decoration: none; font-size: 13px; padding: 6px 10px; }
.link-btn:hover { text-decoration: underline; }

.edit-grid { display: grid; grid-template-columns: 320px 1fr; gap: 18px; }
.edit-side { display: flex; flex-direction: column; }
.edit-main { display: flex; flex-direction: column; }

.editor-label {
  font-size: 12px; font-weight: 600; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px;
}
.actions {
  display: flex; gap: 8px; margin-top: 4px;
}
.errors {
  margin-top: 12px; padding: 10px 14px;
  background: #fef2f2; border: 1px solid #fecaca;
  border-radius: var(--radius-sm);
  color: #991b1b; font-size: 12.5px;
}
.err-title { font-weight: 600; margin-bottom: 4px; }
.errors ul { margin: 0; padding-left: 20px; }
.ok-msg {
  margin-top: 12px; padding: 10px 14px;
  background: var(--brand-50);
  color: var(--brand-700);
  border: 1px solid var(--brand-100);
  border-radius: var(--radius-sm);
  font-size: 12.5px;
}
.hint { color: var(--text-muted); font-size: 12px; margin-top: 4px; line-height: 1.5; }
.hint code { font-family: var(--font-mono); background: var(--bg-soft); padding: 1px 5px; border-radius: 3px; color: var(--brand-700); }
code { font-family: var(--font-mono); background: var(--bg-soft); padding: 1px 5px; border-radius: 3px; color: var(--brand-700); }
</style>
