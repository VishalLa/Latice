<script setup>
import { reactive, ref } from 'vue'
import { useRouter, RouterLink } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useToastStore } from '@/stores/toast'
import ThemeToggle from '@/components/ThemeToggle.vue'
import BrandMark from '@/components/BrandMark.vue'

const auth = useAuthStore()
const toast = useToastStore()
const router = useRouter()

const form = reactive({ username: '', email: '', password: '', confirm: '' })
const errors = reactive({ username: '', email: '', password: '', confirm: '' })
const submitting = ref(false)

function validate() {
  errors.username = form.username.trim() ? '' : 'Username is required.'
  errors.email = form.email && !/^\S+@\S+\.\S+$/.test(form.email) ? 'Enter a valid email.' : ''
  errors.password = form.password.length >= 6 ? '' : 'Use at least 6 characters.'
  errors.confirm = form.confirm === form.password ? '' : 'Passwords do not match.'
  return !errors.username && !errors.email && !errors.password && !errors.confirm
}

async function onSubmit() {
  if (!validate()) return
  submitting.value = true
  try {
    await auth.register({
      username: form.username.trim(),
      email: form.email.trim() || undefined,
      password: form.password,
    })
    toast.success('Account created. Sign in to continue.')
    router.push({ name: 'login' })
  } catch {
    // toast already shown by the axios interceptor
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="relative flex min-h-screen items-center justify-center overflow-hidden bg-hero-dark px-4 py-12">
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
        <h2 class="text-lg font-bold text-white">Create your account</h2>
        <p class="mt-1 text-sm text-white/50">Start reconciling ledgers against bank statements.</p>

        <form class="mt-6 space-y-4" novalidate @submit.prevent="onSubmit">
          <div>
            <label for="reg-username" class="mb-1.5 block text-xs font-medium text-white/70">Username</label>
            <input
              id="reg-username"
              v-model="form.username"
              type="text"
              autocomplete="username"
              class="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2.5 text-sm text-white placeholder-white/30 outline-none transition focus:border-brand-glow"
              placeholder="jane_doe"
            />
            <p v-if="errors.username" class="mt-1 text-xs text-rose-400">{{ errors.username }}</p>
          </div>

          <div>
            <label for="reg-email" class="mb-1.5 block text-xs font-medium text-white/70">Email <span class="text-white/30">(optional)</span></label>
            <input
              id="reg-email"
              v-model="form.email"
              type="email"
              autocomplete="email"
              class="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2.5 text-sm text-white placeholder-white/30 outline-none transition focus:border-brand-glow"
              placeholder="jane@company.com"
            />
            <p v-if="errors.email" class="mt-1 text-xs text-rose-400">{{ errors.email }}</p>
          </div>

          <div>
            <label for="reg-password" class="mb-1.5 block text-xs font-medium text-white/70">Password</label>
            <input
              id="reg-password"
              v-model="form.password"
              type="password"
              autocomplete="new-password"
              class="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2.5 text-sm text-white placeholder-white/30 outline-none transition focus:border-brand-glow"
              placeholder="••••••••"
            />
            <p v-if="errors.password" class="mt-1 text-xs text-rose-400">{{ errors.password }}</p>
          </div>

          <div>
            <label for="reg-confirm" class="mb-1.5 block text-xs font-medium text-white/70">Confirm password</label>
            <input
              id="reg-confirm"
              v-model="form.confirm"
              type="password"
              autocomplete="new-password"
              class="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2.5 text-sm text-white placeholder-white/30 outline-none transition focus:border-brand-glow"
              placeholder="••••••••"
            />
            <p v-if="errors.confirm" class="mt-1 text-xs text-rose-400">{{ errors.confirm }}</p>
          </div>

          <button
            type="submit"
            :disabled="submitting"
            class="mt-2 w-full rounded-pill bg-gradient-to-r from-brand-indigo to-brand-glow py-2.5 text-sm font-semibold text-white transition hover:opacity-90 disabled:opacity-50"
          >
            {{ submitting ? 'Creating account…' : 'Create account' }}
          </button>
        </form>
      </div>

      <p class="mt-5 text-center text-sm text-white/50">
        Already have an account?
        <RouterLink :to="{ name: 'login' }" class="font-semibold text-brand-cyan hover:underline">Sign in</RouterLink>
      </p>
    </div>
  </div>
</template>
