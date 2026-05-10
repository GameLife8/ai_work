import axios from 'axios'
import { ElMessage } from 'element-plus'

/**
 * Axios 实例：所有 admin UI 走这里。
 *
 * - baseURL：默认 ``/admin/api/v1``（同域 nginx 反代）。要把 UI 跟后端拆分部署
 *   时设 ``VITE_API_BASE`` 环境变量（构建期注入）。
 * - 401：清 token 跳登录页（避开本来就在 /login 的死循环）。
 * - 403/5xx：统一弹 toast 提示，避免 view 里漏 catch 时静默失败。
 * - 客户端调用方仍可在 try/catch 里 swallow 掉 toast：检测 ``err.handled``。
 */
const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || '/admin/api/v1',
  timeout: 30000,
})

api.interceptors.request.use((cfg) => {
  const token = localStorage.getItem('admin_token')
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

api.interceptors.response.use(
  (resp) => resp,
  (err) => {
    const status = err.response?.status
    const url = err.config?.url || ''

    if (status === 401) {
      localStorage.removeItem('admin_token')
      if (location.pathname !== '/login') location.href = '/login'
      return Promise.reject(err)
    }

    // 服务端真异常或权限不足 —— 给一个统一兜底 toast，
    // 让 onMounted 里漏 catch 的代码不再"什么都没发生"。
    // 调用方想自定义错误展示就在 catch 里 set err.handled = true
    // 然后我们这里就不会重复弹。
    if (status >= 500 || status === 403) {
      const msg =
        err.response?.data?.error ||
        err.response?.data?.message ||
        err.message ||
        '请求失败'
      // 异步派发，避免在 interceptor 同步链里中断
      queueMicrotask(() => {
        if (!err.handled) ElMessage.error(`[${status || '?'}] ${url}：${msg}`)
      })
    }
    return Promise.reject(err)
  },
)

export default api
