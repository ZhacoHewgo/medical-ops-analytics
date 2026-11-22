<script setup>
import { computed } from 'vue'
import PieChart from './charts/PieChart.vue'
import RankBar from './charts/RankBar.vue'
import { chartTheme } from '../styles/theme.js'

const props = defineProps({
  title: { type: String, required: true },
  rows: { type: Array, required: true },
  field: { type: String, default: 'count' },
  unit: { type: String, default: '人次' },
  category: { type: String, default: '类别' },
  pie: Boolean,
  color: { type: String, default: chartTheme.ranking },
  palette: { type: Array, default: undefined },
})
const theme = computed(() => ({ ranking: props.color, palette: props.palette }))
const hasValue = row => row[props.field] !== null && Number.isFinite(Number(row[props.field]))
const incomplete = computed(() => props.rows.some(row => !hasValue(row)))
const chartRows = computed(() => {
  const visible = props.rows.filter(hasValue)
  const labels = new Map()
  for (const row of visible) labels.set(row.label, (labels.get(row.label) ?? 0) + 1)
  return visible.map(row => ({
    dept_code: labels.get(row.label) > 1 ? `${row.label}（${row.name}）` : row.label,
    visits: Number(row[props.field]),
  })).sort((a, b) => b.visits - a.visits || a.dept_code.localeCompare(b.dept_code)).slice(0, 10)
})
const usePie = computed(() => props.pie && !incomplete.value && props.rows.length <= 10
  && chartRows.value.some(row => row.visits > 0)
  && chartRows.value.every(row => row.visits >= 0))
</script>

<template>
  <section class="panel chart-panel detail-chart">
    <div class="section-heading">
      <h2>{{ title }}</h2>
    </div>
    <PieChart v-if="usePie" :data="chartRows" :theme="theme" />
    <RankBar v-else-if="chartRows.length" :data="chartRows" :unit="unit" :label="title" :category="category" :theme="theme" />
    <p v-else class="chart-empty">当前范围暂无数据</p>
    <slot />
  </section>
</template>
