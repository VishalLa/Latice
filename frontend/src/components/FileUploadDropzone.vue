<script setup>
import { ref, computed } from 'vue'

const props = defineProps({
  label: { type: String, required: true },
  hint: { type: String, default: 'CSV or XLSX, up to 20MB' },
  modelValue: { type: [File, null], default: null },
  error: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue'])

const ACCEPTED = ['csv', 'xlsx']
const isDragging = ref(false)
const inputRef = ref(null)
const localError = ref('')

const displayError = computed(() => props.error || localError.value)

function extensionOf(name) {
  return name.split('.').pop()?.toLowerCase()
}

function validateAndSet(file) {
  if (!file) return
  const ext = extensionOf(file.name)
  if (!ACCEPTED.includes(ext)) {
    localError.value = `".${ext}" isn't supported. Upload a .csv or .xlsx file.`
    return
  }
  localError.value = ''
  emit('update:modelValue', file)
}

function onDrop(e) {
  isDragging.value = false
  const file = e.dataTransfer?.files?.[0]
  validateAndSet(file)
}

function onPick(e) {
  const file = e.target.files?.[0]
  validateAndSet(file)
  e.target.value = ''
}

function clear() {
  localError.value = ''
  emit('update:modelValue', null)
}

function openPicker() {
  inputRef.value?.click()
}
</script>

<template>
  <div>
    <div
      class="group relative flex flex-col items-center justify-center rounded-card border-2 border-dashed px-4 py-8 text-center transition-colors cursor-pointer"
      :class="[
        isDragging ? 'border-brand-glow bg-brand-glow/5' : 'border-[var(--border)] hover:border-brand-blue',
        displayError ? '!border-rose-400' : '',
      ]"
      role="button"
      tabindex="0"
      :aria-label="`Upload ${label}`"
      @click="openPicker"
      @keydown.enter="openPicker"
      @keydown.space.prevent="openPicker"
      @dragover.prevent="isDragging = true"
      @dragleave.prevent="isDragging = false"
      @drop.prevent="onDrop"
    >
      <input
        ref="inputRef"
        type="file"
        class="hidden"
        accept=".csv,.xlsx"
        @change="onPick"
      />

      <template v-if="!modelValue">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="mb-2 h-7 w-7 text-[var(--text-muted)]">
          <path d="M12 16V4M12 4l-4 4M12 4l4 4" stroke-linecap="round" stroke-linejoin="round" />
          <path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <p class="text-sm font-semibold text-[var(--text-primary)]">{{ label }}</p>
        <p class="mt-1 text-xs text-[var(--text-muted)]">{{ hint }}</p>
      </template>

      <template v-else>
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="mb-2 h-7 w-7 text-brand-cyan">
          <path d="M9 12l2 2 4-4" stroke-linecap="round" stroke-linejoin="round" />
          <circle cx="12" cy="12" r="9" />
        </svg>
        <p class="max-w-full truncate text-sm font-semibold text-[var(--text-primary)]">{{ modelValue.name }}</p>
        <p class="mt-1 text-xs text-[var(--text-muted)]">{{ (modelValue.size / 1024).toFixed(1) }} KB</p>
        <button
          type="button"
          class="mt-3 text-xs font-medium text-rose-500 underline-offset-2 hover:underline"
          @click.stop="clear"
        >
          Remove file
        </button>
      </template>
    </div>
    <p v-if="displayError" class="mt-1.5 text-xs font-medium text-rose-500">{{ displayError }}</p>
  </div>
</template>
