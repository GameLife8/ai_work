<template>
  <div>
    <h1 class="page-title">提示词管理</h1>
    <p class="page-subtitle">
      System prompt 拆成 5 段落库。改完 <b>立即对下一次会话生效</b>，不用重启服务。
      想恢复默认点"重置"即可。
    </p>

    <div class="toolbar">
      <el-button type="primary" plain @click="showAssembled">查看完整拼接结果</el-button>
      <el-button @click="load">刷新</el-button>
      <span class="toolbar-right text-muted" style="font-size: 12px">{{ rows.length }} 段</span>
    </div>

    <div class="seg-grid">
      <div v-for="seg in rows" :key="seg.key" class="seg-card">
        <div class="seg-head">
          <div>
            <div class="seg-title">{{ seg.title }}</div>
            <div class="seg-key code-mono">{{ seg.key }}</div>
          </div>
          <div class="seg-actions">
            <el-tag v-if="seg.is_custom" size="small" type="success">已定制 v{{ seg.version }}</el-tag>
            <el-tag v-else size="small" type="info">出厂默认</el-tag>
            <el-switch
              v-model="seg.enabled"
              size="small"
              :disabled="!seg.is_custom"
              inline-prompt
              active-text="启用"
              inactive-text="禁用"
              @change="onToggle(seg)"
            />
          </div>
        </div>

        <CodeEditor
          v-model="seg.content"
          language="plain"
          :height="240"
          :wrap="true"
        />

        <div class="seg-foot">
          <div class="seg-meta">
            <span v-if="seg.updated_at">{{ formatTime(seg.updated_at) }} · {{ seg.updated_by }}</span>
            <span v-else class="text-muted">尚未定制，使用出厂内容</span>
          </div>
          <div>
            <el-button v-if="seg.is_custom" size="small" @click="onReset(seg)">重置出厂</el-button>
            <el-button size="small" @click="onPreviewDiff(seg)">对比默认</el-button>
            <el-button size="small" type="primary" @click="onSave(seg)">保存</el-button>
          </div>
        </div>
      </div>
    </div>

    <!-- 完整拼接对话框 -->
    <el-dialog v-model="assembled.show" title="完整 SYSTEM_PROMPT 预览" width="780px" top="6vh">
      <div class="text-secondary" style="font-size: 12px; margin-bottom: 8px">
        这是 5 段按顺序拼接后传给模型的完整内容。
      </div>
      <CodeEditor
        :model-value="assembled.content"
        language="plain"
        readonly
        :height="500"
        :wrap="true"
      />
    </el-dialog>

    <!-- 对比默认对话框 -->
    <el-dialog v-model="diff.show" :title="`对比默认：${diff.row?.title}`" width="900px" top="5vh">
      <div class="diff-grid">
        <div>
          <div class="diff-label">出厂默认</div>
          <CodeEditor :model-value="diff.row?.default_content" language="plain" readonly :height="420" :wrap="true" />
        </div>
        <div>
          <div class="diff-label">当前内容</div>
          <CodeEditor :model-value="diff.row?.content" language="plain" readonly :height="420" :wrap="true" />
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'
import CodeEditor from '../components/CodeEditor.vue'

const rows = ref([])
const assembled = ref({ show: false, content: '' })
const diff = ref({ show: false, row: null })

async function load() {
  rows.value = (await api.get('/prompts')).data
}

onMounted(load)

async function onSave(seg) {
  try {
    await api.put(`/prompts/${seg.key}`, {
      title: seg.title,
      content: seg.content,
      enabled: seg.enabled,
    })
    ElMessage.success('已保存，下一次会话生效')
    await load()
  } catch (e) { ElMessage.error(e.response?.data?.error || '保存失败') }
}

async function onToggle(seg) {
  // 只对已定制段落起作用；切换后自动 PUT 一次
  if (seg.is_custom) await onSave(seg)
}

async function onReset(seg) {
  try {
    await ElMessageBox.confirm(
      `确认把 ${seg.title} 重置回出厂内容？已定制的内容会被清空。`,
      '重置确认',
      { type: 'warning' },
    )
  } catch { return }
  await api.delete(`/prompts/${seg.key}`)
  ElMessage.success('已重置')
  await load()
}

function onPreviewDiff(seg) {
  diff.value = { show: true, row: seg }
}

async function showAssembled() {
  const { data } = await api.get('/prompts/_assembled')
  assembled.value = { show: true, content: data.content || '' }
}

function formatTime(s) {
  if (!s) return ''
  try { return new Date(s).toLocaleString('zh-CN', { hour12: false }) } catch { return s }
}
</script>

<style scoped>
.seg-grid {
  display: grid;
  gap: 16px;
}
.seg-card {
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-md);
  padding: 16px 18px;
  transition: border-color .2s;
}
.seg-card:hover { border-color: var(--border); }
.seg-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 12px;
}
.seg-title {
  font-weight: 600;
  font-size: 14.5px;
  color: var(--text-primary);
}
.seg-key {
  font-size: 11.5px;
  color: var(--text-muted);
  margin-top: 3px;
}
.seg-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
.seg-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 10px;
}
.seg-meta {
  font-size: 12px;
  color: var(--text-secondary);
}

.diff-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.diff-label {
  font-size: 12px;
  color: var(--text-muted);
  font-weight: 600;
  margin-bottom: 6px;
  text-transform: uppercase;
  letter-spacing: .5px;
}
</style>
