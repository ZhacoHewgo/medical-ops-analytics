<script setup>
import DashboardChart from '../DashboardChart.vue'

defineProps({
  data: { type: Array, required: true },
  loading: Boolean,
  theme: { type: Object, default: () => ({}) },
  drillable: Boolean,
})
defineEmits(['select-row'])
</script>

<template>
  <div :aria-busy="loading">
    <DashboardChart kind="ranking" :rows="data" :theme="theme" :drillable="drillable" @select-row="$emit('select-row', $event)" />
  </div>
</template>
