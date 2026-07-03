import { defineStore } from 'pinia'

const THEME_KEY = 'finsync_theme'

function getInitialTheme() {
  const saved = localStorage.getItem(THEME_KEY)
  if (saved === 'light' || saved === 'dark') return saved
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export const useThemeStore = defineStore('theme', {
  state: () => ({
    mode: getInitialTheme(),
  }),
  actions: {
    apply() {
      document.documentElement.classList.toggle('dark', this.mode === 'dark')
    },
    toggle() {
      this.mode = this.mode === 'dark' ? 'light' : 'dark'
      localStorage.setItem(THEME_KEY, this.mode)
      this.apply()
    },
    init() {
      this.apply()
    },
  },
})
