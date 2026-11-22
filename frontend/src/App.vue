<script setup>
import { provideChartMotion } from './styles/chartMotion.mjs'
import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef } from 'vue'
import { useRoute } from 'vue-router'
import WorkspaceHeading from './components/WorkspaceHeading.vue'
import DashboardLoading from './components/DashboardLoading.vue'
import DateRangeField from './components/DateRangeField.vue'
import OutpatientPanels from './components/OutpatientPanels.vue'
import { createDashboardLoader, trendTransitionSeed } from './api/dashboard.mjs'
import { useSession } from './api/session.mjs'
import { outpatientDepartmentName } from './api/departmentLabels.mjs'

const props = defineProps({ loadResponse: { type: Function, required: true } })
const route = useRoute()
const chartMotionScope = provideChartMotion(() => Boolean(state.value.data))
const session = useSession()
const department = session.current?.user.department ?? null

const state = shallowRef({ status: 'idle', data: null, error: '', requestedFilters: {} })
const meta = shallowRef(null)
const previousTrend = shallowRef(undefined)
let lastOutpatientResult
const draft = reactive({ start_date: '', end_date: '', dept_code: department || '' })
const trail = ref([])
const loader = createDashboardLoader({
  source: 'ads',
  department,
  fetchImpl: props.loadResponse,
  onChange: value => {
    state.value = value
    if (value.status === 'success') {
      chartMotionScope.commit()
      meta.value = value.data.meta
      previousTrend.value = trendTransitionSeed(lastOutpatientResult, value.data)
      lastOutpatientResult = value.data
    }
  },
})
const data = computed(() => state.value.data)
const peak = computed(() => data.value?.daily_trend.reduce((highest, day) => !highest || day.visits > highest.visits ? day : highest, null))
const unapplied = computed(() => {
  const current = data.value?.filters
  return current && (current.start_date !== draft.start_date || current.end_date !== draft.end_date
    || (current.dept_code || '') !== draft.dept_code)
})
const weekdayRows = computed(() => {
  const days = Array.from({ length: 7 }, (_, index) => ({
    name: index, label: '周' + '一二三四五六日'[index], total: 0, count: 0,
  }))
  for (const row of data.value?.daily_trend ?? []) {
    const day = days[(new Date(row.date).getUTCDay() + 6) % 7]
    day.total += row.visits
    day.count += 1
  }
  return days.filter(day => day.count).map(day => ({ ...day, value: day.total / day.count }))
})
const summary = computed(() => {
  if (!data.value) return []
  const { daily_trend: days, department_ranking: departments, outpatient_visits: visits,
    outpatient_avg_cost: averageCost, registration_mix: registrations } = data.value
  const average = { key: 'average', label: '日均门诊（有记录日）',
    value: days.length ? Math.round(visits / days.length * 10) / 10 : null,
    unit: '人次/日', detail: days.length ? `按 ${days.length} 个有记录日计算` : '' }
  const coverage = { key: 'coverage', label: '覆盖科室', value: departments.length, unit: '个' }
  const maximum = { key: 'peak', label: '单日峰值', value: peak.value?.visits ?? null,
    unit: '人次', detail: peak.value?.date, date: peak.value?.date }
  const metrics = [average]
  if (averageCost != null) metrics.push({ key: 'cost', label: '门诊次均费用', value: averageCost, unit: '元/人次' })
  if (visits > 0 && registrations?.length) {
    const emergency = registrations.find(row => row.label === '急诊')?.count ?? 0
    metrics.push({ key: 'emergency', label: '急诊占比', value: emergency * 100 / visits, unit: '%' })
  }
  return [...metrics, maximum, coverage]
})
const busy = computed(() => state.value.status === 'loading')
const scopeLabel = filters => {
  const scope = filters.dept_code ? outpatientDepartmentName(filters.dept_code) : '全院科室'
  return `${filters.start_date || '默认最早日'} 至 ${filters.end_date || '默认最晚日'} · ${scope}`
}

async function loadDefault() {
  trail.value = []
  const result = await loader.load({})
  if (result) Object.assign(draft, { ...result.filters, dept_code: result.filters.dept_code || '' })
}

function setScope(filters) {
  Object.assign(draft, filters, { dept_code: filters.dept_code || '' })
  return loader.load(filters)
}

function applyFilters() {
  trail.value = []
  return loader.load({ ...draft })
}

function handleDrill(next) {
  const current = data.value.filters
  trail.value.push({ ...current })
  return setScope({ ...current, ...next })
}

function showDay(date) {
  return handleDrill({ start_date: date, end_date: date })
}

function back() {
  const previous = trail.value.pop()
  if (previous) return setScope(previous)
}

async function retry() {
  const result = await loader.retry()
  if (result) Object.assign(draft, { ...result.filters, dept_code: result.filters.dept_code || '' })
}

onMounted(loadDefault)
onBeforeUnmount(loader.dispose)
</script>

<template>
  <main id="main" class="workspace dashboard-workspace section-page outpatient-page" tabindex="-1">
    <p v-if="route.query.denied === 'admin'" class="report-error" role="alert">只有管理员可以打开用户管理。</p>
    <p v-if="route.query.denied === 'ops'" class="report-error" role="alert">当前账号无权查看数据源与任务。</p>
    <WorkspaceHeading aria-labelledby="page-title" class="outpatient-head">
      <h1 id="page-title">门诊分析</h1>
      <template #tools>
        <form class="outpatient-toolbar" aria-label="筛选范围" @submit.prevent="applyFilters">
          <DateRangeField v-model:start="draft.start_date" v-model:end="draft.end_date" :disabled="busy" />
          <select v-model="draft.dept_code" aria-label="科室代码" :disabled="!meta || department !== null">
            <option v-if="department === null" value="">全院科室</option>
            <option v-for="code in meta?.department_codes || (department ? [department] : [])" :key="code" :value="code">{{ outpatientDepartmentName(code) }}</option>
          </select>
          <button type="submit" class="button primary" :disabled="!meta">应用筛选</button>
          <button type="button" class="button secondary" @click="loadDefault">重置</button>
          <button v-if="trail.length" type="button" class="button secondary" :disabled="busy" @click="back">返回上一级</button>
        </form>
      </template>
    </WorkspaceHeading>

    <div v-if="data" class="status-announcer" :class="unapplied ? 'outpatient-applied' : 'report-visually-hidden'" role="status">
      {{ unapplied ? '当前结果 · ' : '' }}{{ scopeLabel(data.filters) }}
    </div>

    <DashboardLoading :active="busy || state.status === 'idle'" layout="outpatient"
      label="正在读取门诊数据…" :single-day="Boolean(draft.start_date) && draft.start_date === draft.end_date" :scoped="Boolean(draft.dept_code)">
      <section v-if="state.status === 'error'" class="panel state-panel error-panel" aria-labelledby="error-title" role="alert">
        <span class="state-symbol" aria-hidden="true">!</span><h2 id="error-title">门诊数据暂不可用</h2><p>{{ state.error }}</p><button type="button" class="button primary" @click="retry">重试本次请求</button>
      </section>

      <template v-else-if="data">
        <OutpatientPanels :data="data" :metrics="summary" :weekdays="weekdayRows" :peak="peak" :previous-trend="previousTrend"
          @select-day="showDay" @select-department="code => handleDrill({ dept_code: code })" />
        <section v-if="data.outpatient_visits === 0" class="panel state-panel empty-panel" aria-labelledby="empty-title">
          <span class="state-symbol" aria-hidden="true">—</span><h2 id="empty-title">当前范围暂无有效门诊记录</h2>
          <button class="button secondary" type="button" @click="loadDefault">重置筛选</button>
        </section>

      </template>
    </DashboardLoading>
  </main>
</template>

<style scoped>
.outpatient-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; min-width: 0; }
.outpatient-toolbar select { width: 116px; padding-inline: 8px; }
.outpatient-toolbar :is(.button, select) { min-height: 36px; height: 36px; }
.outpatient-toolbar .button { padding: 6px 10px; }
.outpatient-toolbar :deep(.overview-date-range summary) { min-height: 36px; border-color: var(--border); background: white; }
.outpatient-toolbar :deep(.overview-date-panel) { max-width: 100%; }
.outpatient-head + .outpatient-applied { padding: 0 0 10px; min-height: 0; }
.outpatient-page .empty-panel { margin-top: 14px; }

@media (max-width: 500px) {
  .outpatient-toolbar :deep(.overview-date-range) { flex: 1 0 100%; }
}
</style>
