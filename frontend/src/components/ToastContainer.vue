<script setup>
import { useToastStore } from '@/stores/toast'

const toast = useToastStore()

const styles = {
  success: 'border-l-4 border-l-brand-cyan',
  error: 'border-l-4 border-l-rose-500',
  info: 'border-l-4 border-l-brand-blue',
}

const icons = {
  success: '✓',
  error: '!',
  info: 'i',
}
</script>

<template>
  <div class="pointer-events-none fixed inset-x-0 top-4 z-[100] flex flex-col items-center gap-2 px-4 sm:items-end sm:right-4 sm:left-auto">
    <transition-group name="toast">
      <div
        v-for="t in toast.toasts"
        :key="t.id"
        class="pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-card bg-[var(--surface)] px-4 py-3 shadow-lg ring-1 ring-black/5"
        :class="styles[t.type]"
        role="status"
      >
        <span class="mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-black/5 text-xs font-bold text-[var(--text-primary)] dark:bg-white/10">
          {{ icons[t.type] }}
        </span>
        <p class="flex-1 text-sm leading-snug text-[var(--text-primary)]">{{ t.message }}</p>
        <button
          type="button"
          class="text-[var(--text-muted)] hover:text-[var(--text-primary)]"
          aria-label="Dismiss notification"
          @click="toast.dismiss(t.id)"
        >
          ×
        </button>
      </div>
    </transition-group>
  </div>
</template>

<style scoped>
.toast-enter-active,
.toast-leave-active {
  transition: all 0.25s ease;
}
.toast-enter-from {
  opacity: 0;
  transform: translateY(-8px);
}
.toast-leave-to {
  opacity: 0;
  transform: translateX(8px);
}
</style>
