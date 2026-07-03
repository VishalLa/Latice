import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000',
  timeout: 30000,
})

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('finsync_access_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const status = error?.response?.status
    const message =
      error?.response?.data?.error ||
      error?.response?.data?.message ||
      error?.message ||
      'Something went wrong. Please try again.'

    const { useToastStore } = await import('@/stores/toast')
    const toast = useToastStore()

    if (status === 401) {
      const { useAuthStore } = await import('@/stores/auth')
      const auth = useAuthStore()
      const wasAuthenticated = auth.isAuthenticated
      auth.logout()
      if (wasAuthenticated) {
        toast.error('Your session expired. Please sign in again.')
      }
      const router = (await import('@/router')).default
      if (router.currentRoute.value.name !== 'login') {
        router.push({ name: 'login' })
      }
    } else if (status === 400) {
      toast.error(message)
    } else if (status === 404) {
      toast.error(message)
    } else if (status >= 500) {
      toast.error('The server hit a problem. Please try again shortly.')
    } else if (!error.response) {
      toast.error('Cannot reach the server. Check your connection.')
    } else {
      toast.error(message)
    }

    return Promise.reject(error)
  }
)

export default api
