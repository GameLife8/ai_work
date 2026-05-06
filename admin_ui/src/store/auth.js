import { defineStore } from 'pinia'
import api from '../api'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem('admin_token') || '',
    user: JSON.parse(localStorage.getItem('admin_user') || 'null'),
  }),
  getters: {
    isLogged: (s) => !!s.token,
    isAdmin: (s) => s.user && s.user.role === 'admin',
  },
  actions: {
    async login(username, password) {
      const { data } = await api.post('/auth/login', { username, password })
      this.token = data.token
      this.user = data.user
      localStorage.setItem('admin_token', data.token)
      localStorage.setItem('admin_user', JSON.stringify(data.user))
    },
    async fetchMe() {
      const { data } = await api.get('/auth/me')
      this.user = data
      localStorage.setItem('admin_user', JSON.stringify(data))
    },
    logout() {
      this.token = ''
      this.user = null
      localStorage.removeItem('admin_token')
      localStorage.removeItem('admin_user')
    },
  },
})
