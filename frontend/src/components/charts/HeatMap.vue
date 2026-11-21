<script setup>
import CompositionChart from './CompositionChart.vue'

defineProps({
  data: { type: Array, required: true },
  loading: Boolean,
  theme: { type: Object, default: () => ({}) },
})
</script>

<template>
  <CompositionChart kind="heatmap" :data="data" :loading="loading" :theme="theme" />
</template>
