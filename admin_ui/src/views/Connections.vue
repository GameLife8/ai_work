<template>
  <div>
    <h1 class="page-title">接入管理</h1>
    <p class="page-subtitle">
      每个接入是一份某种类型的具体连接配置（凭证 + 地址）。同类型可存多份，会话/skill 调用时按 connection_id 路由。
      <el-link type="primary" :underline="false" @click="showTypeHelp = !showTypeHelp" style="margin-left: 8px">
        {{ showTypeHelp ? '收起' : '什么是接入类型？' }}
      </el-link>
    </p>

    <el-alert v-if="showTypeHelp" type="info" :closable="false" style="margin-bottom: 14px">
      <div class="type-help">
        <p class="help-intro">
          一个真实集群通常需要 <b>多种类型的接入同时存在</b>——它们职责不同，配合工作。
          以"SWS Swarm 集群"举例：
          <code>swarm</code> 类型管 docker manager（看服务/扩缩容），
          <code>host_agent</code> 类型管每个节点上的诊断 Agent（在宿主机执行命令）。
          用<b>标签</b>把同一集群的多个接入聚合成"逻辑集群"，LLM 路由时识别。
        </p>
        <div class="type-grid">
          <div class="type-card" v-for="t in TYPE_HELP" :key="t.code">
            <div class="type-card-head">
              <span class="code-mono type-code-pill" :style="{background: t.color}">{{ t.code }}</span>
              <span class="type-name">{{ t.name }}</span>
            </div>
            <div class="type-desc">{{ t.desc }}</div>
            <div class="type-when" v-if="t.when">何时用：{{ t.when }}</div>
          </div>
        </div>
      </div>
    </el-alert>

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
          <el-tooltip :content="typeTooltip(row.type_code)" placement="right" :show-after="200">
            <span class="code-mono">{{ row.type_code }}</span>
          </el-tooltip>
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
const showTypeHelp = ref(false)
const dlg = ref({ show: false, id: null, form: { type_code: '', name: '', alias: '', is_default: false, tags: [], config: {} } })
const currentFields = computed(() => drivers.value.find(d => d.type === dlg.value.form.type_code)?.fields || [])

// 接入类型解释——给运维快速理解每种 type 的职责。
// 颜色按 category 区分：监控蓝、容器编排紫、宿主层橙、其他灰。
const TYPE_HELP = [
  {
    code: 'zabbix', name: 'Zabbix 监控',
    color: '#3b82f6',
    desc: '对接 Zabbix API，拉主机/服务指标、告警事件、历史趋势。',
    when: '需要看 CPU/内存/磁盘趋势或告警时间线时。',
  },
  {
    code: 'swarm', name: 'Docker Swarm 集群',
    color: '#7c3aed',
    desc: '对接 Swarm manager 的 docker daemon（DOCKER_HOST=tcp://...），管服务（list/scale/rollout/inspect）。',
    when: '看服务副本状态、调副本数、回滚镜像、查失败 Task 时。',
  },
  {
    code: 'k8s', name: 'Kubernetes 集群',
    color: '#7c3aed',
    desc: '用 kubeconfig 调 K8s API Server，管 Pod/Deployment/DaemonSet/Node。',
    when: '看 pod 状态、describe pod、滚动重启 deployment、看 node 容量时。',
  },
  {
    code: 'host_agent', name: '节点诊断 Agent',
    color: '#ea580c',
    desc: '对接每个节点上跑的 ai-ops-agent（DaemonSet/global service），用 HTTP /v1/exec 在宿主机 namespace 跑 ss/iptables/tcpdump/dmesg/du/find 等取证命令。',
    when: '需要进宿主机执行命令——网络/磁盘/内核取证、长命令异步任务。',
  },
  {
    code: 'http_api', name: 'HTTP API（外部系统）',
    color: '#64748b',
    desc: '通用 HTTP 接入。配 base_url + auth 后，配合"HTTP Skill"可包装任意外部 REST API 成 skill。',
    when: '想把工单/CMDB/堡垒机这类系统的 API 接进来给 LLM 调时。',
  },
  {
    code: 'mcp_client', name: '外部 MCP Server',
    color: '#64748b',
    desc: '反向接入符合 Anthropic MCP 协议的外部工具服务，启动时拉远端 tools 列表注册成本地 skill。',
    when: '已有 MCP server 想直接复用其 tools 时。',
  },
  {
    code: 'alert_analysis', name: '内置告警分析',
    color: '#94a3b8',
    desc: '平台内部驱动，不连外部服务。结构化解析 Zabbix/普罗等系统推过来的 alert payload。',
    when: '系统自带，一般不用动。',
  },
]
const TYPE_HELP_MAP = Object.fromEntries(TYPE_HELP.map(t => [t.code, t]))

// typeColor 之前给表格"类型"列的彩色徽章用；用户反馈想保留原始的等宽字体纯文本，
// 已去掉徽章效果。颜色现在只在顶部"什么是接入类型？"面板的卡片里出现，那里直接
// 用 t.color，所以不再需要这个函数。

function typeTooltip(code) {
  const t = TYPE_HELP_MAP[code]
  if (!t) return code
  return `${t.name}：${t.desc}` + (t.when ? `\n何时用：${t.when}` : '')
}

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

/* 类型徽章——色块 + 等宽字体，鼠标悬停看完整描述 */
.type-code-pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.3px;
  white-space: nowrap;
  cursor: help;
}

/* 类型帮助面板 */
.type-help { font-size: 13px; }
.help-intro {
  color: var(--text-secondary);
  margin: 0 0 12px;
  line-height: 1.7;
}
.help-intro code {
  background: rgba(15, 23, 42, 0.06);
  padding: 1px 6px;
  border-radius: 3px;
  font-family: var(--font-mono);
  font-size: 12px;
}
.type-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 10px;
}
.type-card {
  background: #fff;
  border: 1px solid var(--border-soft, #e2e8f0);
  border-radius: 6px;
  padding: 10px 12px;
}
.type-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.type-name { font-weight: 600; font-size: 13px; }
.type-desc {
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.6;
}
.type-when {
  margin-top: 4px;
  color: #16a34a;
  font-size: 12px;
}
</style>
