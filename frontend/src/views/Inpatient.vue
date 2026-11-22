<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { departmentChartColor, outpatientDepartmentName as departmentName } from '../api/departmentLabels.mjs'
import { requestWithSession } from '../api/request.js'
import DashboardLoading from '../components/DashboardLoading.vue'
import DateRangeField from '../components/DateRangeField.vue'
import OutpatientDepartmentMix from '../components/OutpatientDepartmentMix.vue'
import WorkspaceHeading from '../components/WorkspaceHeading.vue'
import MotionNumber from '../components/charts/MotionNumber.vue'
import RankBar from '../components/charts/RankBar.vue'
import TrendChart from '../components/charts/TrendChart.vue'
import { useSession } from '../store/user.js'
import { provideChartMotion } from '../styles/chartMotion.mjs'

defineOptions({ name: 'InpatientView' })

const router = useRouter()
const session = useSession()
const department = session.current?.user.department || ''
const defaults = { start_date: '', end_date: '', dept_code: department }
const filters = reactive({ ...defaults })
const history = ref([])
const options = reactive(new Map(department ? [[department, departmentName(department)]] : []))
const data = shallowRef(null)
const busy = ref(false)
const error = ref('')
const chartMotionScope = provideChartMotion(() => Boolean(data.value))
const trend = computed(() => (data.value?.daily_trend || []).map(row => ({ date: row.date, visits: row.discharges })))
const peak = computed(() => trend.value.reduce((highest, row) => !highest || row.visits > highest.visits ? row : highest, null))
const dayView = computed(() => data.value?.filters.start_date === data.value?.filters.end_date)
const departmentRows = computed(() => (data.value?.details?.departments || []).map(row => ({
  dept_code: row.name,
  dept_label: row.label && row.label !== row.name ? row.label : departmentName(row.name),
  dept_color: departmentChartColor(row.name),
  visits: row.count,
  stay: row.value,
})).sort((left, right) => right.visits - left.visits))
const equalShares = computed(() => departmentRows.value.length > 1 &&
  departmentRows.value.every(row => row.visits === departmentRows.value[0].visits))
const stayRows = computed(() => departmentRows.value
  .filter(row => row.stay !== null)
  .map(row => ({ ...row, visits: Number(row.stay) }))
  .sort((left, right) => right.visits - left.visits))
const compareDepartments = computed(() => data.value && !data.value.filters.dept_code && departmentRows.value.length > 0)
const integer = new Intl.NumberFormat('zh-CN')
const decimal = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })
const money = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 })
let pending

async function load(scope = filters) {
  pending?.abort()
  const controller = pending = new AbortController()
  busy.value = true
  error.value = ''
  data.value = null
  try {
    const query = new URLSearchParams(Object.entries(scope).filter(([, value]) => value))
    const response = await requestWithSession(session, `/api/v1/inpatient/summary?${query}`, {
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]),
    }, () => { void router.replace({ name: 'login', query: { expired: '1' } }) })
    if (controller.signal.aborted) return false
    if (!response.ok) {
      error.value = response.status === 422 ? '请检查日期及科室范围。' : '住院汇总暂不可用。'
      return false
    }
    const payload = await response.json()
    if (controller.signal.aborted) return false
    if (payload.code !== 0 || !payload.data?.filters) throw new Error('Invalid inpatient summary')
    chartMotionScope.commit()
    data.value = payload.data
    const actual = data.value.filters
    Object.assign(filters, actual, { dept_code: actual.dept_code || '' })
    for (const row of departmentRows.value) options.set(row.dept_code, row.dept_label)
    return true
  } catch {
    if (!controller.signal.aborted) error.value = '暂时无法读取住院汇总，请重试。'
    return false
  } finally {
    if (pending === controller) busy.value = false
  }
}

async function query() {
  if (await load()) history.value = []
}

async function drill(scope) {
  if (busy.value || !data.value) return
  const previous = { ...data.value.filters }
  Object.assign(filters, previous, scope)
  history.value.push(previous)
  await load()
}

async function goBack() {
  const previous = history.value.at(-1)
  if (!previous) return
  Object.assign(filters, previous)
  if (await load()) history.value.pop()
}

async function reset() {
  Object.assign(filters, defaults)
  await query()
}

onMounted(() => load())
onBeforeUnmount(() => pending?.abort())
</script>

<template>
  <main class="workspace section-page inpatient-page">
    <WorkspaceHeading>
      <h1>住院分析</h1>
      <template #tools>
        <form class="inpatient-tools" aria-label="筛选住院数据" @submit.prevent="query">
          <DateRangeField v-model:start="filters.start_date" v-model:end="filters.end_date" :disabled="busy" />
          <select v-model="filters.dept_code" class="inpatient-select" aria-label="科室" :disabled="busy || Boolean(department)">
            <option v-if="!department" value="">全院</option>
            <option v-for="[code, label] in options" :key="code" :value="code">{{ label }}</option>
          </select>
          <button class="button primary" :disabled="busy">查询</button>
          <button v-if="history.length" type="button" class="button secondary" :disabled="busy" @click="goBack">返回上一级</button>
          <button v-else type="button" class="button secondary" :disabled="busy" @click="reset">重置</button>
        </form>
      </template>
    </WorkspaceHeading>

    <DashboardLoading :active="busy" layout="inpatient" label="正在读取住院汇总…"
      :single-day="Boolean(filters.start_date) && filters.start_date === filters.end_date" :scoped="Boolean(filters.dept_code)">
      <div v-if="error" class="status-announcer query-error" role="alert">
        <span>{{ error }}</span><button type="button" class="button secondary" @click="load()">重试</button>
      </div>

      <template v-if="data">
        <div class="inpatient-main" :class="{ 'inpatient-single': dayView && !compareDepartments }">
          <section class="panel inpatient-panel" aria-labelledby="inpatient-metrics">
            <h2 id="inpatient-metrics">住院运营指标</h2>
            <dl class="inpatient-metrics">
              <div class="inpatient-total">
                <dt>出院人次</dt>
                <dd><MotionNumber :value="data.discharges" /> <small>人次</small></dd>
              </div>
              <div>
                <dt>平均住院日</dt>
                <dd v-if="data.avg_stay_days.value !== null"><MotionNumber :value="data.avg_stay_days.value" :format="decimal.format" /> <small>天</small></dd>
                <dd v-else>—</dd>
              </div>
              <div>
                <dt>次均住院费用</dt>
                <dd v-if="data.avg_cost.value !== null"><MotionNumber :value="data.avg_cost.value" :format="money.format" /> <small>元</small></dd>
                <dd v-else>—</dd>
              </div>
              <div>
                <dt>入院人次</dt>
                <dd><MotionNumber :value="data.admissions ?? null" /> <small v-if="data.admissions != null">人次</small></dd>
              </div>
              <div>
                <dt>床位使用率</dt>
                <dd><MotionNumber :value="data.bed_usage_rate?.value ?? null" :format="decimal.format" /><small v-if="data.bed_usage_rate?.value != null">%</small></dd>
              </div>
              <div v-if="peak && !dayView" class="inpatient-peak">
                <dt>单日出院峰值</dt>
                <dd>
                  <button type="button" class="inpatient-link peak-link" aria-label="查看出院峰值日"
                    @click="drill({ start_date: peak.date, end_date: peak.date })">
                    <span><MotionNumber :value="peak.visits" /> <small>人次</small></span><time :datetime="peak.date">{{ peak.date }}</time>
                  </button>
                </dd>
              </div>
            </dl>
          </section>

          <section v-if="!dayView" class="panel inpatient-panel inpatient-trend" aria-labelledby="inpatient-trend">
            <div class="inpatient-panel-heading">
              <h2 id="inpatient-trend">每日出院趋势</h2>
              <span v-if="trend.length" class="inpatient-legend">出院人次</span>
            </div>
            <TrendChart v-if="trend.length" :data="trend" label="出院人次" unit="人次" period="日" :highlight-date="peak?.date" drillable
              @select-row="row => drill({ start_date: row.date, end_date: row.date })" />
            <p v-else class="chart-empty">当前范围无出院记录</p>
          </section>

          <section v-else-if="compareDepartments" class="panel inpatient-panel" aria-labelledby="inpatient-day">
            <h2 id="inpatient-day">当日科室出院量</h2>
            <RankBar :data="departmentRows" label="出院人次" category="科室" unit="人次" compact drillable
              @select-row="row => drill({ dept_code: row.dept_code })" />
          </section>
        </div>

        <div v-if="compareDepartments && (!dayView || stayRows.length)" class="inpatient-secondary">
          <section v-if="!dayView" class="panel inpatient-panel" aria-labelledby="inpatient-mix">
            <h2 id="inpatient-mix">{{ equalShares ? '科室出院构成' : '科室出院量排名' }}</h2>
            <OutpatientDepartmentMix v-if="equalShares" :rows="departmentRows" :total="data.discharges" unit-label="出院人次"
              @select-department="code => drill({ dept_code: code })" />
            <RankBar v-else :data="departmentRows" label="出院人次" category="科室" unit="人次" compact drillable
              @select-row="row => drill({ dept_code: row.dept_code })" />
          </section>
          <section v-if="stayRows.length" class="panel inpatient-panel" aria-labelledby="inpatient-stays">
            <h2 id="inpatient-stays">科室平均住院日</h2>
            <RankBar :data="stayRows" label="平均住院日" category="科室" unit="天" compact drillable
              @select-row="row => drill({ dept_code: row.dept_code })" />
          </section>
        </div>

        <section class="panel inpatient-panel" aria-labelledby="inpatient-details">
          <h2 id="inpatient-details">科室住院明细</h2>
          <div v-if="departmentRows.length" class="table-scroll">
            <table class="inpatient-table">
              <thead><tr><th scope="col">科室</th><th scope="col">出院人次</th><th scope="col">出院占比</th><th scope="col">平均住院日（天）</th></tr></thead>
              <tbody><tr v-for="row in departmentRows" :key="row.dept_code">
                <th scope="row">
                  <button v-if="compareDepartments" type="button" class="inpatient-link" @click="drill({ dept_code: row.dept_code })">
                    <span class="inpatient-dot" :style="{ backgroundColor: row.dept_color }" aria-hidden="true"></span>{{ row.dept_label }}
                  </button>
                  <span v-else>{{ row.dept_label }}</span>
                </th>
                <td>{{ integer.format(row.visits) }}</td>
                <td>{{ data.discharges ? percent.format(row.visits / data.discharges) : '—' }}</td>
                <td>{{ row.stay === null ? '—' : decimal.format(row.stay) }}</td>
              </tr></tbody>
            </table>
          </div>
          <p v-else class="chart-empty">当前范围无科室明细</p>
        </section>
      </template>
    </DashboardLoading>
  </main>
</template>

<style scoped>
.inpatient-page, .inpatient-page :deep(.dashboard-stage-content) { display: grid; gap: 14px; }
.inpatient-page :deep(.workspace-heading) { margin-bottom: 0; }
.inpatient-tools { display: flex; align-items: center; gap: 8px; }
.inpatient-tools :deep(.overview-date-range summary) { white-space: nowrap; }
.inpatient-tools :is(.button, .inpatient-select) { min-height: 36px; padding: 5px 10px; border-radius: 10px; }
.inpatient-select { max-width: 150px; border: 1px solid var(--border); background: white; color: var(--ink); font-size: .82rem; }
.inpatient-panel { min-width: 0; padding: 18px; border: 1px solid var(--border); border-radius: 16px; background: white; box-shadow: 0 2px 8px rgba(23, 35, 61, .03); }
.inpatient-panel h2 { margin: 0 0 12px; color: var(--ink); font-size: 1.02rem; line-height: 1.35; }
.inpatient-panel-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.inpatient-legend { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: .75rem; white-space: nowrap; }
.inpatient-legend::before { content: ''; width: 16px; height: 3px; border-radius: 3px; background: var(--brand-blue); }
.inpatient-metrics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 0; }
.inpatient-metrics > div { min-width: 0; padding: 10px; border-radius: 11px; background: var(--page-base); }
.inpatient-metrics .inpatient-total { grid-column: 1 / -1; padding: 15px 13px; background: var(--surface-tint); }
.inpatient-metrics dt { color: var(--muted); font-size: .76rem; }
.inpatient-metrics dd { margin: 3px 0 0; color: var(--ink); font-size: clamp(1.05rem, 1.5vw, 1.4rem); font-weight: 720; font-variant-numeric: tabular-nums; line-height: 1.2; }
.inpatient-metrics .inpatient-total dd { color: var(--brand-blue); font-size: clamp(2rem, 3vw, 2.65rem); }
.inpatient-metrics small { color: var(--muted); font-size: .72rem; font-weight: 500; }
.inpatient-metrics .inpatient-peak { grid-column: 1 / -1; }
.inpatient-link { display: inline-flex; align-items: center; gap: 8px; min-height: 28px; padding: 0; border: 0; background: transparent; color: var(--brand-blue); font: inherit; text-align: left; }
.inpatient-link:hover { text-decoration: underline; text-underline-offset: 3px; }
.peak-link { justify-content: space-between; width: 100%; }
.peak-link time { color: var(--muted); font-size: .78rem; font-weight: 550; }
.inpatient-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.inpatient-table { min-width: 390px; }
.inpatient-table :is(th, td):not(:first-child) { text-align: right; font-variant-numeric: tabular-nums; }
@media (max-width: 1050px) {
  .inpatient-tools { flex-wrap: wrap; }
}
</style>
