<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 2L2 7l10 5 10-5-10-5z" />
            <path d="M2 17l10 5 10-5" />
            <path d="M2 12l10 5 10-5" />
          </svg>
        </div>
        <div class="brand-text">
          <div class="brand-name">AI 运维平台</div>
          <div class="brand-sub">private · zh-models</div>
        </div>
      </div>

      <nav class="menu">
        <RouterLink class="menu-item" to="/dashboard">
          <span class="menu-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
          </span>
          <span>概览</span>
        </RouterLink>

        <div class="menu-section">资源</div>
        <RouterLink class="menu-item" to="/connections">
          <span class="menu-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.72-1.71"/></svg>
          </span>
          <span>接入管理</span>
        </RouterLink>
        <RouterLink class="menu-item" to="/models">
          <span class="menu-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
          </span>
          <span>模型管理</span>
        </RouterLink>
        <RouterLink class="menu-item" to="/skills">
          <span class="menu-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
          </span>
          <span>Skill 注册表</span>
        </RouterLink>
        <RouterLink v-if="auth.isAdmin" class="menu-item" to="/http-skills">
          <span class="menu-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h18M3 6h18M3 18h18"/><circle cx="6" cy="6" r="1.4" fill="currentColor"/><circle cx="6" cy="12" r="1.4" fill="currentColor"/><circle cx="6" cy="18" r="1.4" fill="currentColor"/></svg>
          </span>
          <span>HTTP Skill</span>
        </RouterLink>
        <RouterLink v-if="auth.isAdmin" class="menu-item" to="/runbooks">
          <span class="menu-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="12" cy="18" r="3"/><line x1="6" y1="9" x2="12" y2="15"/><line x1="18" y1="9" x2="12" y2="15"/></svg>
          </span>
          <span>诊断剧本</span>
        </RouterLink>
        <RouterLink v-if="auth.isAdmin" class="menu-item" to="/prompts">
          <span class="menu-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
          </span>
          <span>提示词管理</span>
        </RouterLink>

        <div class="menu-section">运行</div>
        <RouterLink class="menu-item" to="/pending">
          <span class="menu-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
          </span>
          <span>写操作待确认</span>
        </RouterLink>
        <RouterLink v-if="auth.isAdmin" class="menu-item" to="/runbook-runs">
          <span class="menu-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
          </span>
          <span>剧本执行历史</span>
        </RouterLink>
        <RouterLink class="menu-item" to="/async-tasks">
          <span class="menu-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
          </span>
          <span>异步任务</span>
        </RouterLink>
        <RouterLink v-if="auth.isAdmin" class="menu-item" to="/audit">
          <span class="menu-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
          </span>
          <span>调用审计</span>
        </RouterLink>

        <template v-if="auth.isAdmin">
          <div class="menu-section">系统</div>
          <RouterLink class="menu-item" to="/users">
            <span class="menu-icon">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
            </span>
            <span>用户管理</span>
          </RouterLink>
        </template>
      </nav>

      <div class="user-card">
        <div class="user-avatar">{{ initials }}</div>
        <div class="user-info">
          <div class="user-name">{{ auth.user?.display_name || auth.user?.username }}</div>
          <div class="user-role">{{ auth.user?.role === 'admin' ? '管理员' : '普通用户' }}</div>
        </div>
        <button class="user-logout" @click="onLogout" title="退出登录">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
        </button>
      </div>
    </aside>

    <main class="main">
      <router-view v-slot="{ Component }">
        <transition name="fade" mode="out-in">
          <component :is="Component" />
        </transition>
      </router-view>
    </main>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter, RouterLink } from 'vue-router'
import { useAuthStore } from '../store/auth'

const auth = useAuthStore()
const router = useRouter()
const initials = computed(() => {
  const n = auth.user?.display_name || auth.user?.username || ''
  return n.slice(0, 1).toUpperCase()
})
function onLogout() { auth.logout(); router.push('/login') }
</script>

<style scoped>
.layout {
  display: flex;
  min-height: 100vh;
  background: var(--bg-page);
}

/* ---------- sidebar：白底简约 ---------- */
.sidebar {
  width: 240px;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border-soft);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 22px 20px 20px;
  border-bottom: 1px solid var(--border-soft);
}
.brand-mark {
  width: 36px; height: 36px;
  border-radius: 10px;
  background: linear-gradient(135deg, var(--brand-400), var(--brand-600));
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 4px 12px rgba(20, 184, 166, .25);
}
.brand-name {
  font-weight: 600;
  font-size: 15px;
  color: var(--text-primary);
  letter-spacing: -0.2px;
}
.brand-sub {
  font-size: 11px;
  color: var(--text-muted);
  letter-spacing: .5px;
  margin-top: 3px;
}

.menu {
  padding: 14px 12px;
  flex: 1;
  overflow-y: auto;
}
.menu-section {
  padding: 16px 8px 6px;
  font-size: 11px;
  letter-spacing: 1.2px;
  color: var(--text-muted);
  text-transform: uppercase;
  font-weight: 600;
}
.menu-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 9px 12px;
  margin: 2px 0;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-size: 13.5px;
  text-decoration: none;
  cursor: pointer;
  transition: all .15s;
}
.menu-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.menu-item.router-link-active {
  background: var(--brand-50);
  color: var(--brand-700);
  font-weight: 500;
}
.menu-item.router-link-active .menu-icon {
  color: var(--brand-600);
}
.menu-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  color: var(--text-muted);
  transition: color .15s;
}
.menu-item:hover .menu-icon {
  color: var(--text-secondary);
}

/* ---------- user card ---------- */
.user-card {
  margin: 12px;
  padding: 10px 12px;
  background: var(--bg-soft);
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--border-soft);
}
.user-avatar {
  width: 32px; height: 32px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--brand-400), var(--brand-600));
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-weight: 600;
  font-size: 13px;
}
.user-info { flex: 1; min-width: 0; }
.user-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.user-role {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 2px;
}
.user-logout {
  background: transparent;
  border: 0;
  color: var(--text-muted);
  cursor: pointer;
  padding: 6px;
  border-radius: var(--radius-xs);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all .15s;
}
.user-logout:hover {
  background: var(--bg-card);
  color: var(--danger);
}

/* ---------- main ---------- */
.main {
  flex: 1;
  padding: 28px 36px;
  overflow-y: auto;
  max-width: 100%;
}

/* ---------- 路由切换淡入淡出 ---------- */
.fade-enter-active, .fade-leave-active {
  transition: opacity .18s ease;
}
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
