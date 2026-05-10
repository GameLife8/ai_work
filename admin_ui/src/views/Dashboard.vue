<template>
  <div>
    <div class="page-header">
      <div>
        <h1 class="page-title">概览</h1>
        <p class="page-subtitle">面向运维场景的私有化 Claude Code · 接入 × Skill × 模型 × 用户</p>
      </div>
      <div class="welcome-chip">
        <span class="chip-dot"></span>
        欢迎，{{ auth.user?.display_name || auth.user?.username }}
      </div>
    </div>

    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-icon stat-1">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.72-1.71"/></svg>
        </div>
        <div class="stat-meta">
          <div class="stat-label">接入数量</div>
          <div class="stat-value">{{ stats.connections }}</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon stat-2">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-7-7M5 12a7 7 0 0 0 7 7"/></svg>
        </div>
        <div class="stat-meta">
          <div class="stat-label">已配置模型</div>
          <div class="stat-value">{{ stats.models }}</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon stat-3">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
        </div>
        <div class="stat-meta">
          <div class="stat-label">已注册 Skill</div>
          <div class="stat-value">{{ stats.skills }}</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon stat-4">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
        </div>
        <div class="stat-meta">
          <div class="stat-label">近 200 次调用</div>
          <div class="stat-value">{{ stats.calls }}</div>
        </div>
      </div>
    </div>

    <div class="content-grid">
      <div class="info-card">
        <div class="info-card-header">
          <span class="info-icon">📚</span>
          <span class="info-title">平台简介</span>
        </div>
        <div class="info-card-body">
          <p>
            这是一个面向运维场景的、可私有化部署的 AI 运维助手。
            模型层默认接入 <b>火山方舟</b>，同时兼容通义千问、智谱 GLM、DeepSeek、Moonshot 等任意
            OpenAI 协议兼容的国产模型。
          </p>
          <p>
            四个核心维度：<b>接入(Connections) × 技能(Skills) × 模型(Models) × 用户/会话</b>。
            所有 skill 的执行均经过统一审计；写操作会先生成待确认 token，
            由用户在 Chainlit / 管理后台 / MCP 客户端任一入口确认后才执行。
          </p>
        </div>
      </div>

      <div class="info-card">
        <div class="info-card-header">
          <span class="info-icon">🚀</span>
          <span class="info-title">快速开始</span>
        </div>
        <div class="info-card-body">
          <ol class="steps">
            <li>到 <RouterLink to="/connections">接入管理</RouterLink> 填入 Swarm / K8s / Zabbix 集群信息</li>
            <li>到 <RouterLink to="/models">模型管理</RouterLink> 填入火山或其他国产模型 API Key</li>
            <li>打开 Chainlit 聊天页（独立服务），登录同样的账号开始问答</li>
            <li>遇到写操作（重启、扩缩容、回滚），到 <RouterLink to="/pending">写操作待确认</RouterLink> 复核执行</li>
          </ol>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { RouterLink } from 'vue-router'
import { useAuthStore } from '../store/auth'
import api from '../api'

const auth = useAuthStore()
const stats = ref({ connections: 0, models: 0, skills: 0, calls: 0 })

// onMounted 里之前没 try/catch；任一接口 500 就让整页 stats 永远 0、
// 用户看不到任何"加载失败"提示。现在让每段独立失败，单项 0 而不是全 0，
// 整体异常由 api.js interceptor 自动弹 toast。
onMounted(async () => {
  try {
    const skills = (await api.get('/skills')).data
    stats.value.skills = skills.length
  } catch { /* interceptor 已弹错 */ }

  if (!auth.isAdmin) return

  const [c, m, calls] = await Promise.allSettled([
    api.get('/connections'),
    api.get('/models'),
    api.get('/skill-calls?limit=200'),
  ])
  if (c.status === 'fulfilled') stats.value.connections = c.value.data.length
  if (m.status === 'fulfilled') stats.value.models = m.value.data.length
  if (calls.status === 'fulfilled') stats.value.calls = calls.value.data.length
})
</script>

<style scoped>
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 24px;
  gap: 16px;
}
.welcome-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  border-radius: 999px;
  background: var(--brand-50);
  color: var(--brand-700);
  font-size: 12.5px;
  font-weight: 500;
  border: 1px solid var(--brand-100);
}
.chip-dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--brand-500);
  box-shadow: 0 0 0 4px rgba(20, 184, 166, 0.15);
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
  margin-bottom: 28px;
}
.stat-card {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 20px 22px;
  background: var(--bg-card);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-soft);
  transition: all .2s;
}
.stat-card:hover {
  border-color: var(--border);
  box-shadow: var(--shadow-md);
  transform: translateY(-1px);
}
.stat-icon {
  width: 44px; height: 44px;
  display: flex; align-items: center; justify-content: center;
  border-radius: var(--radius-md);
  color: #fff;
}
.stat-icon svg { width: 22px; height: 22px; }
.stat-1 { background: linear-gradient(135deg, #818cf8, #6366f1); }
.stat-2 { background: linear-gradient(135deg, #fbbf24, #f59e0b); }
.stat-3 { background: linear-gradient(135deg, #34d399, #10b981); }
.stat-4 { background: linear-gradient(135deg, var(--brand-400), var(--brand-600)); }

.stat-label {
  font-size: 12px;
  color: var(--text-muted);
  letter-spacing: 0.3px;
}
.stat-value {
  font-size: 28px;
  font-weight: 600;
  color: var(--text-primary);
  margin-top: 2px;
  letter-spacing: -0.5px;
}

.content-grid {
  display: grid;
  grid-template-columns: 1.4fr 1fr;
  gap: 16px;
}
@media (max-width: 1100px) {
  .content-grid { grid-template-columns: 1fr; }
}

.info-card {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-soft);
  overflow: hidden;
}
.info-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border-soft);
  font-weight: 600;
  font-size: 14px;
  color: var(--text-primary);
}
.info-icon { font-size: 16px; }
.info-card-body {
  padding: 18px 20px;
}
.info-card-body p {
  line-height: 1.7;
  color: var(--text-secondary);
  margin: 0 0 12px;
  font-size: 13.5px;
}
.info-card-body p:last-child { margin-bottom: 0; }

.steps {
  margin: 0;
  padding-left: 22px;
  line-height: 2.1;
  color: var(--text-secondary);
  font-size: 13.5px;
}
.steps a {
  color: var(--brand-700);
  text-decoration: none;
  font-weight: 500;
  border-bottom: 1px dashed var(--brand-300);
  padding-bottom: 1px;
}
.steps a:hover {
  color: var(--brand-600);
  border-bottom-style: solid;
}
</style>
