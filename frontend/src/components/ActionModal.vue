<script setup>
import { onMounted, onUnmounted } from 'vue'

const props = defineProps({
  modelValue: { type: Boolean, required: true },
  title: { type: String, default: '' },
  confirmLabel: { type: String, default: 'Confirm' },
  cancelLabel: { type: String, default: 'Cancel' },
  confirmDisabled: { type: Boolean, default: false },
  tone: { type: String, default: 'default' }, // default | danger
})

const emit = defineEmits(['update:modelValue', 'confirm', 'cancel'])

function close() {
  emit('update:modelValue', false)
  emit('cancel')
}

function confirm() {
  emit('confirm')
}

function onKeydown(e) {
  if (e.key === 'Escape' && props.modelValue) close()
}

onMounted(() => document.addEventListener('keydown', onKeydown))
onUnmounted(() => document.removeEventListener('keydown', onKeydown))
</script>

<template>
  <teleport to="body">
    <transition name="modal">
      <div
        v-if="modelValue"
        class="fixed inset-0 z-[90] flex items-center justify-center bg-black/50 px-4"
        @click.self="close"
      >
        <div
          class="w-full max-w-md rounded-card bg-[var(--surface)] p-6 shadow-2xl"
          role="dialog"
          aria-modal="true"
        >
          <div class="flex items-start justify-between">
            <h2 class="text-base font-semibold text-[var(--text-primary)]">{{ title }}</h2>
            <button type="button" class="text-[var(--text-muted)] hover:text-[var(--text-primary)]" aria-label="Close dialog" @click="close">×</button>
          </div>

          <div class="mt-3 text-sm text-[var(--text-muted)]">
            <slot />
          </div>

          <div class="mt-6 flex justify-end gap-2">
            <button
              type="button"
              class="rounded-pill border border-[var(--border)] px-4 py-2 text-sm font-medium text-[var(--text-primary)] hover:bg-black/5 dark:hover:bg-white/5"
              @click="close"
            >
              {{ cancelLabel }}
            </button>
            <button
              type="button"
              class="rounded-pill px-4 py-2 text-sm font-semibold text-white transition disabled:opacity-50"
              :class="tone === 'danger' ? 'bg-rose-500 hover:bg-rose-600' : 'bg-brand-indigo hover:bg-brand-glow'"
              :disabled="confirmDisabled"
              @click="confirm"
            >
              {{ confirmLabel }}
            </button>
          </div>
        </div>
      </div>
    </transition>
  </teleport>
</template>

<style scoped>
.modal-enter-active,
.modal-leave-active {
  transition: opacity 0.2s ease;
}
.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}
</style>
