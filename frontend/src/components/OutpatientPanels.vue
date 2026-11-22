<script setup>
import { computed, ref } from 'vue'
import { departmentChartColor, outpatientDepartmentName } from '../api/departmentLabels.mjs'
import OutpatientDepartmentMix from './OutpatientDepartmentMix.vue'
import MotionNumber from './charts/MotionNumber.vue'
import TrendChart from './charts/TrendChart.vue'
import RankBar from './charts/RankBar.vue'
import { useChartMotion } from '../styles/chartMotion.mjs'

const props = defineProps({
  data: { type: Object, required: true },
  metrics: { type: Array, required: true },
  weekdays: { type: Array, required: true },
  peak: { type: Object, default: null },
  previousTrend: Array,
})
defineEmits(['select-day', 'select-department'])

const number = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 })
const money = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const { phase: motionPhase, revision: motionRevision } = useChartMotion()
const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 })
const singleDay = computed(() => props.data.filters.start_date === props.data.filters.end_date)
const showRank = computed(() => !props.data.filters.dept_code && props.data.department_ranking.length > 0)
const showTrend = computed(() => !singleDay.value && props.data.daily_trend.length > 0)
const low = computed(() => props.data.daily_trend.reduce((lowest, row) => !lowest || row.visits < lowest.visits ? row : lowest, null))
const turningPoints = computed(() => {
  if (!props.peak) return []
  const points = [{ label: '观测峰值', ...props.peak }]
  if (low.value && low.value.visits !== props.peak.visits) points.push({ label: '观测低点', ...low.value })
  return points
})
const weekdayMax = computed(() => Math.max(0, ...props.weekdays.map(row => row.value)))
const ascending = ref(false)
const rankingRows = computed(() => props.data.department_ranking.map(row => ({
  ...row, dept_label: outpatientDepartmentName(row.dept_code), dept_color: departmentChartColor(row.dept_code),
})))
const sameVolume = computed(() => rankingRows.value.length > 1
  && rankingRows.value.every(row => row.visits === rankingRows.value[0].visits))
const departments = computed(() => ascending.value
  ? [...rankingRows.value].sort((a, b) => a.visits - b.visits)
  : rankingRows.value)
</script>

<template>
  <div class="outpatient-results" :class="{ 'chart-motion-update': motionPhase === 'update' }">
    <div class="outpatient-grid outpatient-main" :class="{ 'outpatient-grid-single': !showTrend && !(singleDay && showRank) }">
      <section class="panel analysis-card metrics-panel" aria-labelledby="outpatient-metrics-title">
        <h2 id="outpatient-metrics-title">运营指标</h2>
        <div class="visits-total">
          <h3>门诊总人次</h3>
          <p class="kpi-value"><MotionNumber :value="data.outpatient_visits" /><span>人次</span></p>
        </div>
        <div class="metric-list">
          <section v-for="metric in metrics" :key="metric.key" class="metric-line">
            <div>
              <h3>{{ metric.label }}</h3>
              <button v-if="metric.date && !singleDay" type="button" class="analysis-link" aria-label="查看峰值日" @click="$emit('select-day', metric.date)">
                {{ metric.date }} <span aria-hidden="true">↗</span>
              </button>
              <p v-else-if="metric.detail" class="metric-note">{{ metric.detail }}</p>
              <p v-else-if="metric.key === 'coverage'" class="metric-note">有门诊记录的科室</p>
            </div>
            <p class="metric-number"><MotionNumber :value="metric.value" :format="metric.key === 'cost' ? money.format : number.format" /><span v-if="metric.value !== null">{{ metric.unit }}</span></p>
          </section>
        </div>
      </section>

      <section v-if="showTrend" class="panel analysis-card trend-panel" aria-labelledby="trend-title">
        <div class="analysis-heading">
          <h2 id="trend-title">门诊人次趋势</h2>
          <span class="trend-legend">门诊人次</span>
        </div>
        <TrendChart :data="data.daily_trend" label="门诊人次" :highlight-date="peak?.date"
          :previous-rows="previousTrend" drillable
          @select-row="row => $emit('select-day', row.date)" />
      </section>
      <section v-else-if="singleDay && showRank" class="panel analysis-card" aria-labelledby="day-rank-title">
        <h2 id="day-rank-title">当日科室贡献</h2>
        <OutpatientDepartmentMix v-if="sameVolume" :rows="rankingRows" :total="data.outpatient_visits"
          @select-department="code => $emit('select-department', code)" />
        <RankBar v-else :data="rankingRows" label="门诊人次" compact drillable
          @select-row="row => $emit('select-department', row.dept_code)" />
      </section>
    </div>

    <div v-if="showTrend" class="outpatient-grid outpatient-secondary" :class="{ 'outpatient-grid-single': !showRank }">
      <section v-if="showRank" class="panel analysis-card rank-panel" aria-labelledby="ranking-title">
        <div class="analysis-heading">
          <h2 id="ranking-title">{{ sameVolume ? '科室门诊量构成' : '科室门诊量排名' }}</h2>
          <span class="analysis-caption">{{ departments.length }} 个科室</span>
        </div>
        <OutpatientDepartmentMix v-if="sameVolume" :rows="rankingRows" :total="data.outpatient_visits"
          @select-department="code => $emit('select-department', code)" />
        <RankBar v-else :data="rankingRows" label="门诊人次" compact drillable
          @select-row="row => $emit('select-department', row.dept_code)" />
      </section>
      <section class="panel analysis-card variation-panel" aria-labelledby="variation-title">
        <div class="analysis-heading">
          <h2 id="variation-title">门诊量波动</h2>
          <span class="analysis-caption">{{ data.daily_trend.length }} 个有记录日</span>
        </div>
        <ul class="turning-points">
          <li v-for="point in turningPoints" :key="point.label">
            <div>
              <span class="point-label">{{ point.label }}</span>
              <button type="button" class="analysis-link" @click="$emit('select-day', point.date)">{{ point.date }} <span aria-hidden="true">↗</span></button>
            </div>
            <strong>{{ number.format(point.visits) }} <span>人次</span></strong>
          </li>
        </ul>
        <div v-if="weekdayMax" class="weekday-section">
          <h3>各星期日均 <span>人次／日 · 有记录日</span></h3>
          <dl class="weekday-values">
            <div v-for="row in weekdays" :key="row.name">
              <dt>{{ row.label }}</dt>
              <dd>
                <span aria-hidden="true" class="weekday-track"><span :key="motionRevision" :style="{ width: `${row.value / weekdayMax * 100}%` }"></span></span>
                <span>{{ number.format(row.value) }}</span>
              </dd>
            </div>
          </dl>
        </div>
      </section>
    </div>

    <section v-if="data.registration_mix?.length" class="panel analysis-card" aria-labelledby="registration-mix">
      <h2 id="registration-mix">号别结构</h2>
      <dl class="weekday-values registration-mix">
        <div v-for="row in data.registration_mix" :key="row.label">
          <dt>{{ row.label }}</dt>
          <dd>
            <span class="weekday-track" aria-hidden="true"><span :style="{ width: percent.format(row.count / data.outpatient_visits) }"></span></span>
            <strong>{{ number.format(row.count) }} 人次 · {{ percent.format(row.count / data.outpatient_visits) }}</strong>
          </dd>
        </div>
      </dl>
    </section>

    <section v-if="departments.length" class="panel analysis-card detail-panel" aria-labelledby="outpatient-detail">
      <h2 id="outpatient-detail">科室门诊明细</h2>
      <div class="table-scroll" tabindex="0" aria-label="科室门诊明细，可滚动">
        <table>
          <thead>
            <tr>
              <th scope="col">科室</th>
              <th scope="col" :aria-sort="ascending ? 'ascending' : 'descending'">
                <button type="button" class="sort-button" @click="ascending = !ascending">
                  门诊人次 <span aria-hidden="true">{{ ascending ? '↑' : '↓' }}</span>
                </button>
              </th>
              <th scope="col">占比</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in departments" :key="row.dept_code">
              <th scope="row">
                <button v-if="showRank" type="button" class="analysis-link" :aria-label="'查看科室 ' + row.dept_label"
                  :title="row.dept_code" @click="$emit('select-department', row.dept_code)">{{ row.dept_label }}</button>
                <span v-else :title="row.dept_code">{{ row.dept_label }}</span>
              </th>
              <td>{{ number.format(row.visits) }}</td>
              <td>{{ percent.format(row.visits / data.outpatient_visits) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.outpatient-results { display: grid; gap: 14px; }
.analysis-card {
  min-width: 0;
  padding: 16px 18px;
  border-radius: 16px;
  color: var(--ink);
  background: white;
  box-shadow: 0 2px 7px color-mix(in srgb, var(--ink) 3%, transparent);
}
.analysis-card h2 { font-size: 1rem; font-weight: 720; }
.analysis-card h3 { margin: 0; font-size: .86rem; font-weight: 620; }
.analysis-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px 12px; margin-bottom: 10px; }
.analysis-caption, .trend-legend { color: var(--muted); font-size: .77rem; }
.trend-legend { display: inline-flex; align-items: center; gap: 7px; }
.trend-legend::before { content: ''; width: 16px; height: 3px; border-radius: 3px; background: var(--brand-blue); }
.visits-total {
  margin-top: 12px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: color-mix(in srgb, var(--comparison-blue) 14%, white);
}
.visits-total .kpi-value { margin: 2px 0 0; color: var(--brand-blue); font-size: clamp(2rem, 3vw, 2.65rem); line-height: 1.12; }
.metrics-panel { display: flex; flex-direction: column; }
.metric-list { flex: 1; display: grid; grid-auto-rows: minmax(42px, 1fr); margin-top: 6px; }
.weekday-values.registration-mix dd { grid-template-columns: minmax(12px, 1fr) max-content; }
.metric-line { display: flex; align-items: center; justify-content: space-between; gap: 8px 12px; min-height: 42px; padding: 5px 1px; }
.metric-line + .metric-line { border-top: 1px solid var(--border); }
.metric-note { color: var(--muted); font-size: .73rem; line-height: 1.5; }
.metric-number { font-size: 1.19rem; font-weight: 720; font-variant-numeric: tabular-nums; white-space: nowrap; }
.metric-number span, .turning-points strong span { margin-inline-start: 4px; color: var(--muted); font-size: .73rem; font-weight: 450; }
.analysis-link, .sort-button { min-height: 28px; padding: 0; border: 0; background: transparent; color: var(--brand-blue); font: inherit; text-align: left; }
.analysis-link:hover { text-decoration: underline; text-underline-offset: 3px; }
.metric-line .analysis-link, .turning-points .analysis-link { min-height: 24px; font-size: .76rem; }
.turning-points { display: grid; gap: 7px; margin: 0; padding: 0; list-style: none; }
.turning-points li { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 7px 10px; border: 1px solid var(--border); border-radius: 10px; background: var(--page-base); }
.turning-points li > div { display: flex; flex-wrap: wrap; align-items: center; gap: 2px 9px; }
.point-label { font-size: .79rem; font-weight: 620; }
.turning-points strong { font-variant-numeric: tabular-nums; white-space: nowrap; }
.weekday-section { margin-top: 14px; }
.weekday-section h3 { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 2px 8px; }
.weekday-section h3 span { color: var(--muted); font-size: .72rem; font-weight: 450; }
.weekday-values { display: grid; gap: 5px; margin-top: 8px; font-size: .74rem; font-variant-numeric: tabular-nums; }
.weekday-values > div { display: grid; grid-template-columns: 2.3em minmax(0, 1fr); align-items: center; column-gap: 8px; }
.weekday-values dd { display: grid; grid-template-columns: minmax(0, 1fr) 3.4em; align-items: center; column-gap: 8px; text-align: right; }
.weekday-track { height: 5px; border-radius: 5px; background: var(--page-base); }
.weekday-track span { display: block; height: 100%; border-radius: inherit; background: var(--support-cyan); transform-origin: left; animation: chart-bar-grow var(--chart-motion-bar) var(--chart-motion-ease) var(--chart-motion-gap) both; }
.detail-panel .table-scroll { margin-top: 10px; }
.detail-panel tr > :nth-child(n + 2) { text-align: right; }
.sort-button { min-height: 20px; color: var(--ink); }
.sort-button span { color: var(--brand-blue); }

@media (max-width: 500px) {
  .analysis-card { padding: 14px; }
}
</style>
