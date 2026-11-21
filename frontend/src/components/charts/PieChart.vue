<script setup>
import CompositionChart from './CompositionChart.vue'

defineProps({
  data: { type: Array, required: true },
  loading: Boolean,
  theme: { type: Object, default: () => ({}) },
})
</script>

<template>
  <CompositionChart kind="pie" :data="data" :loading="loading" :theme="theme" />
</template>
