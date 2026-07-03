<script setup>
import { useAuthStore } from '@/stores/auth'
import { useRouter } from 'vue-router'
import ThemeToggle from './ThemeToggle.vue'
import BrandMark from './BrandMark.vue'

const auth = useAuthStore()
const router = useRouter()

function handleLogout() {
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <header class="sticky top-0 z-20 border-b border-[var(--border)] bg-[var(--surface)]/80 backdrop-blur">
    <div class="mx-auto flex max-w-6xl items-center justify-between px-6 py-3">
      <div class="flex items-center gap-2">
        <BrandMark :size="26" />
        <div class="leading-tight">
          <p class="text-sm font-extrabold tracking-tight text-[var(--text-primary)]">FinSync</p>
          <p class="-mt-0.5 text-[10px] font-medium text-[var(--text-muted)]">by Lattice</p>
        </div>
      </div>

      <div class="flex items-center gap-4">
        <span v-if="auth.user" class="hidden text-sm text-[var(--text-muted)] sm:inline">
          {{ auth.user.username }}
        </span>
        <ThemeToggle />
        <button
          type="button"
          class="rounded-pill border border-[var(--border)] px-4 py-1.5 text-sm font-medium text-[var(--text-primary)] transition hover:bg-black/5 dark:hover:bg-white/5"
          @click="handleLogout"
        >
          Sign out
        </button>
      </div>
    </div>
  </header>
</template>
