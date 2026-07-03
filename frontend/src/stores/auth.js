import { defineStore } from 'pinia'
import api from '@/api/axios'

const TOKEN_KEY = 'finsync_access_token'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    accessToken: localStorage.getItem(TOKEN_KEY) || null,
    user: null, // { id, username, email, created_at }
    loading: false,
  }),
  getters: {
    isAuthenticated: (state) => !!state.accessToken,
  },
  actions: {
    setToken(token) {
      this.accessToken = token
      if (token) {
        localStorage.setItem(TOKEN_KEY, token)
      } else {
        localStorage.removeItem(TOKEN_KEY)
      }
    },

    async register({ username, email, password }) {
      this.loading = true
      try {
        const { data } = await api.post('/auth/register', { username, email, password })
        return data
      } finally {
        this.loading = false
      }
    },

    async login({ username, password }) {
      this.loading = true
      try {
        const { data } = await api.post('/auth/login', { username, password })
        this.setToken(data.access_token)
        await this.fetchCurrentUser()
        return data
      } finally {
        this.loading = false
      }
    },

    async fetchCurrentUser() {
      if (!this.accessToken) return null
      const { data } = await api.get('/auth/me')
      this.user = data
      return data
    },

    logout() {
      this.setToken(null)
      this.user = null
    },
  },
})
