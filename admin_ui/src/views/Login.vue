<template>
  <div class="login-page">
    <div class="bg-blob blob-a"></div>
    <div class="bg-blob blob-b"></div>

    <div class="login-card">
      <div class="brand-row">
        <div class="brand-mark">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 2L2 7l10 5 10-5-10-5z" />
            <path d="M2 17l10 5 10-5" />
            <path d="M2 12l10 5 10-5" />
          </svg>
        </div>
        <div>
          <h1 class="brand-title">AI 运维平台</h1>
          <div class="brand-sub">面向运维 · 私有化部署 · 国产大模型</div>
        </div>
      </div>

      <div class="card-divider"></div>

      <h2 class="card-title">欢迎回来</h2>
      <p class="card-sub">登录账号继续使用</p>

      <el-form :model="form" @submit.prevent="onSubmit" size="large">
        <el-form-item>
          <el-input
            v-model="form.username"
            placeholder="账号"
            autocomplete="username"
          >
            <template #prefix>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="opacity: .5"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="密码"
            autocomplete="current-password"
            @keyup.enter="onSubmit"
          >
            <template #prefix>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="opacity: .5"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="onSubmit" class="login-btn">
            登 录
          </el-button>
        </el-form-item>
      </el-form>

      <div class="login-foot">
        默认管理员 <code>admin</code> / <code>admin123</code>
        <span class="muted">· 生产部署务必通过环境变量改密</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../store/auth'

const auth = useAuthStore()
const router = useRouter()
const loading = ref(false)
const form = ref({ username: '', password: '' })

async function onSubmit() {
  if (!form.value.username || !form.value.password) {
    ElMessage.warning('请输入账号和密码')
    return
  }
  loading.value = true
  try {
    await auth.login(form.value.username, form.value.password)
    router.push('/dashboard')
  } catch (e) {
    ElMessage.error(e.response?.data?.error || '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background:
    radial-gradient(circle at 20% 20%, #effbf8 0%, transparent 50%),
    radial-gradient(circle at 80% 80%, #ecfeff 0%, transparent 50%),
    #fafbfc;
  position: relative;
  overflow: hidden;
}
.bg-blob {
  position: absolute;
  border-radius: 50%;
  filter: blur(80px);
  pointer-events: none;
  opacity: .35;
}
.blob-a {
  width: 480px; height: 480px;
  top: -120px; left: -120px;
  background: var(--brand-300);
}
.blob-b {
  width: 420px; height: 420px;
  bottom: -100px; right: -100px;
  background: #93c5fd;
}

.login-card {
  width: 400px;
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-lg);
  padding: 36px 38px;
  box-shadow:
    0 30px 80px -20px rgba(15, 23, 42, .15),
    0 1px 0 0 rgba(255, 255, 255, .6) inset;
  position: relative;
  z-index: 1;
}

.brand-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand-mark {
  width: 40px; height: 40px;
  border-radius: var(--radius-md);
  background: linear-gradient(135deg, var(--brand-400), var(--brand-600));
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 6px 14px rgba(20, 184, 166, .25);
}
.brand-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: -0.2px;
}
.brand-sub {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 3px;
  letter-spacing: 0.3px;
}

.card-divider {
  height: 1px;
  background: var(--border-soft);
  margin: 24px 0 22px;
}

.card-title {
  font-size: 22px;
  font-weight: 600;
  margin: 0 0 6px;
  color: var(--text-primary);
  letter-spacing: -0.3px;
}
.card-sub {
  font-size: 13px;
  color: var(--text-secondary);
  margin: 0 0 22px;
}

.login-btn {
  width: 100%;
  font-size: 14px;
  font-weight: 500;
  height: 42px;
  letter-spacing: 4px;
}

.login-foot {
  margin-top: 18px;
  text-align: center;
  font-size: 12px;
  color: var(--text-secondary);
}
.login-foot code {
  font-family: var(--font-mono);
  background: var(--bg-soft);
  padding: 1px 6px;
  border-radius: 3px;
  color: var(--brand-700);
}
.muted { color: var(--text-muted); }
</style>
