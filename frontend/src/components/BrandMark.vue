<script setup>
defineProps({
  size: { type: Number, default: 28 },
})
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <rect x="3" y="10" width="16" height="16" rx="4" transform="rotate(-18 3 10)" stroke="#444CE7" stroke-width="3" />
    <rect x="12" y="4" width="16" height="16" rx="4" transform="rotate(18 12 4)" stroke="#8098F9" stroke-width="3" />
    <rect x="14" y="16" width="16" height="16" rx="4" transform="rotate(-10 14 16)" stroke="#B07BF0" stroke-width="3" />
  </svg>
</template>
