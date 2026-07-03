import { defineStore } from 'pinia'

let nextId = 1

export const useToastStore = defineStore('toast', {
  state: () => ({
    toasts: [], // { id, type: 'success' | 'error' | 'info', message }
  }),
  actions: {
    push(message, type = 'info', timeout = 4500) {
      const id = nextId++
      this.toasts.push({ id, type, message })
      if (timeout) {
        setTimeout(() => this.dismiss(id), timeout)
      }
      return id
    },
    success(message) {
      return this.push(message, 'success')
    },
    error(message) {
      return this.push(message, 'error', 6000)
    },
    info(message) {
      return this.push(message, 'info')
    },
    dismiss(id) {
      this.toasts = this.toasts.filter((t) => t.id !== id)
    },
  },
})
