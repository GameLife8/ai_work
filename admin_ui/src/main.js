import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import { ElNotification } from 'element-plus'
import 'element-plus/dist/index.css'
import './styles/theme.css'

import App from './App.vue'
import router from './router'

const app = createApp(App)

// 全局未捕获错误兜底：之前 Vue 组件渲染/事件回调里抛错会被 Vue
// 默默 console.error，用户看不到任何反馈。这里挂上 ElNotification，
// 让 dev 一目了然，prod 用户也至少知道"出了问题"，不会以为页面死了。
app.config.errorHandler = (err, _instance, info) => {
  // axios 错误已经在 interceptor 里弹过 toast 了，避免重复
  if (err && (err.isAxiosError || err.response || err.handled)) return
  console.error('[Vue errorHandler]', info, err)
  ElNotification.error({
    title: '页面异常',
    message: (err && err.message) || String(err) || '未知错误',
    duration: 6000,
  })
}

// Promise 未捕获 rejection（fetch 风格的代码常见）
window.addEventListener('unhandledrejection', (event) => {
  const err = event.reason
  if (err && (err.isAxiosError || err.response || err.handled)) return
  console.error('[unhandledrejection]', err)
  ElNotification.error({
    title: '异步异常',
    message: (err && err.message) || String(err) || '未知错误',
    duration: 6000,
  })
})

app.use(createPinia())
app.use(router)
app.use(ElementPlus)
app.mount('#app')
