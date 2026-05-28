<template>
  <div>
    <h1 class="page-title">节点维护 · 清理残留容器</h1>
    <p class="page-subtitle">
      扫描所有 host_agent 集群里每个节点上残留的 stopped 容器(主要是 ``docker_proxy``
      模式下没 <code>--rm</code> 干净的 sibling 容器),勾选后下发 ``docker rm`` 清理。
      默认按镜像名 <code>ai-ops-agent</code> 过滤,留空可看节点上**全部** stopped 容器。
    </p>

    <div class="toolbar">
      <el-input
        v-model="imageFilter"
        placeholder="镜像名子串(留空看全部)"
        size="default"
        clearable
        style="width: 280px"
      >
        <template #prepend>镜像过滤</template>
      </el-input>
      <el-button type="primary" :loading="scanning" @click="scan">扫描所有节点</el-button>
      <el-button
        type="danger"
        :disabled="selected.length === 0"
        :loading="cleaning"
        @click="cleanupSelected"
      >
        清理选中({{ selected.length }})
      </el-button>
      <span class="toolbar-right text-muted" style="font-size: 12px">
        共扫描 {{ totalNodes }} 个节点,发现 {{ totalLeftovers }} 个残留容器
      </span>
    </div>

    <el-alert
      v-if="globalErrors.length"
      type="warning" :closable="false" show-icon
      :title="`${globalErrors.length} 个 connection 拿节点列表失败`"
      style="margin-bottom: 12px"
    >
      <ul style="margin: 4px 0; padding-left: 18px">
        <li v-for="e in globalErrors" :key="e.connection_id">
          <strong>{{ e.alias }}</strong>({{ e.phase }}):{{ e.error }}
        </li>
      </ul>
    </el-alert>

    <div v-if="!scanned && !scanning" class="empty-state">
      <el-empty description="点击「扫描所有节点」开始检查" />
    </div>

    <div v-for="conn in connections" :key="conn.connection_id" class="conn-block">
      <div class="conn-header">
        <span class="conn-kind" :class="`kind-${conn.kind}`">{{ conn.kind || '?' }}</span>
        <strong>{{ conn.alias }}</strong>
        <span class="text-muted">
          {{ conn.nodes.length }} 个节点 ·
          {{ countConnLeftovers(conn) }} 个残留
        </span>
      </div>

      <div v-for="nodeBlock in conn.nodes" :key="`${conn.connection_id}-${nodeBlock.node}`" class="node-block">
        <div class="node-header">
          <span class="node-name">🖥️ {{ nodeBlock.node }}</span>
          <span v-if="nodeBlock.error" class="text-danger">扫描失败:{{ nodeBlock.error }}</span>
          <span v-else-if="!nodeBlock.leftovers || nodeBlock.leftovers.length === 0"
                class="text-success">✅ 无残留</span>
          <span v-else class="text-warning">⚠️ {{ nodeBlock.leftovers.length }} 个残留</span>
        </div>

        <el-table
          v-if="nodeBlock.leftovers && nodeBlock.leftovers.length > 0"
          :data="nodeBlock.leftovers"
          stripe
          size="small"
          @selection-change="(rows) => onNodeSelectionChange(conn, nodeBlock, rows)"
        >
          <el-table-column type="selection" width="42" />
          <el-table-column prop="container_id" label="Container ID" width="160">
            <template #default="{ row }">
              <span class="mono">{{ row.container_id.slice(0, 12) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="image" label="Image" min-width="200">
            <template #default="{ row }">
              <span class="mono">{{ row.image }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="容器名" min-width="180">
            <template #default="{ row }">
              <span class="mono">{{ row.name }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="status" label="状态" min-width="180" />
          <el-table-column prop="created_at" label="创建时间" min-width="180" />
        </el-table>
      </div>
    </div>

    <!-- 清理结果弹窗 -->
    <el-dialog v-model="resultDialogVisible" title="清理结果" width="60%">
      <div v-for="(r, idx) in cleanupResults" :key="idx" class="result-row">
        <div class="result-header">
          <span :class="r.ok ? 'text-success' : 'text-danger'">
            {{ r.ok ? '✅' : '❌' }}
          </span>
          <strong>{{ r.node }}</strong>
          <span class="text-muted">
            ({{ r.requested && r.requested.length }} 个容器)
          </span>
        </div>
        <pre v-if="r.stdout" class="result-output">{{ r.stdout }}</pre>
        <pre v-if="r.stderr" class="result-output text-danger">{{ r.stderr }}</pre>
        <pre v-if="r.error" class="result-output text-danger">{{ r.error }}</pre>
      </div>
      <template #footer>
        <el-button @click="resultDialogVisible = false">关闭</el-button>
        <el-button type="primary" @click="resultDialogVisible = false; scan()">重新扫描</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const imageFilter = ref('ai-ops-agent')
const scanning = ref(false)
const cleaning = ref(false)
const scanned = ref(false)
const connections = ref([])
const globalErrors = ref([])

// 选中状态:Map<`${connId}::${node}`, container_id[]>
const selectionMap = reactive({})

const selected = computed(() => {
  // 摊平成 [{connection_id, node, container_ids}] 列表
  const items = []
  for (const key in selectionMap) {
    const ids = selectionMap[key]
    if (!ids || ids.length === 0) continue
    const [connection_id, node] = key.split('::')
    items.push({ connection_id, node, container_ids: ids })
  }
  return items
})

const totalNodes = computed(() =>
  connections.value.reduce((sum, c) => sum + c.nodes.length, 0)
)
const totalLeftovers = computed(() =>
  connections.value.reduce((sum, c) => sum + countConnLeftovers(c), 0)
)

function countConnLeftovers(conn) {
  return conn.nodes.reduce((sum, n) => sum + ((n.leftovers || []).length), 0)
}

function onNodeSelectionChange(conn, nodeBlock, rows) {
  const key = `${conn.connection_id}::${nodeBlock.node}`
  selectionMap[key] = rows.map(r => r.container_id)
}

async function scan() {
  scanning.value = true
  // 清掉旧选择
  for (const k in selectionMap) delete selectionMap[k]
  try {
    const params = { image_filter: imageFilter.value || '' }
    const { data } = await api.get('/maintenance/sibling-containers', { params })
    connections.value = data.connections || []
    globalErrors.value = data.errors || []
    scanned.value = true
    if (totalLeftovers.value === 0) {
      ElMessage.success('扫描完成,所有节点没有残留容器')
    } else {
      ElMessage.warning(`扫描完成,发现 ${totalLeftovers.value} 个残留容器`)
    }
  } catch (e) {
    if (!e.handled) ElMessage.error('扫描失败:' + (e.response?.data?.error || e.message))
  } finally {
    scanning.value = false
  }
}

const resultDialogVisible = ref(false)
const cleanupResults = ref([])

async function cleanupSelected() {
  if (selected.value.length === 0) return
  cleaning.value = true
  try {
    const { data } = await api.post(
      '/maintenance/sibling-containers/cleanup',
      { items: selected.value },
    )
    cleanupResults.value = data.results || []
    resultDialogVisible.value = true
    const okCount = cleanupResults.value.filter(r => r.ok).length
    const failCount = cleanupResults.value.length - okCount
    if (failCount === 0) {
      ElMessage.success(`已清理 ${okCount} 个节点上的容器`)
    } else {
      ElMessage.warning(`${okCount} 成功 · ${failCount} 失败,查看详情`)
    }
  } catch (e) {
    if (!e.handled) ElMessage.error('清理失败:' + (e.response?.data?.error || e.message))
  } finally {
    cleaning.value = false
  }
}
</script>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.toolbar-right {
  margin-left: auto;
}

.conn-block {
  margin-bottom: 20px;
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-md);
  padding: 14px 16px;
}
.conn-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border-soft);
  margin-bottom: 12px;
  font-size: 14px;
}
.conn-kind {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 600;
  letter-spacing: 0.4px;
  text-transform: uppercase;
  background: var(--bg-soft);
  color: var(--text-muted);
}
.conn-kind.kind-swarm { background: #dbeafe; color: #1d4ed8; }
.conn-kind.kind-k8s   { background: #ddf4ff; color: #0e7490; }

.node-block {
  margin: 10px 0 16px;
}
.node-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 0;
  font-size: 13px;
}
.node-name {
  font-weight: 500;
}
.empty-state {
  margin-top: 40px;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12.5px;
}
.text-success { color: var(--success, #16a34a); }
.text-danger { color: var(--danger, #dc2626); }
.text-warning { color: var(--warning, #d97706); }
.text-muted { color: var(--text-muted); }

.result-row {
  margin-bottom: 12px;
  padding: 10px 12px;
  background: var(--bg-soft);
  border-radius: var(--radius-sm);
}
.result-header {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 6px;
}
.result-output {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  white-space: pre-wrap;
  margin: 4px 0 0;
  padding: 6px 8px;
  background: var(--bg-card);
  border-radius: var(--radius-xs);
  max-height: 240px;
  overflow-y: auto;
}
</style>
