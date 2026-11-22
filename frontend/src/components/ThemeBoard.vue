<script setup>
import { provideChartMotion } from '../styles/chartMotion.mjs'
import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { buildQuery } from '../api/dashboard.mjs'
import { useSession } from '../store/user.js'
import TrendChart from './charts/TrendChart.vue'
import MotionNumber from './charts/MotionNumber.vue'
import DashboardLoading from './DashboardLoading.vue'
import DateRangeField from './DateRangeField.vue'
import WorkspaceHeading from './WorkspaceHeading.vue'

const props = defineProps({
  title: { type: String, required: true },
  source: { type: String, required: true },
  labels: { type: Object, required: true },
  loadData: { type: Function, required: true },
  hideTrend: Boolean,
  loadingLayout: { type: String, required: true },
  loadingCards: { type: Number, required: true },
})

const router = useRouter()
const chartMotionScope = provideChartMotion(() => Boolean(data.value))
const session = useSession()
const department = session.current?.user.department ?? null
const filters = reactive({ start_date: '', end_date: '', dept_code: department || '' })
const data = shallowRef(null)
const trail = ref([])
const busy = ref(false)
const error = ref('')
const chartRows = computed(() => data.value?.group_by === 'day'
  ? data.value.rows.map(row => ({ date: row.group, visits: row.value === null ? null : Number(row.value) }))
  : [])
const chartAvailable = computed(() => chartRows.value.some(row => row.visits !== null))
const visibleMetrics = computed(() => Object.entries(data.value?.metrics ?? {}).filter(([, item]) => item.value !== null))
const metricFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })
const moneyFormat = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
let pending
let lastRequest

async function load(selection = filters, drill = null) {
  pending?.abort()
  const controller = pending = new AbortController()
  const selected = { ...selection, dept_code: department || selection.dept_code }
  lastRequest = { selected, drill }
  busy.value = true
  error.value = ''
  data.value = null
  try {
    const query = buildQuery(selected)
    const params = drill ? `${query || '?'}${query ? '&' : ''}drill=${drill}` : query
    const response = await props.loadData(session, params, {
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]),
    }, () => { void router.replace({ name: 'login', query: { expired: '1' } }) })
    if (controller.signal.aborted) return
    if (!response.ok) {
      error.value = {
        401: '请重新登录。',
        403: '当前账号无权查看所选科室。',
        422: '请检查日期及科室范围。',
        503: '汇总暂不可用。',
      }[response.status] || '暂时无法读取汇总，请重试。'
      return
    }
    const payload = await response.json()
    if (controller.signal.aborted) return
    const result = payload?.data
    if (payload?.code !== 0 || result?.meta?.source !== props.source
        || result.filters?.dept_code !== (selected.dept_code || null)
        || (selected.start_date && result.filters.start_date !== selected.start_date)
        || (selected.end_date && result.filters.end_date !== selected.end_date)
        || result.group_by !== (drill || 'day') || !Array.isArray(result.rows)
        || !Object.hasOwn(props.labels, result.metric) || !result.metrics
        || Object.keys(props.labels).some(key => !Object.hasOwn(result.metrics, key))
        || Object.keys(result.metrics).some(key => !Object.hasOwn(props.labels, key))) {
      throw new Error('汇总响应与请求范围不一致。')
    }
    chartMotionScope.commit()
    data.value = result
    Object.assign(filters, result.filters, { dept_code: result.filters.dept_code || '' })
  } catch (cause) {
    if (!controller.signal.aborted) {
      error.value = cause?.name === 'Error' && cause.message === '汇总响应与请求范围不一致。'
        ? cause.message : '暂时无法读取汇总，请检查日期或重试。'
    }
  } finally {
    if (pending === controller) busy.value = false
  }
}

function apply() {
  trail.value = []
  return load()
}

function retry() {
  if (lastRequest) return load(lastRequest.selected, lastRequest.drill)
}

function canDrill() {
  const result = data.value
  if (result.group_by === 'department') return !result.filters.dept_code
  return !result.filters.dept_code || result.filters.start_date !== result.filters.end_date
}

function handleDrill(group, dimension = data.value?.group_by) {
  if (!data.value || !canDrill()) return
  const current = data.value
  const scope = { ...current.filters }
  if (dimension === 'department' && scope.dept_code) return
  trail.value.push({ scope, drill: current.group_by === 'department' ? 'department' : null })
  if (dimension === 'department') return load({ ...scope, dept_code: group })
  return load({ ...scope, start_date: group, end_date: group }, scope.dept_code ? null : 'department')
}

function back() {
  const item = trail.value.pop()
  if (item) return load(item.scope, item.drill)
}

function display(item, unit) {
  if (item.value === null) return '—'
  const value = (unit === '元' ? moneyFormat : metricFormat).format(item.value)
  return unit ? `${value} ${unit}` : value
}

onMounted(apply)
onBeforeUnmount(() => pending?.abort())
</script>

<template>
  <main class="workspace section-page" :class="{ 'theme-with-dashboard': $slots.dashboard }">
    <WorkspaceHeading><h1>{{ title }}</h1></WorkspaceHeading>
    <section class="panel filter-panel" aria-label="筛选范围">
      <form class="filter-form range-filter" @submit.prevent="apply">
        <DateRangeField v-model:start="filters.start_date" v-model:end="filters.end_date" :disabled="busy" />
        <div class="field"><label for="theme-dept">科室代码</label><input id="theme-dept" v-model.trim="filters.dept_code" placeholder="全院" maxlength="64" :readonly="Boolean(department)"></div>
        <div class="filter-actions">
          <button type="submit" class="button primary" :disabled="busy">查询</button>
          <button v-if="trail.length" type="button" class="button secondary" :disabled="busy" @click="back">返回上层</button>
        </div>
      </form>
    </section>
    <DashboardLoading :active="busy" :layout="loadingLayout" :cards="loadingCards"
      :single-day="lastRequest?.drill === 'department'" :scoped="Boolean(lastRequest?.selected.dept_code)">
      <div v-if="error" class="status-announcer query-error" role="alert">
        <span>{{ error }}</span><button type="button" class="button secondary" @click="retry">重试</button>
      </div>
      <slot v-if="data && $slots.dashboard" name="dashboard" :summary="data" :drillable="canDrill()" :select="handleDrill" />
      <template v-else-if="data">
      <section class="business-section" aria-label="汇总结果">
        <h2 class="selection-scope">{{ data.filters.dept_code || '全院' }} · {{ data.filters.start_date }} 至 {{ data.filters.end_date }}</h2>
        <dl v-if="visibleMetrics.length" class="business-metrics metric-tiles">
          <div v-for="[key, item] in visibleMetrics" :key="key">
            <dt>{{ labels[key][0] }}</dt><dd><MotionNumber :value="item.value" :format="value => display({ value }, labels[key][1])" /></dd>
          </div>
          <slot name="summary-extra" :detail="data.details" />
        </dl>
      </section>
      <slot :detail="data.details" :metrics="data.metrics" />
      <section v-if="!hideTrend" class="panel chart-panel business-section">
        <h2>{{ data.group_by === 'day' ? labels[data.metric][0] + '日趋势' : '当日科室明细' }}</h2>
        <TrendChart v-if="chartAvailable" :data="chartRows"
                    :label="labels[data.metric][0]" :unit="labels[data.metric][1]" :drillable="canDrill()"
                    @select-row="row => handleDrill(row.date)" />
        <div v-else-if="data.rows.length" class="table-scroll" tabindex="0" aria-label="汇总明细">
          <table>
            <caption>{{ data.group_by === 'day' ? '按日明细' : '按科室明细' }}</caption>
            <thead><tr><th scope="col">{{ data.group_by === 'day' ? '日期' : '科室代码' }}</th><th scope="col">{{ labels[data.metric][0] }}</th></tr></thead>
            <tbody>
              <tr v-for="row in data.rows" :key="row.group">
                <th scope="row">
                  <button v-if="canDrill()" type="button" class="button secondary" @click="handleDrill(row.group)">{{ row.group }}</button>
                  <span v-else>{{ row.group }}</span>
                </th>
                <td>{{ display(row, labels[data.metric][1]) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else>当前范围没有汇总记录。</p>
      </section>
      </template>
    </DashboardLoading>
  </main>
</template>

<style scoped>
.theme-with-dashboard :deep(.workspace-heading) { margin-bottom: 12px; }
.theme-with-dashboard .filter-panel { margin-bottom: 14px; padding: 0; border: 0; background: transparent; box-shadow: none; }
.theme-with-dashboard .range-filter { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.theme-with-dashboard .range-filter .field { display: flex; gap: 8px; align-items: center; min-width: 0; }
.theme-with-dashboard .range-filter .field input { width: 95px; min-width: 0; }
.theme-with-dashboard .range-filter :deep(summary) { white-space: nowrap; }
.theme-with-dashboard .range-filter .filter-actions { margin: 0; }
</style>
