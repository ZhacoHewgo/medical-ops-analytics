<script setup>
import PieChart from './charts/PieChart.vue'

defineProps({
  rows: { type: Array, required: true },
  total: { type: Number, required: true },
  unitLabel: { type: String, default: '门诊人次' },
})
defineEmits(['select-department'])

const count = new Intl.NumberFormat('zh-CN')
const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 })
</script>

<template>
  <div class="department-mix">
    <div class="department-ring">
      <PieChart :data="rows" drillable @select-row="row => $emit('select-department', row.dept_code)" />
      <div class="ring-total" aria-hidden="true"><strong>{{ count.format(total) }}</strong><span>{{ unitLabel }}</span></div>
    </div>
    <ul class="department-legend" aria-label="各科室实际占比">
      <li v-for="row in rows" :key="row.dept_code">
        <button type="button" @click="$emit('select-department', row.dept_code)">
          <span class="legend-dot" :style="{ backgroundColor: row.dept_color }" aria-hidden="true"></span>
          <span>{{ row.dept_label }}</span>
          <strong>{{ percent.format(row.visits / total) }}</strong>
        </button>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.department-mix { display: grid; grid-template-columns: minmax(160px, .85fr) minmax(0, 1fr); align-items: center; gap: 12px; }
.department-ring { position: relative; min-width: 0; }
.ring-total { position: absolute; inset: 0; display: grid; place-content: center; text-align: center; pointer-events: none; }
.ring-total strong { font-size: clamp(1.24rem, 2vw, 1.7rem); font-variant-numeric: tabular-nums; }
.ring-total span { color: var(--muted); font-size: .75rem; }
.department-legend { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 128px), 1fr)); gap: 3px 8px; margin: 0; padding: 0; list-style: none; }
.department-legend button { display: grid; grid-template-columns: 8px minmax(0, 1fr) auto; gap: 6px; align-items: center; width: 100%; min-height: 32px; padding: 3px 5px; border: 0; border-radius: 6px; background: transparent; color: var(--ink); text-align: left; font-size: .76rem; }
.department-legend button:hover { background: var(--surface-tint); }
.legend-dot { width: 8px; height: 8px; border-radius: 50%; }
.department-legend strong { font-variant-numeric: tabular-nums; }

@media (max-width: 520px) {
  .department-mix { grid-template-columns: minmax(0, 1fr); }
  .department-ring { width: min(100%, 260px); justify-self: center; }
}
</style>
