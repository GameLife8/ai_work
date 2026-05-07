<template>
  <div>
    <h1 class="page-title">HTTP Skill（外部系统接入）</h1>
    <p class="page-subtitle">
      用 YAML 声明一个外部 HTTP API 端点 → 自动变成平台 skill。
      <b>不写 Python 代码、不重启服务</b>，admin 改完即时生效。
      Jira / GitLab / 内部 CMDB / 工单系统都用这个接入。
    </p>

    <div class="toolbar">
      <el-button type="primary" @click="openCreate">+ 新增 HTTP Skill</el-button>
      <el-button @click="load">刷新</el-button>
      <span class="toolbar-right text-muted" style="font-size: 12px">{{ rows.length }} 条</span>
    </div>

    <el-table :data="rows" v-loading="loading" stripe>
      <el-table-column label="Code / 标题" min-width="280">
        <template #default="{ row }">
          <div class="rb-title">{{ row.title }}</div>
          <div class="rb-key code-mono">{{ row.code }}</div>
        </template>
      </el-table-column>
      <el-table-column label="HTTP" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="methodTag(row.method)">{{ row.method }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="Path" min-width="220">
        <template #default="{ row }"><span class="code-mono">{{ row.path }}</span></template>
      </el-table-column>
      <el-table-column label="连接" width="180">
        <template #default="{ row }">
          <span v-if="row.connection_id" class="code-mono">{{ row.connection_id }}</span>
          <span v-else class="text-muted">默认</span>
        </template>
      </el-table-column>
      <el-table-column label="只读" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.read_only ? 'success' : 'warning'">{{ row.read_only ? '只读' : '写' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="审批" width="80">
        <template #default="{ row }">
          <el-tag v-if="row.requires_admin_approval" size="small" type="danger">admin</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="版本" width="70" align="center">
        <template #default="{ row }">v{{ row.version }}</template>
      </el-table-column>
      <el-table-column label="操作" width="180" align="right">
        <template #default="{ row }">
          <el-button size="small" link @click="openEdit(row)">编辑</el-button>
          <el-button size="small" link type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="dlg.show"
      :title="dlg.create ? '新增 HTTP Skill' : `编辑 HTTP Skill：${dlg.code}`"
      width="980px"
      top="3vh"
      :close-on-click-modal="false"
    >
      <div class="edit-grid">
        <div class="edit-side">
          <el-form label-position="top" size="default">
            <el-form-item label="Skill Code（创建后不可改）">
              <el-input v-model="dlg.code" :disabled="!dlg.create" placeholder="jira_search_issues" />
            </el-form-item>
            <el-form-item label="说明">
              <el-input v-model="dlg.summary" type="textarea" :rows="2" :disabled="true" />
              <div class="hint">从 YAML 的 description 字段自动同步</div>
            </el-form-item>
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

          <div class="ref-card">
            <div class="ref-title">YAML 字段速查</div>
            <ul>
              <li><code>method</code> + <code>path</code> 必填</li>
              <li><code>connection_id</code> 绑定 http_api connection</li>
              <li><code>read_only=false</code> 时必走 needs_confirmation</li>
              <li><code>params_schema</code> 给模型用的工具 schema</li>
              <li><code>body_template</code> Jinja2 渲染（可用 <code>{{'{{'}} x {{'}}'}}</code>、<code>tojson</code>、<code>now()</code>）</li>
              <li><code>response_extract</code> 用 <code>$.path[*].field</code> 精简响应</li>
              <li><code>signal_rules</code> 把响应转成跨域信号</li>
            </ul>
          </div>
        </div>

        <div class="edit-main">
          <div class="editor-label">Skill 定义（YAML）</div>
          <CodeEditor v-model="dlg.yaml" language="yaml" :height="640" />
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'
import CodeEditor from '../components/CodeEditor.vue'

const rows = ref([])
const loading = ref(false)
const dlg = ref({ show: false, create: false, code: '', yaml: '', summary: '' })
const validateResult = ref({ ok: null, errors: [] })

function methodTag(m) {
  return ({ GET: 'success', POST: 'warning', PUT: 'warning', PATCH: 'warning', DELETE: 'danger' })[m] || 'info'
}

function templateYaml(code) {
  return `code: ${code}
name: 示例 - 调用外部系统
description: |
  何时使用本 skill；返回字段说明；信号→下一步建议。
  这段是给模型看的，写得越具体模型越能精准选择。
category: integration
connection_id:                    # 留空则用平台默认 http_api connection
read_only: true
visibility: all

method: GET
path: /api/v1/example/{{ id }}

# 可选：query string
query_template:
  expand: "true"

# 可选：调用级 header（与 connection custom_headers 合并）
headers_template:
  X-Request-ID: "{{ uuid() }}"

# 可选：POST/PUT/PATCH 用，Jinja2 渲染后须为合法 JSON
# body_template: |
#   { "title": "{{ title }}", "items": {{ items | tojson }} }

params_schema:
  type: object
  required: [id]
  properties:
    id:
      type: string
      description: 示例 ID

# 精简响应：只把模型真正需要的字段提出来
response_extract:
  status: '$.status'
  items: '$.data.items[*]{id, name}'

# 把异常响应转成结构化信号驱动跨域 pivot
signal_rules:
  - when: { type: http_status, eq: 401 }
    emit:
      type: auth_failure
      severity: critical
      evidence: "外部系统返回 401，凭证可能失效"
  - when: { type: http_status, gte: 500 }
    emit:
      type: backend_error
      severity: critical
      evidence: "后端 5xx，建议排查上游"

timeout_seconds: 30
retry: 0
`
}

async function load() {
  loading.value = true
  try { rows.value = (await api.get('/http-skills')).data }
  finally { loading.value = false }
}
onMounted(load)

function openCreate() {
  dlg.value = { show: true, create: true, code: 'example_skill', yaml: templateYaml('example_skill'), summary: '' }
  validateResult.value = { ok: null, errors: [] }
  syncSummary()
}

async function openEdit(row) {
  const { data } = await api.get(`/http-skills/${row.code}`)
  dlg.value = {
    show: true,
    create: false,
    code: data.code,
    yaml: yamlStringify(data.definition || {}),
    summary: '',
  }
  validateResult.value = { ok: null, errors: [] }
  syncSummary()
}

function syncSummary() {
  const m = (dlg.value.yaml || '').match(/description:\s*\|?\s*\n([\s\S]*?)(?=\n[a-z_]+:)/)
  dlg.value.summary = m ? m[1].trim().split('\n')[0] : ''
}
watch(() => dlg.value.yaml, syncSummary)

function yamlStringify(obj) { return _y(obj, 0) }
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
    return '\n' + keys.map(k => `${pad}${k}: ${_y(v[k], indent + 1)}`).join('\n')
  }
  return String(v)
}

// 复用 Runbooks 那个简版 YAML 解析器
function parseYaml(yamlText) {
  const trimmed = yamlText.trim()
  if (trimmed.startsWith('{')) return JSON.parse(trimmed)
  return yamlToObj(yamlText)
}
function yamlToObj(text) {
  const lines = text.split('\n').filter(l => l.length === 0 || !/^\s*#/.test(l))
  let i = 0
  function curIndent(line) { return line.search(/\S|$/) }
  function parseScalar(s) {
    s = s.trim()
    if (s === '' || s === 'null' || s === '~') return null
    if (s === 'true') return true
    if (s === 'false') return false
    if (/^-?\d+$/.test(s)) return parseInt(s, 10)
    if (/^-?\d+\.\d+$/.test(s)) return parseFloat(s)
    if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) {
      try { return JSON.parse(s.replace(/^'/, '"').replace(/'$/, '"')) } catch { return s.slice(1, -1) }
    }
    return s
  }
  function parseBlock(indent) {
    while (i < lines.length && lines[i].trim() === '') i++
    if (i >= lines.length) return null
    const first = lines[i]
    const ind = curIndent(first)
    if (ind < indent) return null
    if (first.trim().startsWith('-')) {
      const arr = []
      while (i < lines.length) {
        const ln = lines[i]
        if (ln.trim() === '') { i++; continue }
        const lnInd = curIndent(ln)
        if (lnInd < indent) break
        if (lnInd > indent) break
        const tail = ln.slice(lnInd + 1).trimStart()
        if (tail.includes(':') && !tail.startsWith('"')) {
          const dict = {}
          const ci = tail.indexOf(':')
          const key = tail.slice(0, ci).trim()
          const val = tail.slice(ci + 1).trim()
          i++
          if (val === '') dict[key] = parseBlock(lnInd + 2)
          else dict[key] = parseScalar(val)
          while (i < lines.length) {
            const nx = lines[i]
            if (nx.trim() === '') { i++; continue }
            const nxInd = curIndent(nx)
            if (nxInd <= lnInd) break
            const trimmed = nx.trim()
            const ci2 = trimmed.indexOf(':')
            if (ci2 < 0) break
            const k2 = trimmed.slice(0, ci2).trim()
            const v2 = trimmed.slice(ci2 + 1).trim()
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
      const dict = {}
      while (i < lines.length) {
        const ln = lines[i]
        if (ln.trim() === '') { i++; continue }
        const lnInd = curIndent(ln)
        if (lnInd < indent) break
        if (lnInd > indent) { i++; continue }
        const trimmed = ln.trim()
        const ci = trimmed.indexOf(':')
        if (ci < 0) { i++; continue }
        const key = trimmed.slice(0, ci).trim()
        const val = trimmed.slice(ci + 1).trim()
        i++
        if (val === '') {
          // pipe-multiline: |
          if (ln.includes('|') || ln.endsWith(':')) {
            // 探测下面是不是缩进文本块
            const blockLines = []
            const baseInd = lnInd + 2
            while (i < lines.length) {
              const nx = lines[i]
              if (nx.trim() === '') { blockLines.push(''); i++; continue }
              if (curIndent(nx) < baseInd) break
              blockLines.push(nx.slice(baseInd))
              i++
            }
            if (blockLines.length && val === '' && ln.trim().endsWith('|')) {
              dict[key] = blockLines.join('\n').replace(/\n+$/, '\n')
              continue
            }
            // 把已读取的退回（相当于不当作多行处理）
            i -= blockLines.length
          }
          dict[key] = parseBlock(lnInd + 2)
        } else dict[key] = parseScalar(val)
      }
      return dict
    }
  }
  return parseBlock(0)
}

async function buildPayload() {
  let definition
  try { definition = parseYaml(dlg.value.yaml) }
  catch (e) { throw new Error('YAML/JSON 解析失败：' + e.message) }
  if (!definition || typeof definition !== 'object') throw new Error('definition 不是对象')
  definition.code = dlg.value.code
  return definition
}

async function onValidate() {
  validateResult.value = { ok: null, errors: [] }
  try {
    const def = await buildPayload()
    const { data } = await api.post(`/http-skills/${dlg.value.code}/validate`, { definition: def })
    validateResult.value = data
    data.ok ? ElMessage.success('校验通过') : ElMessage.warning('校验未通过')
  } catch (e) {
    validateResult.value = { ok: false, errors: [e.message || String(e)] }
  }
}

async function onSave() {
  try {
    const def = await buildPayload()
    if (!def.code) { ElMessage.warning('Code 必填'); return }
    const { data } = await api.put(`/http-skills/${def.code}`, { definition: def })
    if (data.error) {
      validateResult.value = { ok: false, errors: data.errors || [data.message || data.error] }
      ElMessage.error('保存被拒：' + (data.message || data.error))
      return
    }
    ElMessage.success('已保存并热加载')
    dlg.value.show = false
    await load()
  } catch (e) { ElMessage.error(e.response?.data?.message || e.message || '保存失败') }
}

async function onDelete(row) {
  await ElMessageBox.confirm(`确认删除 ${row.code}？`, '提示', { type: 'warning' })
  await api.delete(`/http-skills/${row.code}`)
  ElMessage.success('已删除')
  await load()
}
</script>

<style scoped>
.rb-title { font-weight: 500; color: var(--text-primary); font-size: 14px; }
.rb-key { font-size: 11.5px; color: var(--text-muted); margin-top: 3px; font-family: var(--font-mono); }

.edit-grid { display: grid; grid-template-columns: 320px 1fr; gap: 18px; }
.edit-side { display: flex; flex-direction: column; }
.edit-main { display: flex; flex-direction: column; }
.editor-label {
  font-size: 12px; font-weight: 600; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px;
}
.actions { display: flex; gap: 8px; margin-top: 4px; }
.errors {
  margin-top: 12px; padding: 10px 14px;
  background: #fef2f2; border: 1px solid #fecaca;
  border-radius: var(--radius-sm); color: #991b1b; font-size: 12.5px;
}
.err-title { font-weight: 600; margin-bottom: 4px; }
.errors ul { margin: 0; padding-left: 20px; }
.ok-msg {
  margin-top: 12px; padding: 10px 14px;
  background: var(--brand-50); color: var(--brand-700);
  border: 1px solid var(--brand-100); border-radius: var(--radius-sm);
  font-size: 12.5px;
}
.ref-card {
  margin-top: 16px; padding: 12px 14px;
  background: var(--bg-soft); border-radius: var(--radius-sm);
  font-size: 12.5px; line-height: 1.7; color: var(--text-secondary);
}
.ref-title {
  font-weight: 600; color: var(--text-primary); font-size: 12px;
  text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px;
}
.ref-card ul { margin: 0; padding-left: 16px; }
.hint { color: var(--text-muted); font-size: 12px; margin-top: 4px; }
code { font-family: var(--font-mono); background: var(--bg-card); padding: 1px 5px; border-radius: 3px; color: var(--brand-700); }
</style>
