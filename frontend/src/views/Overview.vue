<script setup>
import { provideChartMotion } from '../styles/chartMotion.mjs'
import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { buildQuery } from '../api/dashboard.mjs'
import { departmentChartColor, outpatientDepartmentName } from '../api/departmentLabels.mjs'
import { overviewBedPeriods, overviewDays, overviewFocus, overviewPeriods, sparkline } from '../api/overview.mjs'
import { appendedScope, appendOverview } from '../api/overviewImport.mjs'
import { useSession } from '../store/user.js'
import DashboardLoading from '../components/DashboardLoading.vue'
import DateRangeField from '../components/DateRangeField.vue'
import OverviewCsvImport from '../components/OverviewCsvImport.vue'
import OverviewTrend from '../components/OverviewTrend.vue'
import MotionNumber from '../components/charts/MotionNumber.vue'
import TrendChart from '../components/charts/TrendChart.vue'
import WorkspaceIcon from '../components/WorkspaceIcon.vue'
import WorkspaceHeading from '../components/WorkspaceHeading.vue'
import { chartTheme, revenueCareColors } from '../styles/theme.js'

defineOptions({ name: 'OverviewView' })

const router = useRouter()
const chartMotionScope = provideChartMotion(() => Boolean(result.value))
const { phase: motionPhase, revision: motionRevision } = chartMotionScope
const session = useSession()
const department = session.current?.user.department ?? null
const filters = reactive({ start_date: '', end_date: '', dept_code: department || '' })
const result = shallowRef(null)
const importedData = shallowRef(null)
const departmentOptions = shallowRef([])
const publication = shallowRef(null)
const visibleDepartments = computed(() => {
  const choices = new Map(departmentOptions.value.map(row => [row.code, row]))
  for (const code of importedData.value?.departments ?? []) {
    if (!choices.has(code)) choices.set(code, { code })
  }
  return [...choices.values()].sort((a, b) => a.code.localeCompare(b.code))
})
const busy = ref(false)
const error = ref('')
const grain = ref('day')
const trendMode = ref('operating')
let pending

const number = new Intl.NumberFormat('zh-CN')
const money = new Intl.NumberFormat('zh-CN', { style: 'decimal', maximumFractionDigits: 2, minimumFractionDigits: 2 })
const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 })
const rate = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })
const signedPercent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1, signDisplay: 'exceptZero' })
const care = { OUTPATIENT: '门诊', INPATIENT: '住院', OTHER: '其他' }
const colors = [chartTheme.primary, chartTheme.ranking, chartTheme.support]
function departmentLabel(code, fallback = code) {
  const name = outpatientDepartmentName(code)
  return name === code ? fallback : name
}
const metrics = {
  outpatient: { label: '门诊人次', unit: '人次', icon: 'people', color: colors[0] },
  inpatient: { label: '出院人次', unit: '人次', icon: 'bed', color: colors[1] },
  amount: { label: '收费金额', unit: '元', icon: 'money', color: chartTheme.support },
}
const trendColors = [chartTheme.primary, chartTheme.ranking, chartTheme.text]
const grains = [['day', '按日'], ['week', '按周'], ['month', '按月']]
const daily = computed(() => result.value ? overviewDays(result.value) : [])
const series = computed(() => overviewPeriods(daily.value, grain.value, Boolean(importedData.value)))
const beds = computed(() => importedData.value ? [] : result.value?.bed_trend ?? [])
const bedSeries = computed(() => overviewBedPeriods(beds.value, grain.value))
const occupiedBeds = computed(() => beds.value.reduce((sum, row) => sum + row.occupied, 0))
const bedTrendActive = computed(() => beds.value.length > 0 && trendMode.value === 'beds')
const focus = computed(() => overviewFocus(daily.value))
const unapplied = computed(() => {
  const applied = result.value?.filters
  return applied && (applied.start_date !== filters.start_date || applied.end_date !== filters.end_date
    || (applied.dept_code || '') !== filters.dept_code)
})
const departments = computed(() => [...(result.value?.departments ?? [])].sort((a, b) => {
  if (a.amount === null) return b.amount === null ? a.code.localeCompare(b.code) : 1
  if (b.amount === null) return -1
  return Number(b.amount) - Number(a.amount) || a.code.localeCompare(b.code)
}))
const ranked = computed(() => departments.value.filter(row => row.amount !== null).slice(0, 5))
const rankMax = computed(() => Math.max(0, ...ranked.value.map(row => Number(row.amount))))
const chargeTotal = computed(() => result.value?.charge_total ?? null)
const mix = computed(() => {
  const rows = result.value?.charges ?? []
  const complete = chargeTotal.value !== null && Number(chargeTotal.value) > 0
    && rows.every(row => row.value !== null && Number(row.value) >= 0)
  return rows.map(row => ({
    label: care[row.name] ?? row.label, code: row.name, value: row.value,
    color: revenueCareColors[row.name] ?? revenueCareColors.OTHER, share: complete ? Number(row.value) / Number(chargeTotal.value) : null,
  }))
})
const ring = computed(() => {
  let offset = 0
  const segments = mix.value.filter(row => row.share > 0).map(row => {
    const start = offset
    offset += row.share * 100
    return `${row.color} ${start}% ${offset}%`
  })
  if (segments.length < 2) return mix.value.find(row => row.share > 0)?.color ?? 'var(--surface-tint)'
  return `conic-gradient(${segments.join(', ')})`
})
const kpis = computed(() => {
  if (!result.value) return []
  return [
    ...['outpatient', 'inpatient'].map(key => {
      const values = daily.value.map(row => row[key])
      const reported = values.filter(value => value !== null)
      const value = reported.length ? reported.reduce((sum, value) => sum + value, 0) : null
      return { ...metrics[key], value, format: number.format, unit: value === null ? '' : metrics[key].unit,
        spark: sparkline(values.slice(-14)) }
    }),
    ...['OUTPATIENT', 'INPATIENT'].map((code, i) => {
      const row = mix.value.find(item => item.code === code)
      return { label: `${care[code]}收费收入`, icon: i ? 'bed' : 'money', color: colors[i],
        value: row?.value ?? null, format: money.format, unit: row?.value == null ? '' : '元', share: row?.share }
    }),
  ]
})
const coverage = computed(() => [
  { ...metrics.outpatient, label: '门诊记录', count: daily.value.filter(row => row.outpatient > 0).length },
  { ...metrics.inpatient, label: '出院记录', count: daily.value.filter(row => row.inpatient > 0).length },
  { ...metrics.amount, label: '收费金额可用', count: daily.value.filter(row => row.chargeRecorded).length },
])

function useCourseData() {
  importedData.value = null
  Object.assign(filters, { start_date: '', end_date: '', dept_code: department || '' })
  void load()
}

async function readCourse(selected, controller) {
  const response = await session.request(`/api/v1/overview/summary${buildQuery(selected)}`, {
    signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]),
  }, () => { void router.replace({ name: 'login', query: { expired: '1' } }) })
  if (!response.ok) throw new Error(response.status === 422 ? '请检查日期及科室范围。' : '运营总览暂不可用。')
  const payload = await response.json()
  const data = payload.data
  if (payload.code !== 0 || data?.meta?.source !== 'dwd_visit_detail' || !data.filters
    || data.filters.dept_code !== (selected.dept_code || null)
    || (selected.start_date && data.filters.start_date !== selected.start_date)
    || (selected.end_date && data.filters.end_date !== selected.end_date)
    || !Array.isArray(data.activity) || !Array.isArray(data.charges)) {
    throw new Error('总览数据与请求范围不一致。')
  }
  return data
}

async function readAppendedCourse(selected, controller, knownDepartment) {
  const requireCurrentPublication = data => {
    if (data.meta.build_run_id !== publication.value.build_run_id) {
      throw new Error('课程数据已更新，请清除 CSV 后重新导入。')
    }
    return data
  }
  if (!knownDepartment) {
    const available = requireCurrentPublication(await readCourse({ ...selected, dept_code: null }, controller))
    if (!available.departments.some(row => row.code === selected.dept_code)) return null
  }
  return requireCurrentPublication(await readCourse(selected, controller))
}

async function load(dataset = importedData.value, selected = filters, importing = false) {
  pending?.abort()
  const controller = pending = new AbortController()
  const requested = { ...selected, dept_code: department || selected.dept_code || '' }
  busy.value = true
  error.value = ''
  if (!dataset) result.value = null
  try {
    let data
    if (dataset) {
      const scope = appendedScope(publication.value, dataset, requested)
      const courseDepartment = !requested.dept_code || departmentOptions.value.some(row => row.code === requested.dept_code)
      if (!courseDepartment && !dataset.departments.has(requested.dept_code)) throw new Error('请选择已有科室。')
      const course = scope.course ? await readAppendedCourse(scope.course, controller, courseDepartment) : null
      data = appendOverview(course, dataset, scope.filters)
    } else {
      data = await readCourse(requested, controller)
      if (controller.signal.aborted) return
      if (publication.value?.build_run_id !== data.meta.build_run_id) departmentOptions.value = []
      publication.value = data.meta
      if (!data.filters.dept_code && Array.isArray(data.departments)) {
        const choices = new Map(departmentOptions.value.map(row => [row.code, row]))
        for (const row of data.departments) choices.set(row.code, row)
        departmentOptions.value = [...choices.values()]
      }
    }
    if (controller.signal.aborted) return
    chartMotionScope.commit()
    importedData.value = dataset
    result.value = data
    Object.assign(filters, data.filters, { dept_code: data.filters.dept_code || '' })
  } catch (cause) {
    if (controller.signal.aborted) return
    if (importing) throw cause
    error.value = cause.name === 'Error' ? cause.message : '暂时无法读取运营总览。'
  } finally {
    if (pending === controller) busy.value = false
  }
}

onMounted(load)
onBeforeUnmount(() => pending?.abort())
</script>

<template>
  <main class="workspace hospital-overview">
    <WorkspaceHeading class="overview-top">
      <h1>管理驾驶舱</h1>
      <p v-if="unapplied" class="overview-scope">{{ departmentLabel(result.filters.dept_code) || '全院' }} · {{ result.filters.start_date }} 至 {{ result.filters.end_date }}</p>
      <template #tools>
        <form class="overview-filter" aria-label="运营总览筛选" @submit.prevent="load()">
          <DateRangeField v-model:start="filters.start_date" v-model:end="filters.end_date" :disabled="busy"
            :min="importedData ? publication.available_start_date : undefined" :max="importedData?.end" />
          <div class="overview-filter-group">
            <WorkspaceIcon name="department" />
            <input id="overview-dept" v-model.trim="filters.dept_code" list="overview-departments" placeholder="全院" aria-label="科室代码"
              maxlength="64" :readonly="Boolean(department)" :disabled="busy">
            <datalist id="overview-departments">
              <option v-for="row in visibleDepartments" :key="row.code" :value="row.code">{{ departmentLabel(row.code, row.label) }}</option>
            </datalist>
          </div>
          <button type="submit" class="button primary" :disabled="busy">查询</button>
          <OverviewCsvImport v-if="session.current?.user.role === 'admin'" :disabled="busy || !publication"
            :previous="importedData" :cutoff="publication?.available_end_date" :apply="dataset => load(dataset, {}, true)" />
          <button v-if="importedData" type="button" class="button secondary" @click="useCourseData">清除追加数据</button>
        </form>
      </template>
    </WorkspaceHeading>
    <p v-if="importedData" class="overview-scope overview-import-status" role="status">
      课程数据 + CSV · 累计追加 {{ number.format(importedData.added) }} 条 · 本次跳过 {{ number.format(importedData.skipped) }} 条重复
    </p>
    <DashboardLoading :active="busy" layout="overview" label="正在读取运营总览…">
      <div v-if="error" class="status-announcer query-error" role="alert">
        <span>{{ error }}</span><button v-if="!importedData" type="button" class="button secondary" @click="load()">重试</button>
      </div>
      <div v-if="result" :class="{ 'chart-motion-update': motionPhase === 'update' }">
        <section class="overview-kpis" aria-label="运营指标">
          <article v-for="item in kpis" :key="item.label" class="panel overview-kpi" :style="{ '--series': item.color }">
            <div class="overview-kpi-label"><span class="overview-icon"><WorkspaceIcon :name="item.icon" /></span><h2>{{ item.label }}</h2></div>
            <div class="overview-kpi-value"><MotionNumber :value="item.value" :format="item.format" /> <span>{{ item.unit }}</span></div>
            <svg v-if="item.spark" :key="motionRevision" class="overview-spark" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
              <polyline :points="item.spark" pathLength="1" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" />
            </svg>
            <div v-else-if="item.share != null" class="overview-kpi-share">
              <span>收费占比 {{ percent.format(item.share) }}</span>
              <div class="overview-track" aria-hidden="true"><i :key="motionRevision" :style="{ width: item.share * 100 + '%' }"></i></div>
            </div>
          </article>
        </section>
        <div class="overview-middle">
          <section class="panel overview-panel">
            <h2>重点关注</h2>
            <div class="overview-focus">
              <article v-for="row in focus" :key="row.key" :style="{ '--series': metrics[row.key].color }">
                <span class="overview-icon"><WorkspaceIcon :name="metrics[row.key].icon" /></span>
                <div>
                  <h3>近 {{ row.days }} 日{{ metrics[row.key].label }}</h3>
                  <p><MotionNumber :value="row.value === null ? null : row.key === 'amount' ? row.value / 100 : row.value"
                    :format="row.key === 'amount' ? money.format : number.format" />
                    <span v-if="row.value !== null">{{ metrics[row.key].unit }}</span></p>
                </div>
                <span v-if="row.change !== null" class="overview-change" :class="{ 'overview-change-down': row.change < 0 }">
                  {{ signedPercent.format(row.change) }}<small>较前 7 日</small>
                </span>
              </article>
            </div>
          </section>
          <section class="panel overview-panel">
            <div class="overview-panel-head">
              <h2>{{ bedTrendActive ? '床位使用趋势' : '运营趋势分析' }}</h2>
              <div class="overview-grains" role="group" aria-label="趋势粒度">
                <button v-for="[key, label] in grains" :key="key" type="button" :aria-pressed="grain === key" @click="grain = key">{{ label }}</button>
              </div>
            </div>
            <div class="overview-legend">
              <span v-if="bedTrendActive" :style="{ '--series': trendColors[0] }"><i></i>床位使用率</span>
              <template v-else>
                <span v-for="(item, key, index) in metrics" :key="key" :style="{ '--series': trendColors[index] }">
                  <i :class="{ 'overview-legend-dashed': key === 'inpatient' }"></i>{{ item.label }}
                </span>
              </template>
              <button v-if="beds.length" class="overview-trend-toggle" type="button"
                :aria-pressed="bedTrendActive" @click="trendMode = bedTrendActive ? 'operating' : 'beds'">
                床位趋势
              </button>
            </div>
            <TrendChart v-if="bedTrendActive" :data="bedSeries" label="床位使用率" unit="%" :period="{ day: '日', week: '周', month: '月' }[grain]" />
            <OverviewTrend v-else-if="series.length" :rows="series" :grain="grain" :colors="trendColors" />
            <p v-else class="overview-empty">暂无记录</p>
          </section>
        </div>
        <div class="overview-bottom">
          <section class="panel overview-panel">
            <h2>收入结构</h2>
            <div v-if="mix.length" class="overview-mix">
              <div class="overview-ring" :style="{ '--ring-fill': ring }">
                <i :key="motionRevision" class="overview-ring-paint" :class="{ 'overview-ring-ready': mix.some(row => row.share > 0) }" aria-hidden="true"></i>
                <div><span>收费合计</span><strong>{{ chargeTotal === null ? '—' : money.format(chargeTotal) }}</strong><small v-if="chargeTotal !== null">元</small></div>
              </div>
              <dl class="overview-mix-list">
                <div v-for="row in mix" :key="row.code" :style="{ '--series': row.color }">
                  <dt><i></i>{{ row.label }}收费</dt>
                  <dd v-if="row.share !== null" class="overview-mix-percent">{{ percent.format(row.share) }}</dd>
                  <dd class="overview-mix-amount">{{ row.value === null ? '—' : money.format(row.value) + ' 元' }}</dd>
                </div>
              </dl>
            </div>
            <p v-else class="overview-empty">暂无收费记录</p>
          </section>
          <section class="panel overview-panel">
            <h2>运营数据覆盖</h2>
            <dl class="overview-coverage">
              <div v-for="row in coverage" :key="row.label" :style="{ '--series': row.color }">
                <span class="overview-icon" aria-hidden="true"><WorkspaceIcon :name="row.icon" /></span>
                <div>
                  <div><dt>{{ row.label }}</dt><dd>{{ row.count }} / {{ daily.length }} 天</dd></div>
                  <div class="overview-track" aria-hidden="true"><i :key="motionRevision" :style="{ width: (daily.length ? row.count / daily.length * 100 : 0) + '%' }"></i></div>
                </div>
              </div>
            </dl>
            <dl v-if="result.bed_usage_rate?.value != null && beds.length" class="overview-bed-metrics" aria-label="床位效率">
              <div><dt>床位使用率</dt><dd>{{ rate.format(result.bed_usage_rate.value) }}%</dd></div>
              <div><dt>占用床日</dt><dd>{{ number.format(occupiedBeds) }}</dd></div>
            </dl>
          </section>
          <section class="panel overview-panel">
            <h2>科室收费排行</h2>
            <ol v-if="ranked.length" class="overview-ranking">
              <li v-for="(row, index) in ranked" :key="row.code" :style="{ '--series': departmentChartColor(row.code) }">
                <span aria-hidden="true" class="overview-position">{{ index + 1 }}</span>
                <span :title="departmentLabel(row.code, row.label)" class="overview-rank-name">{{ departmentLabel(row.code, row.label) }}</span>
                <span class="overview-track" aria-hidden="true"><i :key="motionRevision" :style="{ width: (rankMax ? Math.max(0, Number(row.amount)) / rankMax * 100 : 0) + '%' }"></i></span>
                <strong>{{ money.format(row.amount) }} <small>元</small></strong>
              </li>
            </ol>
            <p v-else class="overview-empty">暂无收费排行</p>
          </section>
        </div>
        <details v-if="departments.length" class="panel overview-panel overview-detail">
          <summary>科室运营明细（{{ departments.length }} 个科室）</summary>
          <div class="table-scroll" tabindex="0" aria-label="科室运营明细，可滚动">
            <table>
              <thead><tr><th scope="col">科室</th><th scope="col">门诊人次</th><th scope="col">出院人次</th><th scope="col">收费金额（元）</th></tr></thead>
              <tbody><tr v-for="row in departments" :key="row.code">
                <th scope="row">{{ departmentLabel(row.code, row.label) }}</th><td>{{ row.outpatient === null ? '—' : number.format(row.outpatient) }}</td>
                <td>{{ row.inpatient === null ? '—' : number.format(row.inpatient) }}</td>
                <td>{{ row.amount === null ? '—' : money.format(row.amount) }}</td>
              </tr></tbody>
            </table>
          </div>
        </details>
        <details v-if="daily.length" class="panel overview-panel overview-detail">
          <summary>日度运营明细（{{ daily.length }} 天）</summary>
          <div class="table-scroll" tabindex="0" aria-label="日度运营明细，可滚动">
            <table>
              <thead><tr><th scope="col">日期</th><th scope="col">门诊人次</th><th scope="col">出院人次</th><th scope="col">收费金额（元）</th></tr></thead>
              <tbody><tr v-for="row in daily" :key="row.date">
                <th scope="row">{{ row.date }}</th><td>{{ row.outpatient === null ? '—' : number.format(row.outpatient) }}</td>
                <td>{{ row.inpatient === null ? '—' : number.format(row.inpatient) }}</td>
                <td>{{ row.amount === null ? '—' : money.format(row.amount / 100) }}</td>
              </tr></tbody>
            </table>
          </div>
        </details>
      </div>
    </DashboardLoading>
  </main>
</template>

<style scoped>
.overview-import-status { margin: 0 0 12px; }
.overview-trend-toggle { margin-inline-start: auto; border: 0; border-radius: 6px; padding: 4px 8px; background: var(--surface-tint); color: var(--brand-blue); font-size: .75rem; }
.overview-trend-toggle[aria-pressed='true'] { background: var(--brand-blue); color: white; }
.overview-bed-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(98px, 100%), 1fr)); gap: 6px; margin-top: 8px; }
.overview-bed-metrics div { padding: 5px 8px; border-radius: 8px; background: var(--page-base); }
.overview-bed-metrics dt { font-size: .74rem; }
.overview-bed-metrics dd { color: var(--brand-blue); font-weight: 700; font-variant-numeric: tabular-nums; }
</style>
