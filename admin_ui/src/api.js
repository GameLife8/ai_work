import axios from 'axios'

const api = axios.create({
  baseURL: '/admin/api/v1',
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
    if (err.response && err.response.status === 401) {
      localStorage.removeItem('admin_token')
      if (location.pathname !== '/login') location.href = '/login'
    }
    return Promise.reject(err)
  },
)

export default api
