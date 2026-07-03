<script setup>
import { reactive, ref } from 'vue'
import { useRouter, useRoute, RouterLink } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useToastStore } from '@/stores/toast'
import ThemeToggle from '@/components/ThemeToggle.vue'
import BrandMark from '@/components/BrandMark.vue'

const auth = useAuthStore()
const toast = useToastStore()
const router = useRouter()
const route = useRoute()

const form = reactive({ username: '', password: '' })
const errors = reactive({ username: '', password: '' })
const submitting = ref(false)

function validate() {
  errors.username = form.username.trim() ? '' : 'Username is required.'
  errors.password = form.password ? '' : 'Password is required.'
  return !errors.username && !errors.password
}

async function onSubmit() {
  if (!validate()) return
  submitting.value = true
  try {
    await auth.login({ username: form.username.trim(), password: form.password })
    toast.success(`Welcome back, ${form.username}.`)
    router.push(route.query.redirect || { name: 'dashboard' })
  } catch {
    // toast already shown by the axios interceptor
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="relative flex min-h-screen items-center justify-center overflow-hidden bg-hero-dark px-4 py-12">
    <!-- Ambient beam + glow, dark-mode signature element -->
    <div class="pointer-events-none absolute inset-0 bg-hero-glow"></div>
    <div class="pointer-events-none absolute left-1/2 top-0 h-full w-px -translate-x-1/2 bg-beam opacity-70"></div>

    <div class="absolute right-4 top-4 z-10">
      <ThemeToggle />
    </div>

    <div class="relative z-10 w-full max-w-sm">
      <div class="mb-8 flex flex-col items-center text-center">
        <BrandMark :size="40" />
        <h1 class="mt-4 text-xl font-extrabold tracking-tight text-white">FinSync</h1>
        <p class="text-xs font-medium text-white/50">by Lattice</p>
      </div>

      <div class="rounded-card border border-white/10 bg-ink-800/70 p-7 shadow-glow backdrop-blur">
        <h2 class="text-lg font-bold text-white">Sign in</h2>
        <p class="mt-1 text-sm text-white/50">Reconcile your ledger and bank statement in minutes.</p>

        <form class="mt-6 space-y-4" novalidate @submit.prevent="onSubmit">
          <div>
            <label for="username" class="mb-1.5 block text-xs font-medium text-white/70">Username</label>
            <input
              id="username"
              v-model="form.username"
              type="text"
              autocomplete="username"
              class="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2.5 text-sm text-white placeholder-white/30 outline-none transition focus:border-brand-glow"
              placeholder="jane_doe"
            />
            <p v-if="errors.username" class="mt-1 text-xs text-rose-400">{{ errors.username }}</p>
          </div>

          <div>
            <label for="password" class="mb-1.5 block text-xs font-medium text-white/70">Password</label>
            <input
              id="password"
              v-model="form.password"
              type="password"
              autocomplete="current-password"
              class="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2.5 text-sm text-white placeholder-white/30 outline-none transition focus:border-brand-glow"
              placeholder="••••••••"
            />
            <p v-if="errors.password" class="mt-1 text-xs text-rose-400">{{ errors.password }}</p>
          </div>

          <button
            type="submit"
            :disabled="submitting"
            class="mt-2 w-full rounded-pill bg-gradient-to-r from-brand-indigo to-brand-glow py-2.5 text-sm font-semibold text-white transition hover:opacity-90 disabled:opacity-50"
          >
            {{ submitting ? 'Signing in…' : 'Sign in' }}
          </button>
        </form>
      </div>

      <p class="mt-5 text-center text-sm text-white/50">
        New to FinSync?
        <RouterLink :to="{ name: 'register' }" class="font-semibold text-brand-cyan hover:underline">Create an account</RouterLink>
      </p>
    </div>
  </div>
</template>
