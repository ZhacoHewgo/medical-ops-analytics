<script setup>
import DashboardChart from '../DashboardChart.vue'

defineProps({
  data: { type: Array, required: true },
  loading: Boolean,
  theme: { type: Object, default: () => ({}) },
  drillable: Boolean,
  label: String,
  unit: String,
  category: String,
  compact: Boolean,
})
defineEmits(['select-row'])
</script>

<template>
  <div :aria-busy="loading">
    <DashboardChart kind="ranking" :rows="data" :theme="theme" :drillable="drillable" :label="label" :unit="unit"
      :category="category" :compact-ranking="compact" @select-row="$emit('select-row', $event)" />
  </div>
</template>
