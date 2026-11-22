<script setup>
import { provideChartMotion } from '../styles/chartMotion.mjs'
import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { formatMetric, isMetricValue, useAnalysisRequest } from '../api/analysis.mjs'
import { outpatientDepartmentName } from '../api/departmentLabels.mjs'
import MotionNumber from '../components/charts/MotionNumber.vue'
import WorkspaceHeading from '../components/WorkspaceHeading.vue'
import { buildQuery, validateDashboard } from '../api/dashboard.mjs'
import { defaultMultidimensional, multidimensionalEndpoint, multidimensionalPath, reportDateChoices } from '../api/report.mjs'
import { useSession } from '../api/session.mjs'
import DashboardLoading from '../components/DashboardLoading.vue'

defineOptions({ name: 'MultidimensionalReportView' })

const session = useSession()
const router = useRouter()
const chartMotionScope = provideChartMotion(() => Boolean(result.value))
const department = session.current?.user.department ?? null
const types = {
  billing_income: ['账单收入', '元'],
  charge_amount: ['收费明细金额', '元'],
  ledger_income: ['财务台账收入', '元'],
  outpatient_visits: ['门诊人次', '人次'],
  discharges: ['出院人次', '人次'],
}
const draft = reactive({
  start_date: '', end_date: '', dept_code: department || '',
  ...defaultMultidimensional, metrics: [...defaultMultidimensional.metrics],
})
const excel = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
const { run, busy, error, status } = useAnalysisRequest()
const result = shallowRef(null)
const dates = shallowRef(null)
const range = ref('all')
const exporting = ref(false)
const page = ref(1)
const pageSize = 20
const pageCount = computed(() => Math.ceil((result.value?.rows.length ?? 0) / pageSize))
const visibleRows = computed(() => result.value?.rows.slice((page.value - 1) * pageSize, page.value * pageSize))
const endDates = computed(() => dates.value?.days.filter(day => day >= draft.start_date) ?? [])
const exportLifetime = new AbortController()
onBeforeUnmount(() => exportLifetime.abort())

function readReport(data, requested) {
  const hasMetrics = values => values && Object.keys(values).length === requested.metrics.length
    && requested.metrics.every(key => Object.hasOwn(values, key) && (values[key] === null || isMetricValue(values[key])))
  if (data.meta?.source !== 'multidimensional_report' || !/^[a-f0-9]{64}$/.test(data.report_id)
      || data.filters?.start_date !== (requested.start_date || data.meta.available_start_date)
      || data.filters?.end_date !== (requested.end_date || data.meta.available_end_date)
      || data.filters?.dept_code !== (requested.dept_code || null)
      || data.group_by !== requested.group_by || data.grain !== requested.grain
      || !Array.isArray(data.metrics) || data.metrics.join(',') !== requested.metrics.join(',')
      || !hasMetrics(data.totals) || !Array.isArray(data.rows) || data.rows.length > 10000
      || data.rows.some(row => !hasMetrics(row.values)
        || (data.group_by === 'department' ? row.period !== null : typeof row.period !== 'string')
        || (data.group_by === 'date' ? row.department !== null
          : typeof row.department !== 'string' || Boolean(requested.dept_code) && row.department !== requested.dept_code))) {
    throw new Error('Invalid report')
  }
  return data
}

function usePublishedDates(meta) {
  const { available_start_date: first, available_end_date: last } = meta
  if (dates.value?.days[0] !== first || dates.value?.days.at(-1) !== last) dates.value = reportDateChoices(first, last)
}

async function query() {
  if (busy.value || exporting.value || !draft.metrics.length) return
  result.value = null
  page.value = 1
  const window = range.value === 'custom' ? draft : dates.value?.months.find(item => item.month === range.value)
  const requested = {
    ...draft, start_date: window?.start_date ?? '', end_date: window?.end_date ?? '',
    dept_code: department || draft.dept_code, metrics: Object.keys(types).filter(key => draft.metrics.includes(key)),
  }
  let path
  try {
    path = multidimensionalPath(requested)
  } catch (cause) {
    error.value = cause.message
    return
  }
  result.value = await run(path, {
    read: data => {
      const report = readReport(data, requested)
      usePublishedDates(report.meta)
      chartMotionScope.commit()
      return report
    },
    invalid: '请核对查询范围及科室。',
  }) ?? null
}

async function initialize() {
  await query()
  if (dates.value || status.value !== 422) return
  const scope = buildQuery({ dept_code: department ?? '' })
  await run(`/api/v1/outpatient/summary${scope}`, {
    read: data => { usePublishedDates(validateDashboard(data, 'ads', department).meta) },
  })
  if (dates.value) error.value = '请选择较短的日期范围。'
}

function changeRange() {
  if (range.value !== 'custom') return query()
  const days = dates.value.days
  const filters = result.value?.filters
  draft.start_date = days.includes(filters?.start_date) ? filters.start_date : days[0]
  draft.end_date = days.includes(filters?.end_date) ? filters.end_date : days.at(-1)
}

function changeStart() {
  if (draft.end_date < draft.start_date) draft.end_date = draft.start_date
}

async function download() {
  const report = result.value
  if (!report || exporting.value) return
  exporting.value = true
  error.value = ''
  try {
    const response = await session.request(multidimensionalEndpoint + '/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: excel },
      body: JSON.stringify({
        ...report.filters, metrics: report.metrics, group_by: report.group_by, grain: report.grain, report_id: report.report_id,
      }),
      signal: AbortSignal.any([exportLifetime.signal, AbortSignal.timeout(15000)]),
    }, () => { void router.replace({ name: 'login', query: { expired: '1' } }) })
    if (response.status === 409) {
      error.value = '报表已更新，请重新查询。'
      return
    }
    if (!response.ok || response.headers.get('Content-Type')?.split(';')[0] !== excel) throw new Error('Export failed')
    const file = await response.blob()
    if (exportLifetime.signal.aborted) return
    const url = URL.createObjectURL(file)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = '多维报表.xlsx'
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch {
    if (!exportLifetime.signal.aborted) error.value = '导出失败，请重试。'
  } finally {
    exporting.value = false
  }
}

onMounted(initialize)
</script>

<template>
  <main class="workspace section-page cube-page">
    <WorkspaceHeading>
      <h1>多维报表</h1>
      <template #tools>
        <button type="button" class="button secondary" :disabled="!result?.rows.length || busy || exporting" @click="download">
          {{ exporting ? '导出中…' : '导出 Excel' }}
        </button>
      </template>
    </WorkspaceHeading>
    <section class="panel filter-panel cube-builder" aria-label="报表筛选">
      <form @submit.prevent="query">
        <div class="report-range-fields">
          <div class="field">
            <label for="cube-range">日期范围</label>
            <select id="cube-range" v-model="range" :disabled="!dates || busy || exporting" @change="changeRange">
              <option value="all">全期</option>
              <option v-for="item in dates?.months ?? []" :key="item.month" :value="item.month">
                {{ item.month.slice(0, 4) }}年{{ Number(item.month.slice(5)) }}月
              </option>
              <option v-if="dates" value="custom">自定义</option>
            </select>
          </div>
          <template v-if="range === 'custom'">
            <div class="field">
              <label for="cube-start">开始日期</label>
              <select id="cube-start" v-model="draft.start_date" :disabled="busy || exporting" @change="changeStart">
                <option v-for="day in dates.days" :key="day" :value="day">{{ day }}</option>
              </select>
            </div>
            <div class="field">
              <label for="cube-end">结束日期</label>
              <select id="cube-end" v-model="draft.end_date" :disabled="busy || exporting">
                <option v-for="day in endDates" :key="day" :value="day">{{ day }}</option>
              </select>
            </div>
          </template>
          <div class="field"><label for="cube-group">维度</label><select id="cube-group" v-model="draft.group_by" :disabled="busy || exporting">
            <option value="date_department">日期 × 科室</option><option value="date">日期</option><option value="department">科室</option>
          </select></div>
          <div class="field"><label for="cube-grain">粒度</label><select id="cube-grain" v-model="draft.grain"
            :disabled="busy || exporting || draft.group_by === 'department'">
            <option value="day">按日</option><option value="week">按周</option><option value="month">按月</option>
          </select></div>
          <div class="field"><label for="cube-dept">科室代码</label><input id="cube-dept" v-model.trim="draft.dept_code"
            placeholder="全院" maxlength="64" :readonly="Boolean(department)" :disabled="busy || exporting"></div>
          <button type="submit" class="button primary" :disabled="busy || exporting || !draft.metrics.length">查询</button>
        </div>
        <fieldset class="report-indicators" :disabled="busy || exporting">
          <legend class="report-visually-hidden">指标</legend>
          <label v-for="([label], key) in types" :key="key"><input v-model="draft.metrics" type="checkbox" :value="key">{{ label }}</label>
        </fieldset>
      </form>
    </section>
    <DashboardLoading :active="busy" layout="multidimensional" label="正在读取报表…" :cards="draft.metrics.length">
      <p v-if="error" class="report-error business-section" role="alert">{{ error }}</p>
      <template v-if="result">
        <section class="panel chart-panel cube-report" aria-labelledby="cube-result-title">
          <div class="cube-heading">
            <h2 id="cube-result-title">报表明细 <small>{{ result.rows.length }} 行</small></h2>
            <span class="available-range">{{ result.filters.start_date }} 至 {{ result.filters.end_date }} · {{ result.filters.dept_code ? outpatientDepartmentName(result.filters.dept_code) : '全院' }}</span>
          </div>
          <dl class="cube-totals" aria-label="报表合计">
            <div v-for="metric in result.metrics" :key="metric">
              <dt>{{ types[metric][0] }}</dt>
              <dd><MotionNumber :value="result.totals[metric]" :format="value => formatMetric(value, types[metric][1])" /></dd>
            </div>
          </dl>
          <div v-if="visibleRows.length" class="table-scroll multidimensional-table" tabindex="0" aria-label="多维报表明细">
            <table>
              <thead><tr>
                <th v-if="result.group_by !== 'department'" scope="col">期间</th>
                <th v-if="result.group_by !== 'date'" scope="col">科室</th>
                <th v-for="metric in result.metrics" :key="metric" class="report-number" scope="col">{{ types[metric][0] }}（{{ types[metric][1] }}）</th>
              </tr></thead>
              <tbody><tr v-for="row in visibleRows" :key="JSON.stringify([row.period, row.department])">
                <td v-if="result.group_by !== 'department'">{{ row.period }}</td>
                <td v-if="result.group_by !== 'date'" :title="row.department">{{ outpatientDepartmentName(row.department) }}</td>
                <td v-for="metric in result.metrics" :key="metric" class="report-number">
                  {{ formatMetric(row.values[metric], types[metric][1] === '元' ? '元' : '').replace(/ 元$/, '') }}
                </td>
              </tr></tbody>
            </table>
          </div>
          <p v-else>暂无记录</p>
          <div v-if="pageCount > 1" class="report-pagination">
            <button type="button" class="button secondary" :disabled="page === 1" @click="page--">上一页</button>
            <span>{{ (page - 1) * pageSize + 1 }}–{{ Math.min(page * pageSize, result.rows.length) }} / {{ result.rows.length }} 行</span>
            <button type="button" class="button secondary" :disabled="page === pageCount" @click="page++">下一页</button>
          </div>
        </section>
      </template>
    </DashboardLoading>
  </main>
</template>

<style scoped>
.cube-builder { margin-bottom: 14px; }
.cube-builder .report-range-fields { gap: 8px 12px; }
.cube-builder .report-range-fields > .field { flex-basis: 145px; }
.cube-builder .report-indicators { gap: 6px 8px; margin: 12px 0 0; }
.cube-builder .report-indicators label { min-height: 34px; border-radius: 9px; }
.cube-heading { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 5px 14px; }
.cube-heading h2 { margin: 0; }
.cube-heading small { margin-left: 6px; color: var(--muted); font-size: .74rem; font-weight: 500; }
.cube-totals { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(166px, 100%), 1fr)); gap: 10px 20px; margin: 12px 0; padding: 12px 14px; border-radius: 10px; background: var(--surface-tint); }
.cube-totals dt { color: var(--muted); font-size: .75rem; }
.cube-totals dd { margin: 2px 0 0; color: var(--ink); font-size: clamp(1rem, 1.4vw, 1.35rem); font-variant-numeric: tabular-nums; }
.cube-page :deep(.skeleton-cube-report) { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(150px, 100%), 1fr)); gap: 14px; padding: 14px; }
.cube-report .multidimensional-table { max-height: 440px; margin-top: 0; }
.multidimensional-table thead th { position: sticky; top: 0; z-index: 1; background: var(--surface-tint); }
.multidimensional-table tbody tr:nth-child(even) { background: var(--page-base); }
.multidimensional-table .report-number { font-variant-numeric: tabular-nums; }
@media (max-width: 640px) {
  .cube-builder .report-range-fields > .field { flex: 1 1 110px; }
  .cube-totals { grid-template-columns: repeat(auto-fit, minmax(min(120px, 100%), 1fr)); }
}
</style>
