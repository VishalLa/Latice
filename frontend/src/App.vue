<script setup>
import { onMounted } from 'vue'
import { useThemeStore } from '@/stores/theme'
import ToastContainer from '@/components/ToastContainer.vue'

const theme = useThemeStore()
onMounted(() => theme.init())
</script>

<template>
  <div class="min-h-screen bg-[var(--page-bg)] text-[var(--text-primary)]">
    <router-view />
    <ToastContainer />
  </div>
</template>
