<script setup>
import DashboardChart from '../DashboardChart.vue'

defineProps({
  data: { type: Array, required: true },
  loading: Boolean,
  theme: { type: Object, default: () => ({}) },
  drillable: Boolean,
  label: String,
  unit: String,
})
defineEmits(['select-row'])
</script>

<template>
  <div :aria-busy="loading">
    <DashboardChart kind="trend" :rows="data" :theme="theme" :label="label" :unit="unit" :drillable="drillable" @select-row="$emit('select-row', $event)" />
  </div>
</template>
