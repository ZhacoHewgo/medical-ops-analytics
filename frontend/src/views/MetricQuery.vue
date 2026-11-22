<script setup>
import { provideChartMotion } from '../styles/chartMotion.mjs'
import { computed, onMounted, reactive, ref, shallowRef } from 'vue'
import { formatMetric, useAnalysisRequest } from '../api/analysis.mjs'
import { buildQuery } from '../api/dashboard.mjs'
import { indicators, readMetric } from '../api/metricQuery.mjs'
import { outpatientDepartmentName } from '../api/departmentLabels.mjs'
import { useSession } from '../api/session.mjs'
import DashboardLoading from '../components/DashboardLoading.vue'
import DateRangeField from '../components/DateRangeField.vue'
import TrendChart from '../components/charts/TrendChart.vue'
import MotionNumber from '../components/charts/MotionNumber.vue'
import WorkspaceHeading from '../components/WorkspaceHeading.vue'

defineOptions({ name: 'MetricQueryView' })

const department = useSession().current?.user.department ?? null
const chartMotionScope = provideChartMotion(() => Boolean(result.value))
const selection = reactive({ start_date: '', end_date: '', dept_code: department || '' })
const metric = ref('outpatient')
const groups = {
  门诊: ['outpatient', 'departments'],
  住院: ['discharge', 'stay', 'cost', 'bed'],
  收费: ['charge', 'drug', 'supply'],
}
const result = shallowRef(null)
const recorded = computed(() => result.value?.trend.filter(row => row.visits !== null) ?? [])
const { busy, error, run } = useAnalysisRequest()

async function query() {
  if (busy.value) return
  const indicator = indicators[metric.value]
  const filters = { ...selection, dept_code: department || selection.dept_code }
  let queryString
  try {
    queryString = buildQuery(filters)
  } catch (cause) {
    result.value = null
    error.value = cause.message
    return
  }
  result.value = null
  const data = await run(`/api/v1/${indicator.endpoint}/summary${queryString}`, {
    read: payload => readMetric(payload, indicator, filters),
  })
  if (data) {
    chartMotionScope.commit()
    Object.assign(selection, data.filters, { dept_code: data.filters.dept_code || '' })
    result.value = { ...data, label: indicator.label, unit: indicator.unit }
  }
}

onMounted(query)
</script>

<template>
  <main class="workspace section-page metric-page">
    <WorkspaceHeading><h1>指标查询</h1></WorkspaceHeading>
    <section class="panel filter-panel metric-controls" aria-label="指标筛选">
      <form @submit.prevent="query">
        <div class="metric-choices" role="group" aria-label="指标">
          <fieldset v-for="(keys, label) in groups" :key="label" :disabled="busy">
            <legend>{{ label }}</legend>
            <div>
              <label v-for="key in keys" :key="key" class="metric-choice">
                <input v-model="metric" type="radio" name="metric-name" :value="key" class="report-visually-hidden">
                <span>{{ indicators[key].label }}</span>
              </label>
            </div>
          </fieldset>
        </div>
        <div class="metric-scope">
          <DateRangeField v-model:start="selection.start_date" v-model:end="selection.end_date" :disabled="busy" />
          <div class="field"><label for="metric-department">科室代码</label><input id="metric-department" v-model.trim="selection.dept_code"
            placeholder="全院" maxlength="64" :readonly="Boolean(department)" :disabled="busy"></div>
          <button type="submit" class="button primary" :disabled="busy">查询</button>
        </div>
      </form>
    </section>
    <DashboardLoading :active="busy" layout="metric" label="正在查询指标…">
      <p v-if="error" class="report-error business-section" role="alert">{{ error }}</p>
      <div v-if="result" class="metric-result-board">
        <section class="panel metric-summary" aria-labelledby="metric-result-title">
          <div>
            <h2 id="metric-result-title">{{ result.label }}</h2>
            <div v-if="result.value !== null" class="metric-answer"><MotionNumber :value="result.value" :format="value => formatMetric(value, result.unit)" /></div>
            <p v-else>暂无结果</p>
          </div>
          <dl class="metric-result-scope">
            <div><dt>统计期间</dt><dd>{{ result.filters.start_date }} 至 {{ result.filters.end_date }}</dd></div>
            <div><dt>统计科室</dt><dd>{{ result.filters.dept_code ? outpatientDepartmentName(result.filters.dept_code) : '全院' }}</dd></div>
          </dl>
        </section>
        <section v-if="recorded.length" class="panel chart-panel metric-trend" aria-labelledby="metric-daily-title">
          <div class="metric-chart-heading">
            <h2 id="metric-daily-title">{{ result.label }}{{ result.label.endsWith('日') ? '趋势' : '日趋势' }}</h2>
            <span>{{ recorded.length }} 个有记录日</span>
          </div>
          <TrendChart :data="result.trend" :label="result.label" :unit="result.unit" period="日" :highlight-date="recorded.at(-1).date" />
        </section>
      </div>
    </DashboardLoading>
  </main>
</template>

<style scoped>
.metric-controls { margin-bottom: 14px; }
.metric-choices { display: flex; flex-wrap: wrap; gap: 11px 24px; }
.metric-choices fieldset { min-width: 0; margin: 0; padding: 0; border: 0; }
.metric-choices legend { margin-bottom: 6px; color: var(--muted); font-size: .75rem; }
.metric-choices fieldset > div, .metric-scope { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.metric-choice { position: relative; cursor: pointer; }
.metric-choice span { display: block; padding: 6px 10px; border: 1px solid var(--border); border-radius: 9px; color: var(--ink); font-size: .82rem; }
.metric-choice :checked + span { border-color: var(--brand-blue); background: var(--surface-tint); color: var(--brand-blue); font-weight: 650; }
.metric-choice :focus-visible + span { outline: 2px solid var(--brand-blue); outline-offset: 2px; }
.metric-choice :disabled + span { cursor: wait; opacity: .62; }
.metric-scope { gap: 8px 10px; padding-top: 12px; margin-top: 12px; border-top: 1px solid var(--border); }
.metric-scope .field { display: flex; align-items: center; gap: 6px; }
.metric-scope .field input { width: 95px; }
.metric-scope :deep(summary) { white-space: nowrap; }
.metric-result-board { display: grid; gap: 14px; }
.metric-summary { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px 24px; padding: 16px 18px; }
.metric-summary h2, .metric-chart-heading h2 { margin: 0; color: var(--ink); font-size: 1rem; }
.metric-answer { margin-top: 3px; color: var(--brand-blue); font-size: clamp(1.45rem, 2.7vw, 2.3rem); font-variant-numeric: tabular-nums; }
.metric-result-scope { display: flex; flex-wrap: wrap; gap: 8px 23px; margin: 0; }
.metric-result-scope dt, .metric-chart-heading span { color: var(--muted); font-size: .75rem; }
.metric-result-scope dd { margin: 2px 0 0; color: var(--ink); font-size: .87rem; }
.metric-chart-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
</style>
