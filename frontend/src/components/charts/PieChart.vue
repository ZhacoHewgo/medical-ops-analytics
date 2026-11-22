<script setup>
import CompositionChart from './CompositionChart.vue'

defineProps({
  data: { type: Array, required: true },
  loading: Boolean,
  drillable: Boolean,
  theme: { type: Object, default: () => ({}) },
})
defineEmits(['select-row'])
</script>

<template>
  <CompositionChart kind="pie" :data="data" :loading="loading" :theme="theme"
    :drillable="drillable" @select-row="$emit('select-row', $event)" />
</template>
