<script setup>
import { computed } from 'vue'

const props = defineProps({
  status: {
    type: String,
    required: true, // idle | uploading | PENDING | STARTED | RETRY | SUCCESS | FAILURE
  },
})

const config = computed(() => {
  const map = {
    idle: { label: 'Idle', dot: 'bg-gray-400', text: 'text-gray-500' },
    uploading: { label: 'Uploading', dot: 'bg-brand-blue animate-pulse', text: 'text-brand-blue' },
    PENDING: { label: 'Queued', dot: 'bg-amber-400 animate-pulse', text: 'text-amber-500' },
    STARTED: { label: 'Matching in progress', dot: 'bg-brand-glow animate-pulse', text: 'text-brand-glow' },
    RETRY: { label: 'Retrying', dot: 'bg-amber-400 animate-pulse', text: 'text-amber-500' },
    SUCCESS: { label: 'Complete', dot: 'bg-emerald-400', text: 'text-emerald-500' },
    FAILURE: { label: 'Failed', dot: 'bg-rose-500', text: 'text-rose-500' },
  }
  return map[props.status] || map.idle
})
</script>

<template>
  <span class="inline-flex items-center gap-2 rounded-pill border border-[var(--border)] px-3 py-1 text-xs font-medium" :class="config.text">
    <span class="h-1.5 w-1.5 rounded-full" :class="config.dot"></span>
    {{ config.label }}
  </span>
</template>
